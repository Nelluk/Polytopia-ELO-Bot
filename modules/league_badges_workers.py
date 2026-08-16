"""Bounded atomic workers for guild-local PolyChampions player badges."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import logging

import peewee

from modules import models
import settings


MAX_TARGETS = 25
MAX_BADGES_PER_PLAYER = 100
MAX_AUTOCOMPLETE_CHOICES = 25
MAX_AUTOCOMPLETE_PLAYERS = 5000
logger = logging.getLogger('polybot.' + __name__)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='polybot-badges')


class BadgeError(RuntimeError):
    """Base error for a rejected badge operation."""


class BadgePermissionError(BadgeError):
    pass


class BadgeValidationError(BadgeError):
    def __init__(self, message: str, *, invalid_recipient_ids=()):
        super().__init__(message)
        self.invalid_recipient_ids = tuple(int(v) for v in invalid_recipient_ids)


@dataclass(frozen=True)
class BadgeMutationRequest:
    operation: str
    guild_id: int
    actor_discord_id: int
    actor_display_label: str
    recipient_discord_ids: tuple[int, ...]
    badge: str


@dataclass(frozen=True)
class BadgeRecipientResult:
    discord_id: int
    display_label: str
    changed: bool


@dataclass(frozen=True)
class BadgeMutationResult:
    operation: str
    guild_id: int
    actor_discord_id: int
    actor_display_label: str
    badge: str
    recipients: tuple[BadgeRecipientResult, ...]
    audit_written: bool

    @property
    def changed_count(self) -> int:
        return sum(value.changed for value in self.recipients)

    @property
    def unchanged_count(self) -> int:
        return len(self.recipients) - self.changed_count


def _allowed_guild_id() -> int:
    try:
        return int(settings.server_ids['polychampions'])
    except (KeyError, TypeError, ValueError) as exc:
        raise BadgePermissionError(
            'The PolyChampions guild is not configured.'
        ) from exc


def _validate_request(request: BadgeMutationRequest) -> None:
    if request.operation not in {'add', 'remove'}:
        raise BadgeValidationError('Badge operation must be add or remove.')
    if int(request.guild_id) != _allowed_guild_id():
        raise BadgePermissionError(
            'Badges are available only in the configured PolyChampions guild.'
        )
    target_ids = tuple(int(value) for value in request.recipient_discord_ids)
    if not 1 <= len(target_ids) <= MAX_TARGETS:
        raise BadgeValidationError('Select between 1 and 25 recipients.')
    if len(set(target_ids)) != len(target_ids) or any(v <= 0 for v in target_ids):
        raise BadgeValidationError(
            'Badge recipients must be ordered unique positive Discord IDs.'
        )
    if (
        not request.badge
        or len(request.badge) > 200
        or any(character in '\r\n' for character in request.badge)
    ):
        raise BadgeValidationError('The normalized badge is invalid.')
    if (
        len(request.actor_display_label) > 200
        or any(character in '\r\n' for character in request.actor_display_label)
    ):
        raise BadgeValidationError('The captured actor label is too long.')


def _target_rows(request: BadgeMutationRequest) -> tuple:
    target_ids = tuple(int(value) for value in request.recipient_discord_ids)
    query = (
        models.Player.select(models.Player, models.DiscordMember)
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == int(request.guild_id))
            & (models.DiscordMember.discord_id.in_(target_ids))
        )
        .order_by(models.Player.id)
    )
    if isinstance(models.db, peewee.PostgresqlDatabase):
        query = query.for_update()
    rows = tuple(query)
    by_discord_id: dict[int, list] = {}
    for player in rows:
        by_discord_id.setdefault(
            int(player.discord_member.discord_id), []
        ).append(player)
    invalid = tuple(
        discord_id for discord_id in target_ids
        if len(by_discord_id.get(discord_id, ())) != 1
    )
    if invalid:
        raise BadgeValidationError(
            'Every recipient must resolve to exactly one registered Player '
            'in this guild.',
            invalid_recipient_ids=invalid,
        )
    return tuple(by_discord_id[discord_id][0] for discord_id in target_ids)


def _validated_badges(player) -> list[str]:
    badges = player.badges
    if not isinstance(badges, (list, tuple)):
        raise BadgeValidationError(
            f'Player {int(player.id)} has a malformed badge array.'
        )
    values = list(badges)
    if (
        len(values) > MAX_BADGES_PER_PLAYER
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > 200
            or any(character in '\r\n' for character in value)
            for value in values
        )
    ):
        raise BadgeValidationError(
            f'Player {int(player.id)} has an invalid stored badge array.'
        )
    keys = [value.casefold() for value in values]
    if len(keys) != len(set(keys)):
        raise BadgeValidationError(
            f'Player {int(player.id)} has duplicate stored badges.'
        )
    return values


def mutate_badges(request: BadgeMutationRequest) -> BadgeMutationResult:
    """Lock, compute, save, and audit one complete batch atomically."""

    with models.db.connection_context():
        with models.db.atomic():
            _validate_request(request)
            players = _target_rows(request)
            changes = []
            key = request.badge.casefold()
            for player in players:
                current = _validated_badges(player)
                matches = [i for i, value in enumerate(current) if value.casefold() == key]
                if request.operation == 'add':
                    changed = not matches
                    updated = current + [request.badge] if changed else current
                    if len(updated) > MAX_BADGES_PER_PLAYER:
                        raise BadgeValidationError(
                            f'Player {int(player.id)} would exceed the '
                            f'{MAX_BADGES_PER_PLAYER}-badge limit.'
                        )
                else:
                    changed = bool(matches)
                    updated = [
                        value for value in current if value.casefold() != key
                    ]
                changes.append((player, updated, changed))

            for player, updated, changed in changes:
                if changed:
                    player.badges = updated
                    player.save(only=[models.Player.badges])

            changed_ids = tuple(
                int(player.id) for player, _updated, changed in changes if changed
            )
            if changed_ids:
                models.GameLog.write(
                    guild_id=int(request.guild_id),
                    message=(
                        f'{request.actor_display_label} ({request.actor_discord_id}) '
                        f'{request.operation} player badge {request.badge!r}; '
                        f'guild {request.guild_id}; Player IDs '
                        + ','.join(str(value) for value in changed_ids)
                        + '. (/league badge)'
                    )[:1900],
                )

            recipients = tuple(
                BadgeRecipientResult(
                    discord_id=int(player.discord_member.discord_id),
                    display_label=str(player.name or player.discord_member.name),
                    changed=changed,
                )
                for player, _updated, changed in changes
            )
            return BadgeMutationResult(
                operation=request.operation,
                guild_id=int(request.guild_id),
                actor_discord_id=int(request.actor_discord_id),
                actor_display_label=str(request.actor_display_label),
                badge=str(request.badge),
                recipients=recipients,
                audit_written=bool(changed_ids),
            )


def autocomplete_badges(guild_id: int, query: str) -> tuple[str, ...]:
    if int(guild_id) != _allowed_guild_id():
        return ()
    needle = str(query or '').casefold()
    with models.db.connection_context():
        rows = tuple(
            models.Player.select(models.Player.badges)
            .where(models.Player.guild_id == int(guild_id))
            .order_by(models.Player.id)
            .limit(MAX_AUTOCOMPLETE_PLAYERS + 1)
        )
        if len(rows) > MAX_AUTOCOMPLETE_PLAYERS:
            raise BadgeValidationError(
                'Badge autocomplete exceeded its bounded player scan.'
            )
        values = []
        seen = set()
        for player in rows:
            for badge in _validated_badges(player):
                key = badge.casefold()
                if key in seen or needle not in key:
                    continue
                seen.add(key)
                values.append(badge)
                if len(values) == MAX_AUTOCOMPLETE_CHOICES:
                    return tuple(values)
        return tuple(values)


async def _run(call):
    future = _executor.submit(call)
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            future.result()
        except BaseException:
            logger.exception('Cancelled badge worker completed with an error')
        raise asyncio.CancelledError
    return future.result()


async def run_badge_mutation(request: BadgeMutationRequest) -> BadgeMutationResult:
    return await _run(functools.partial(mutate_badges, request))


async def run_badge_autocomplete(guild_id: int, query: str) -> tuple[str, ...]:
    return await _run(functools.partial(autocomplete_badges, guild_id, query))
