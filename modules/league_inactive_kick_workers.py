"""Bounded selection and audit workers for inactive-member removal."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import datetime
import threading

from modules import models


UNREGISTERED_JOIN_DAYS = 7
REGISTERED_JOIN_DAYS = 30
TRACKED_GAME_DAYS = 60
MAX_ACTION_CANDIDATES = 25
MAX_MEMBER_SNAPSHOTS = 10_000


class InactiveKickError(RuntimeError):
    """Base user-facing inactive-removal error."""


class InactiveKickPermissionError(InactiveKickError):
    """The requester cannot use inactive-member removal."""


class InactiveKickBusyError(InactiveKickError):
    """Another selection or execution currently owns the workflow."""


@dataclass(frozen=True)
class KickRoleSnapshot:
    role_id: int
    name: str
    managed: bool


@dataclass(frozen=True)
class KickMemberSnapshot:
    member_id: int
    display_name: str
    joined_timestamp: float | None
    roles: tuple[KickRoleSnapshot, ...]
    is_bot: bool
    is_owner: bool


@dataclass(frozen=True)
class InactiveKickPreviewRequest:
    guild_id: int
    requester_id: int
    requester_is_mod: bool
    league_scope: bool
    now_timestamp: float
    inactive_role_id: int
    inactive_role_name: str
    starter_role_names: tuple[str, ...]
    protected_role_names: tuple[str, ...]
    members: tuple[KickMemberSnapshot, ...]


@dataclass(frozen=True)
class InactiveKickDecision:
    member_id: int
    display_name: str
    joined_days: int | None
    eligible: bool
    reason: str
    has_team_role: bool


@dataclass(frozen=True)
class InactiveKickPreviewResult:
    guild_id: int
    requester_id: int
    generated_timestamp: float
    inactive_role_id: int
    inactive_role_name: str
    starter_role_names: tuple[str, ...]
    protected_role_names: tuple[str, ...]
    team_role_names: tuple[str, ...]
    decisions: tuple[InactiveKickDecision, ...]

    @property
    def candidates(self) -> tuple[InactiveKickDecision, ...]:
        return tuple(row for row in self.decisions if row.eligible)

    @property
    def candidate_ids(self) -> tuple[int, ...]:
        return tuple(row.member_id for row in self.candidates)

    @property
    def action_candidates(self) -> tuple[InactiveKickDecision, ...]:
        return self.candidates[:MAX_ACTION_CANDIDATES]

    @property
    def action_candidate_ids(self) -> tuple[int, ...]:
        return tuple(row.member_id for row in self.action_candidates)

    @property
    def deferred_candidate_count(self) -> int:
        return max(0, len(self.candidates) - MAX_ACTION_CANDIDATES)

    @property
    def exclusion_count(self) -> int:
        return len(self.decisions) - len(self.candidates)

    @property
    def confirmation_text(self) -> str:
        return f'KICK {len(self.action_candidates)}'


@dataclass(frozen=True)
class KickAuditRow:
    member_id: int
    display_name: str


@dataclass(frozen=True)
class InactiveKickAuditRequest:
    guild_id: int
    actor_id: int
    actor_description: str
    rows: tuple[KickAuditRow, ...]


@dataclass(frozen=True)
class InactiveKickAuditResult:
    log_ids: tuple[int, ...]


_selection_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='league-inactive-kick-selection',
)
_audit_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='league-inactive-kick-audit',
)
_selection_claim = threading.Lock()
_execution_claim = threading.Lock()


def claim_execution() -> bool:
    return _execution_claim.acquire(blocking=False)


def release_execution() -> None:
    if _execution_claim.locked():
        _execution_claim.release()


def _database_state(request: InactiveKickPreviewRequest):
    member_ids = tuple(member.member_id for member in request.members)
    if not member_ids:
        return set(), set(), set(), tuple()

    cutoff_datetime = datetime.datetime.fromtimestamp(
        request.now_timestamp,
        tz=datetime.timezone.utc,
    ) - datetime.timedelta(days=TRACKED_GAME_DAYS)
    cutoff_date = cutoff_datetime.date()

    registered_ids = {
        int(discord_id)
        for (discord_id,) in (
            models.DiscordMember
            .select(models.DiscordMember.discord_id)
            .where(models.DiscordMember.discord_id.in_(member_ids))
            .tuples()
        )
    }
    recent_ids = {
        int(discord_id)
        for (discord_id,) in (
            models.Lineup
            .select(models.DiscordMember.discord_id)
            .join(models.Player)
            .join(models.DiscordMember)
            .switch(models.Lineup)
            .join(models.Game)
            .where(
                (models.DiscordMember.discord_id.in_(member_ids))
                & (
                    (models.Game.date > cutoff_date)
                    | (models.Game.completed_ts > cutoff_datetime)
                )
            )
            .distinct()
            .tuples()
        )
    }
    blocked_ids = {
        int(discord_id)
        for (discord_id,) in (
            models.Lineup
            .select(models.DiscordMember.discord_id)
            .join(models.Player)
            .join(models.DiscordMember)
            .switch(models.Lineup)
            .join(models.Game)
            .where(
                (models.DiscordMember.discord_id.in_(member_ids))
                & (models.Game.guild_id == int(request.guild_id))
                & (
                    (models.Game.is_pending == True)
                    | (models.Game.is_completed == False)
                )
            )
            .distinct()
            .tuples()
        )
    }
    team_role_names = tuple(
        str(name)
        for (name,) in (
            models.Team
            .select(models.Team.name)
            .where(models.Team.guild_id == int(request.guild_id))
            .order_by(models.Team.name)
            .tuples()
        )
    )
    return registered_ids, recent_ids, blocked_ids, team_role_names


def _load_preview(
    request: InactiveKickPreviewRequest,
) -> InactiveKickPreviewResult:
    if not request.league_scope or not request.requester_is_mod:
        raise InactiveKickPermissionError(
            'Removing inactive members requires Mod access in the configured '
            'league server.'
        )
    if request.guild_id <= 0 or request.requester_id <= 0:
        raise InactiveKickPermissionError(
            'The server and requester must be valid.'
        )
    if request.inactive_role_id <= 0 or not request.inactive_role_name:
        raise InactiveKickError(
            'The configured Inactive role could not be resolved.'
        )
    if len(request.members) > MAX_MEMBER_SNAPSHOTS:
        raise InactiveKickError(
            f'The Inactive role has more than the safe '
            f'{MAX_MEMBER_SNAPSHOTS:,}-member preview limit.'
        )

    with models.db.connection_context():
        registered_ids, recent_ids, blocked_ids, team_role_names = (
            _database_state(request)
        )

    starter_names = set(request.starter_role_names)
    protected_names = set(request.protected_role_names)
    team_names = set(team_role_names)
    allowed_names = (
        starter_names
        | team_names
        | {request.inactive_role_name, '@everyone'}
    )
    decisions = []
    for member in request.members:
        joined_days = None
        if member.joined_timestamp is not None:
            joined_days = max(
                0,
                int(
                    (request.now_timestamp - member.joined_timestamp)
                    // 86400
                ),
            )
        role_names = {role.name for role in member.roles}
        has_team_role = bool(role_names.intersection(team_names))
        managed_roles = tuple(
            role.name
            for role in member.roles
            if role.managed and role.name != '@everyone'
        )
        unknown_roles = tuple(sorted(role_names - allowed_names))
        protected_roles = tuple(sorted(role_names.intersection(protected_names)))

        eligible = False
        if member.is_bot or member.is_owner:
            reason = 'protected bot/owner account'
        elif request.inactive_role_id not in {
            role.role_id for role in member.roles
        }:
            reason = 'Inactive role no longer assigned'
        elif managed_roles:
            reason = 'protected managed role'
        elif protected_roles:
            reason = 'protected staff/leadership role'
        elif unknown_roles:
            reason = 'protected unrecognized role'
        elif joined_days is None:
            reason = 'join date unavailable'
        elif member.member_id in blocked_ids:
            reason = 'pending or incomplete league game'
        elif member.member_id not in registered_ids:
            if joined_days < UNREGISTERED_JOIN_DAYS:
                reason = 'unregistered but joined fewer than 7 days ago'
            else:
                eligible = True
                reason = 'unregistered and joined at least 7 days ago'
        elif joined_days < REGISTERED_JOIN_DAYS:
            reason = 'registered but joined fewer than 30 days ago'
        elif member.member_id in recent_ids:
            reason = 'tracked game within the last 60 days'
        else:
            eligible = True
            reason = 'registered, joined 30+ days, no tracked game in 60 days'

        decisions.append(InactiveKickDecision(
            member_id=int(member.member_id),
            display_name=str(member.display_name),
            joined_days=joined_days,
            eligible=eligible,
            reason=reason,
            has_team_role=has_team_role,
        ))

    decisions.sort(key=lambda row: (
        not row.eligible,
        -(row.joined_days if row.joined_days is not None else -1),
        row.member_id,
    ))
    return InactiveKickPreviewResult(
        guild_id=int(request.guild_id),
        requester_id=int(request.requester_id),
        generated_timestamp=float(request.now_timestamp),
        inactive_role_id=int(request.inactive_role_id),
        inactive_role_name=str(request.inactive_role_name),
        starter_role_names=tuple(request.starter_role_names),
        protected_role_names=tuple(request.protected_role_names),
        team_role_names=team_role_names,
        decisions=tuple(decisions),
    )


def _write_kick_audit(
    request: InactiveKickAuditRequest,
) -> InactiveKickAuditResult:
    if request.guild_id <= 0 or request.actor_id <= 0:
        raise InactiveKickError('The audit guild and actor must be valid.')
    if not request.actor_description.strip():
        raise InactiveKickError('The audit actor description is required.')
    if len(request.rows) > MAX_ACTION_CANDIDATES:
        raise InactiveKickError(
            f'No more than {MAX_ACTION_CANDIDATES} removals may be audited.'
        )

    log_ids = []
    with models.db.connection_context():
        with models.db.atomic():
            for row in request.rows:
                log = models.GameLog.write(
                    game_id=0,
                    guild_id=int(request.guild_id),
                    message=(
                        f'{request.actor_description} removed '
                        f'**{row.display_name}** (`{row.member_id}`) during '
                        'confirmed inactive-member maintenance.'
                    ),
                )
                log_ids.append(int(log.id))
    return InactiveKickAuditResult(log_ids=tuple(log_ids))


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


async def run_preview(
    request: InactiveKickPreviewRequest,
) -> InactiveKickPreviewResult:
    if not _selection_claim.acquire(blocking=False):
        raise InactiveKickBusyError(
            'Another inactive-member preview is loading. Try again shortly.'
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


async def record_kicks(
    request: InactiveKickAuditRequest,
) -> InactiveKickAuditResult:
    future = _audit_executor.submit(_write_kick_audit, request)
    return await _drain_future(future)
