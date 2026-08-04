"""Bounded worker-local writes for canonical player registration."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import re
import unicodedata

import discord

from modules import models
import settings


logger = logging.getLogger('polybot.' + __name__)

MAX_NAME_LENGTH = 200

_PLACEHOLDER_NAMES = frozenset({
    'none',
    'null',
    'n/a',
    'na',
    'your name',
    'your mobile name',
    'your steam name',
    'your game name',
    'your in game name',
    'your in-game name',
    'mobile name here',
    'polytopia name',
})

_player_write_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-player-write',
)


class PlayerRegistrationValidationError(ValueError):
    """The submitted canonical name is not a usable account name."""


class PlayerRegistrationPermissionError(PermissionError):
    """The actor is not allowed to register the requested target."""


@dataclass(frozen=True)
class MemberSnapshot:
    """Primitive Discord identity captured before worker submission."""

    discord_id: int
    discord_name: str
    discord_nick: str | None
    display_name: str
    role_names: tuple[str, ...]

    @property
    def description(self) -> str:
        safe_name = discord.utils.escape_markdown(
            safe_public_name(self.display_name),
            as_needed=True,
        )
        return f'**{safe_name}** (`{self.discord_id}`)'


@dataclass(frozen=True)
class PlayerRegistrationRequest:
    """Immutable, Discord/Peewee-free input to the registration worker."""

    guild_id: int
    requester_id: int
    actor: MemberSnapshot
    target: MemberSnapshot
    canonical_name: str
    requester_is_staff: bool
    invoked_with: str


@dataclass(frozen=True)
class PlayerRegistrationResult:
    """Committed registration result suitable for post-commit presentation."""

    guild_id: int
    requester_id: int
    target_id: int
    canonical_name: str
    player_created: bool
    member_created: bool
    team_name: str | None
    duplicate_count: int
    warnings: tuple[str, ...]


def validate_canonical_name(value: str | None) -> str:
    """Trim, bound, and validate one account-wide Polytopia name."""

    value = str(value or '').strip()[:MAX_NAME_LENGTH]
    if not value:
        raise PlayerRegistrationValidationError(
            'A canonical Polytopia name is required.'
        )
    if any(unicodedata.category(character).startswith('C') for character in value):
        raise PlayerRegistrationValidationError(
            'The canonical Polytopia name cannot contain control characters, '
            'newlines, or tabs.'
        )

    folded = ' '.join(value.casefold().split())
    if folded in _PLACEHOLDER_NAMES or (
        'your' in folded
        and 'name' in folded
        and ('game' in folded or 'mobile' in folded or 'steam' in folded)
    ):
        raise PlayerRegistrationValidationError(
            'That looks like a placeholder. Enter your actual Polytopia name.'
        )
    return value


def _snapshot_staff(request: PlayerRegistrationRequest) -> bool:
    """Recheck staff parity from immutable role snapshots in the worker."""

    if request.requester_id == settings.owner_id:
        return True
    try:
        helper_roles = settings.guild_setting(request.guild_id, 'helper_roles')
        mod_roles = settings.guild_setting(request.guild_id, 'mod_roles')
    except Exception:
        # A settings failure cannot authorize a staff-targeted write. The
        # event-loop check remains useful for immediate UX, but the worker
        # fails closed at the authoritative boundary.
        return False
    configured_roles = {str(role) for role in (*helper_roles, *mod_roles)}
    return bool(configured_roles.intersection(request.actor.role_names))


def _ensure_request_is_allowed(request: PlayerRegistrationRequest) -> None:
    if request.guild_id <= 0:
        raise PlayerRegistrationValidationError(
            'Registration requires a valid Discord server.'
        )
    if request.requester_id != request.actor.discord_id:
        raise PlayerRegistrationPermissionError(
            'The registration actor snapshot is inconsistent.'
        )
    if request.target.discord_id != request.requester_id and not _snapshot_staff(
        request
    ):
        raise PlayerRegistrationPermissionError(
            'Only server staff can register another member.'
        )
    # Modal limits and prefix callers both use this function, but the worker
    # is the final boundary before a database write.
    validate_canonical_name(request.canonical_name)


def _matching_team(guild_id: int, role_names: tuple[str, ...]):
    if not role_names:
        return ()
    return tuple(
        models.Team.select().where(
            (models.Team.guild_id == guild_id)
            & models.Team.name.in_(tuple(role_names))
        )
    )


def _duplicate_count(guild_id: int, target_id: int, canonical_name: str) -> int:
    """Count duplicate account names without adding a uniqueness constraint."""

    del guild_id  # Canonical names are intentionally account-wide.
    return models.DiscordMember.select().where(
        (models.DiscordMember.discord_id != target_id)
        & models.DiscordMember.polytopia_name.is_null(False)
        & (
            models.fn.LOWER(models.DiscordMember.polytopia_name)
            == canonical_name.lower()
        )
    ).count()


def safe_public_name(value: str) -> str:
    """Escape everyone/here and user/role mention syntax for presentation."""

    escaped = discord.utils.escape_mentions(value)
    return re.sub(
        r'<@(?=[!&]?\d+>)',
        '<@\u200b',
        escaped,
    )


def _safe_name(value: str) -> str:
    return discord.utils.escape_markdown(
        safe_public_name(value),
        as_needed=True,
    )


def register_player(request: PlayerRegistrationRequest) -> PlayerRegistrationResult:
    """Commit member, guild player, canonical name, team, and audit together."""

    _ensure_request_is_allowed(request)
    canonical_name = validate_canonical_name(request.canonical_name)
    target = request.target

    with models.db.connection_context():
        with models.db.atomic():
            member = models.DiscordMember.get_or_none(
                discord_id=target.discord_id,
            )
            member_created = member is None
            if member is None:
                member = models.DiscordMember.create(
                    discord_id=target.discord_id,
                    name=target.discord_name,
                )
            else:
                # Keep the account-wide legacy fields untouched. The current
                # Discord username is only metadata used by old displays.
                member.name = target.discord_name
                member.save(only=[models.DiscordMember.name])

            display_name = models.Player.generate_display_name(
                player_name=target.discord_name,
                player_nick=target.discord_nick,
            )
            player = models.Player.get_or_none(
                (models.Player.discord_member == member)
                & (models.Player.guild_id == request.guild_id)
            )
            player_created = player is None
            if player is None:
                player = models.Player.create(
                    discord_member=member,
                    guild_id=request.guild_id,
                    nick=target.discord_nick,
                    name=display_name,
                )

            matching_teams = _matching_team(
                request.guild_id,
                target.role_names,
            )
            warnings = []
            if len(matching_teams) == 1:
                player.team = matching_teams[0]
            elif len(matching_teams) > 1:
                warnings.append(
                    'Multiple persisted team roles matched; the existing team '
                    'assignment was retained.'
                )
            player.name = display_name
            player.nick = target.discord_nick
            player.save()

            # Only the canonical account-wide field is written by this unit.
            # In particular, do not clear/backfill name_steam or polytopia_id.
            member.polytopia_name = canonical_name
            member.save(only=[models.DiscordMember.polytopia_name])

            duplicate_count = _duplicate_count(
                request.guild_id,
                target.discord_id,
                canonical_name,
            )
            if duplicate_count:
                warnings.append(
                    f'{duplicate_count} other account(s) already use this '
                    'canonical Polytopia name.'
                )

            audit_message = (
                f'{request.actor.description} registered '
                f'{request.target.description} account-wide Polytopia name '
                f'to `{_safe_name(canonical_name)}`'
            )
            if duplicate_count:
                audit_message += (
                    f' [duplicate warning: {duplicate_count} other account(s)]'
                )
            models.GameLog.write(
                message=audit_message,
                guild_id=request.guild_id,
                game_id=0,
            )

            team_name = (
                str(player.team.name) if player.team is not None else None
            )

    return PlayerRegistrationResult(
        guild_id=request.guild_id,
        requester_id=request.requester_id,
        target_id=target.discord_id,
        canonical_name=canonical_name,
        player_created=player_created,
        member_created=member_created,
        team_name=team_name,
        duplicate_count=duplicate_count,
        warnings=tuple(warnings),
    )


async def run_player_registration(
    request: PlayerRegistrationRequest,
) -> PlayerRegistrationResult:
    """Submit one ordinary write and drain a canceled caller safely."""

    concurrent_future = _player_write_executor.submit(
        functools.partial(register_player, request),
    )
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.01)
        return concurrent_future.result()
    except asyncio.CancelledError:
        # A synchronous transaction cannot be interrupted safely. Drain the
        # worker before allowing the caller task to finish cancellation.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        while not concurrent_future.done():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                if task is not None:
                    task.uncancel()
        concurrent_future.result()
        raise asyncio.CancelledError
