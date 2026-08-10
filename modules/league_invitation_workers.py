"""Bounded worker-local eligibility and persistence for league invitations."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from peewee import Case, fn

from modules import models


MAX_INVITATION_SCAN = 1000
MINIMUM_LIFETIME_MAX_ELO = 1075
MINIMUM_WINS = 5
MINIMUM_RECENT_GAMES = 1
HIGH_ELO_QUALIFICATION = 1150
RECENT_ACTIVITY_DAYS = 15


class LeagueInvitationError(RuntimeError):
    """Base error for a rejected invitation worker request."""


class LeagueInvitationValidationError(LeagueInvitationError):
    """A primitive request is invalid or outside its supported bounds."""


class LeagueInvitationConflictError(LeagueInvitationError):
    """The delivery target changed before persistence completed."""


@dataclass(frozen=True)
class LeagueInvitationEligibilityRequest:
    """Primitive policy and cursor snapshot for one bounded scan."""

    as_of: datetime.datetime
    polychampions_guild_id: int
    global_guild_ids: tuple[int, ...]
    era_start: datetime.date
    era_end: datetime.date
    after_member_id: int | None = None
    limit: int = MAX_INVITATION_SCAN


@dataclass(frozen=True)
class LeagueInvitationEvaluation:
    """Immutable evaluation for one scanned DiscordMember row."""

    member_id: int
    discord_id: int
    name: str
    wins: int
    losses: int
    recent_games: int
    elo_max_moonrise: int
    eligible: bool
    reason: str


@dataclass(frozen=True)
class LeagueInvitationBatch:
    """One bounded candidate page and its continuation cursor."""

    evaluations: tuple[LeagueInvitationEvaluation, ...]
    scanned_count: int
    truncated: bool
    next_after_member_id: int | None

    @property
    def eligible(self) -> tuple[LeagueInvitationEvaluation, ...]:
        return tuple(row for row in self.evaluations if row.eligible)


@dataclass(frozen=True)
class LeagueInvitationDeliveryRequest:
    """Primitive successful-DM identity to persist idempotently."""

    member_id: int
    discord_id: int
    sent_on: datetime.date


@dataclass(frozen=True)
class LeagueInvitationDeliveryResult:
    member_id: int
    discord_id: int
    sent_on: datetime.date
    recorded: bool


def _validated_limit(value: int) -> int:
    limit = int(value)
    if limit < 1 or limit > MAX_INVITATION_SCAN:
        raise LeagueInvitationValidationError(
            f'Invitation scan limit must be between 1 and '
            f'{MAX_INVITATION_SCAN}.'
        )
    return limit


def _candidate_members(request: LeagueInvitationEligibilityRequest):
    """Return at most one bounded page of legacy base candidates."""

    limit = _validated_limit(request.limit)
    polychampions_players = (
        models.Player
        .select(models.Player.discord_member)
        .where(
            models.Player.guild_id == int(request.polychampions_guild_id)
        )
    )
    conditions = (
        (models.DiscordMember.id.not_in(polychampions_players))
        & (
            models.DiscordMember.elo_max
            > MINIMUM_LIFETIME_MAX_ELO
        )
        & (models.DiscordMember.is_banned == 0)
        & models.DiscordMember.date_polychamps_invite_sent.is_null(True)
    )
    if request.after_member_id is not None:
        conditions &= (
            models.DiscordMember.id > int(request.after_member_id)
        )
    return tuple(
        models.DiscordMember
        .select(
            models.DiscordMember.id,
            models.DiscordMember.discord_id,
            models.DiscordMember.name,
            models.DiscordMember.elo_max_moonrise,
            models.DiscordMember.polytopia_id,
            models.DiscordMember.polytopia_name,
        )
        .where(conditions)
        .order_by(models.DiscordMember.id)
        .limit(limit + 1)
    )


def _record_counts(member_ids: tuple[int, ...], request):
    if not member_ids or not request.global_guild_ids:
        return {}
    wins = Case(
        None,
        ((models.Game.winner == models.Lineup.gameside, 1),),
        0,
    )
    losses = Case(
        None,
        ((models.Game.winner != models.Lineup.gameside, 1),),
        0,
    )
    query = (
        models.Lineup
        .select(
            models.Player.discord_member.alias('member_id'),
            fn.SUM(wins).alias('wins'),
            fn.SUM(losses).alias('losses'),
        )
        .join(models.Player)
        .switch(models.Lineup)
        .join(models.Game)
        .where(
            (models.Player.discord_member.in_(member_ids))
            & (models.Game.is_completed == 1)
            & (models.Game.is_confirmed == 1)
            & (models.Game.is_ranked == 1)
            & (models.Game.guild_id.in_(request.global_guild_ids))
            & (models.Game.date >= request.era_start)
            & (models.Game.date <= request.era_end)
        )
        .group_by(models.Player.discord_member)
        .dicts()
    )
    return {
        int(row['member_id']): (int(row['wins'] or 0), int(row['losses'] or 0))
        for row in query
    }


def _recent_counts(member_ids: tuple[int, ...], request):
    if not member_ids:
        return {}
    cutoff = request.as_of - datetime.timedelta(days=RECENT_ACTIVITY_DAYS)
    query = (
        models.Lineup
        .select(
            models.Player.discord_member.alias('member_id'),
            fn.COUNT(models.Lineup.id).alias('recent_games'),
        )
        .join(models.Player)
        .switch(models.Lineup)
        .join(models.Game)
        .where(
            (models.Player.discord_member.in_(member_ids))
            & (
                (models.Game.date > cutoff)
                | (models.Game.completed_ts > cutoff)
            )
        )
        .group_by(models.Player.discord_member)
        .dicts()
    )
    return {
        int(row['member_id']): int(row['recent_games'] or 0)
        for row in query
    }


def _evaluate(member, *, wins: int, losses: int, recent_games: int):
    reason = 'eligible_positive_record'
    eligible = False
    if wins < MINIMUM_WINS:
        reason = 'insufficient_wins'
    elif recent_games < MINIMUM_RECENT_GAMES:
        reason = 'no_recent_games'
    elif int(member.elo_max_moonrise) > HIGH_ELO_QUALIFICATION:
        reason = 'eligible_high_elo'
        eligible = True
    elif wins > losses:
        eligible = True
    else:
        reason = 'insufficient_elo_or_record'
    if eligible and not (member.polytopia_id or member.polytopia_name):
        eligible = False
        reason = 'missing_polytopia_identity'
    return LeagueInvitationEvaluation(
        member_id=int(member.id),
        discord_id=int(member.discord_id),
        name=str(member.name),
        wins=int(wins),
        losses=int(losses),
        recent_games=int(recent_games),
        elo_max_moonrise=int(member.elo_max_moonrise),
        eligible=eligible,
        reason=reason,
    )


def load_invitation_eligibility(
    request: LeagueInvitationEligibilityRequest,
) -> LeagueInvitationBatch:
    """Evaluate one deterministic page using three bounded database queries."""

    limit = _validated_limit(request.limit)
    if int(request.polychampions_guild_id) <= 0:
        raise LeagueInvitationValidationError(
            'A valid PolyChampions guild ID is required.'
        )
    with models.db.connection_context():
        candidates = _candidate_members(request)
        truncated = len(candidates) > limit
        selected = candidates[:limit]
        member_ids = tuple(int(member.id) for member in selected)
        records = _record_counts(member_ids, request)
        recent = _recent_counts(member_ids, request)
        evaluations = tuple(
            _evaluate(
                member,
                wins=records.get(int(member.id), (0, 0))[0],
                losses=records.get(int(member.id), (0, 0))[1],
                recent_games=recent.get(int(member.id), 0),
            )
            for member in selected
        )
    return LeagueInvitationBatch(
        evaluations=evaluations,
        scanned_count=len(selected),
        truncated=truncated,
        next_after_member_id=(
            int(selected[-1].id) if truncated and selected else None
        ),
    )


def _update_delivery(request: LeagueInvitationDeliveryRequest) -> int:
    return int(
        models.DiscordMember
        .update(date_polychamps_invite_sent=request.sent_on)
        .where(
            (models.DiscordMember.id == int(request.member_id))
            & (models.DiscordMember.discord_id == int(request.discord_id))
            & models.DiscordMember.date_polychamps_invite_sent.is_null(True)
        )
        .execute()
    )


def _delivery_member(request: LeagueInvitationDeliveryRequest):
    return models.DiscordMember.get_or_none(
        (models.DiscordMember.id == int(request.member_id))
        & (models.DiscordMember.discord_id == int(request.discord_id))
    )


def record_invitation_delivery(
    request: LeagueInvitationDeliveryRequest,
) -> LeagueInvitationDeliveryResult:
    """Idempotently record one successful Discord DM in its own transaction."""

    if int(request.member_id) <= 0 or int(request.discord_id) <= 0:
        raise LeagueInvitationValidationError(
            'A valid invitation member identity is required.'
        )
    if not isinstance(request.sent_on, datetime.date):
        raise LeagueInvitationValidationError(
            'A valid invitation delivery date is required.'
        )
    with models.db.connection_context():
        with models.db.atomic():
            updated = _update_delivery(request)
            if updated == 1:
                recorded = True
            elif updated == 0:
                member = _delivery_member(request)
                if member is None:
                    raise LeagueInvitationConflictError(
                        'The invitation member changed before delivery was recorded.'
                    )
                if member.date_polychamps_invite_sent is None:
                    raise LeagueInvitationConflictError(
                        'The invitation delivery could not be recorded safely.'
                    )
                recorded = False
            else:
                raise LeagueInvitationConflictError(
                    'More than one invitation member matched the delivery.'
                )
    return LeagueInvitationDeliveryResult(
        member_id=int(request.member_id),
        discord_id=int(request.discord_id),
        sent_on=request.sent_on,
        recorded=recorded,
    )


_invitation_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-league-invitation',
)


async def _run_worker(function, request):
    future = _invitation_executor.submit(function, request)
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = future.result()
    if cancellation is not None:
        raise cancellation
    return result


async def run_load_invitation_eligibility(request):
    return await _run_worker(load_invitation_eligibility, request)


async def run_record_invitation_delivery(request):
    return await _run_worker(record_invitation_delivery, request)
