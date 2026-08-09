"""Bounded database workers for league inactivity maintenance."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import datetime
import threading

import peewee

from modules import models


ACTIVITY_DAYS = 60
MAX_ACTION_CANDIDATES = 100
MAX_GUILD_MEMBER_SNAPSHOTS = 10_000


class LeagueInactivityError(RuntimeError):
    """Base user-facing inactivity-maintenance error."""


class LeagueInactivityPermissionError(LeagueInactivityError):
    """The requester is not allowed to run inactivity maintenance."""


class LeagueInactivityBusyError(LeagueInactivityError):
    """Another inactivity selection currently owns the bounded worker."""


@dataclass(frozen=True)
class InactivityMemberSnapshot:
    member_id: int
    display_name: str
    joined_timestamp: float | None
    role_ids: tuple[int, ...]
    role_names: tuple[str, ...]
    is_bot: bool
    is_owner: bool


@dataclass(frozen=True)
class InactivityPreviewRequest:
    guild_id: int
    requester_id: int
    requester_is_mod: bool
    league_scope: bool
    now_timestamp: float
    inactive_role_id: int
    inactive_role_name: str
    protected_role_names: tuple[str, ...]
    missing_protected_role_names: tuple[str, ...]
    members: tuple[InactivityMemberSnapshot, ...]


@dataclass(frozen=True)
class InactivityCandidate:
    member_id: int
    display_name: str
    joined_days: int
    role_names: tuple[str, ...]


@dataclass(frozen=True)
class InactivityPreviewResult:
    guild_id: int
    requester_id: int
    generated_timestamp: float
    inactive_role_id: int
    inactive_role_name: str
    protected_role_names: tuple[str, ...]
    missing_protected_role_names: tuple[str, ...]
    candidates: tuple[InactivityCandidate, ...]
    active_count: int
    recent_join_count: int
    already_inactive_count: int
    protected_count: int
    omitted_count: int
    total_member_count: int

    @property
    def candidate_ids(self) -> tuple[int, ...]:
        return tuple(row.member_id for row in self.candidates)

    @property
    def action_candidates(self) -> tuple[InactivityCandidate, ...]:
        return self.candidates[:MAX_ACTION_CANDIDATES]

    @property
    def deferred_candidate_count(self) -> int:
        return max(0, len(self.candidates) - MAX_ACTION_CANDIDATES)


@dataclass(frozen=True)
class InactiveRoleAuditRequest:
    guild_id: int
    member_id: int
    role_name: str
    applied: bool


_selection_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='league-inactivity-selection',
)
_audit_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='league-inactivity-audit',
)
_selection_claim = threading.Lock()


def _active_member_ids(request: InactivityPreviewRequest) -> set[int]:
    member_ids = tuple(member.member_id for member in request.members)
    if not member_ids:
        return set()
    cutoff_date = (
        datetime.datetime.fromtimestamp(
            request.now_timestamp,
            tz=datetime.timezone.utc,
        ).date()
        - datetime.timedelta(days=ACTIVITY_DAYS)
    )
    query = (
        models.Player
        .select(models.DiscordMember.discord_id)
        .join(models.Lineup)
        .join(models.Game)
        .join_from(models.Player, models.DiscordMember)
        .where(
            (models.Game.guild_id == int(request.guild_id))
            & (models.DiscordMember.discord_id.in_(member_ids))
            & (
                (models.Game.date > cutoff_date)
                | (models.Game.is_completed == False)
            )
        )
        .distinct()
    )
    return {int(row[0]) for row in query.tuples()}


def _load_preview(request: InactivityPreviewRequest) -> InactivityPreviewResult:
    if not request.league_scope or not request.requester_is_mod:
        raise LeagueInactivityPermissionError(
            'Marking inactive members requires Mod access in the configured '
            'league server.'
        )
    if request.guild_id <= 0 or request.requester_id <= 0:
        raise LeagueInactivityPermissionError(
            'The server and requester must be valid.'
        )
    if request.inactive_role_id <= 0 or not request.inactive_role_name:
        raise LeagueInactivityError(
            'The configured Inactive role could not be resolved.'
        )
    if len(request.members) > MAX_GUILD_MEMBER_SNAPSHOTS:
        raise LeagueInactivityError(
            f'This server has more than the safe '
            f'{MAX_GUILD_MEMBER_SNAPSHOTS:,}-member preview limit.'
        )

    with models.db.connection_context():
        active_ids = _active_member_ids(request)

    cutoff_timestamp = request.now_timestamp - (
        ACTIVITY_DAYS * 24 * 60 * 60
    )
    protected_names = set(request.protected_role_names)
    candidates: list[InactivityCandidate] = []
    active_count = 0
    recent_join_count = 0
    already_inactive_count = 0
    protected_count = 0
    omitted_count = 0

    for member in request.members:
        if member.is_bot or member.is_owner or member.joined_timestamp is None:
            omitted_count += 1
            continue
        if request.inactive_role_id in member.role_ids:
            already_inactive_count += 1
            continue
        if protected_names.intersection(member.role_names):
            protected_count += 1
            continue
        if member.joined_timestamp > cutoff_timestamp:
            recent_join_count += 1
            continue
        if member.member_id in active_ids:
            active_count += 1
            continue
        joined_days = max(
            0,
            int((request.now_timestamp - member.joined_timestamp) // 86400),
        )
        candidates.append(InactivityCandidate(
            member_id=int(member.member_id),
            display_name=str(member.display_name),
            joined_days=joined_days,
            role_names=tuple(member.role_names),
        ))

    candidates.sort(key=lambda row: (-row.joined_days, row.member_id))
    return InactivityPreviewResult(
        guild_id=int(request.guild_id),
        requester_id=int(request.requester_id),
        generated_timestamp=float(request.now_timestamp),
        inactive_role_id=int(request.inactive_role_id),
        inactive_role_name=str(request.inactive_role_name),
        protected_role_names=tuple(request.protected_role_names),
        missing_protected_role_names=tuple(
            request.missing_protected_role_names
        ),
        candidates=tuple(candidates),
        active_count=active_count,
        recent_join_count=recent_join_count,
        already_inactive_count=already_inactive_count,
        protected_count=protected_count,
        omitted_count=omitted_count,
        total_member_count=len(request.members),
    )


def _write_role_audit(request: InactiveRoleAuditRequest) -> int | None:
    with models.db.connection_context():
        try:
            discord_member = (
                models.DiscordMember
                .select(models.DiscordMember)
                .join(models.Player)
                .where(
                    (models.DiscordMember.discord_id == int(request.member_id))
                    & (models.Player.guild_id == int(request.guild_id))
                )
                .get()
            )
        except peewee.DoesNotExist:
            return None
        action = 'applied to' if request.applied else 'removed from'
        with models.db.atomic():
            log = models.GameLog.write(
                game_id=0,
                guild_id=int(request.guild_id),
                message=(
                    f'{models.GameLog.member_string(discord_member)} had '
                    f'*{request.role_name}* role {action} them.'
                ),
            )
    return int(log.id)


async def _drain_future(future: Future):
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            future.result()
        except BaseException as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError
    return future.result()


async def run_inactivity_preview(
    request: InactivityPreviewRequest,
) -> InactivityPreviewResult:
    if not _selection_claim.acquire(blocking=False):
        raise LeagueInactivityBusyError(
            'Another inactivity preview is loading. Try again after it finishes.'
        )

    def work():
        try:
            return _load_preview(request)
        finally:
            _selection_claim.release()

    try:
        future = _selection_executor.submit(work)
    except BaseException:
        _selection_claim.release()
        raise
    return await _drain_future(future)


async def record_inactive_role_change(
    request: InactiveRoleAuditRequest,
) -> int | None:
    future = _audit_executor.submit(_write_role_audit, request)
    result = await _drain_future(future)
    return int(result) if result is not None else None
