"""Configuration and orchestration for automatic vacant lobbies."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
import logging
from typing import Mapping, Sequence

import discord

from modules import game_lobby_workers


logger = logging.getLogger('polybot.' + __name__)

MAX_CONFIGURED_LOBBIES = 100


@dataclass(frozen=True)
class LobbyCycleResult:
    """Primitive summary of one configured-lobby maintenance cycle."""

    outcomes: tuple[game_lobby_workers.EnsureLobbyResult, ...]
    skipped_indexes: tuple[int, ...]
    truncated: bool


def _freeze_request(
    lobby: Mapping[str, object],
    *,
    guild,
    as_of: datetime.datetime,
) -> game_lobby_workers.EnsureLobbyRequest:
    sizes = tuple(int(size) for size in lobby['size'])
    configured_locks = tuple(lobby.get('role_locks', (None,) * len(sizes)))
    if len(configured_locks) != len(sizes):
        raise ValueError('role_locks must contain one entry for every side')

    role_locks = []
    for role_id_value in configured_locks:
        if role_id_value is None:
            role_locks.append(game_lobby_workers.LobbySideLock(None, None))
            continue
        role_id = int(role_id_value)
        role = guild.get_role(role_id)
        if role is None:
            logger.warning(
                'Configured vacant lobby role %s was not found in guild %s '
                '(%s); creating that side without a role lock.',
                role_id,
                guild.id,
                guild.name,
            )
            role_locks.append(game_lobby_workers.LobbySideLock(None, None))
            continue
        role_locks.append(game_lobby_workers.LobbySideLock(
            role_id=int(role.id),
            role_name=str(role.name),
        ))

    expiration_hours = int(lobby.get('exp', 30))
    if expiration_hours < 1:
        raise ValueError('configured lobby expiration must be positive')
    notes = str(lobby.get('notes', ''))
    escaped_notes = discord.utils.escape_markdown(notes)
    return game_lobby_workers.EnsureLobbyRequest(
        guild_id=int(guild.id),
        size=sizes,
        size_display=str(lobby['size_str']),
        is_ranked=bool(lobby['ranked']),
        remake_partial=bool(lobby['remake_partial']),
        notes=notes,
        notes_log_display=f'*{escaped_notes}*' if notes else '',
        expiration_at=as_of + datetime.timedelta(hours=expiration_hours),
        role_locks=tuple(role_locks),
    )


async def ensure_configured_lobbies(
    *,
    bot,
    lobbies: Sequence[Mapping[str, object]],
    as_of: datetime.datetime | None = None,
) -> LobbyCycleResult:
    """Ensure each bounded configuration independently and contain failures."""

    frozen_time = as_of or datetime.datetime.now()
    configured = tuple(lobbies)
    truncated = len(configured) > MAX_CONFIGURED_LOBBIES
    if truncated:
        logger.warning(
            'Configured vacant lobby maintenance reached the %s-definition '
            'bound; later definitions are deferred to the next cycle.',
            MAX_CONFIGURED_LOBBIES,
        )
    outcomes = []
    skipped = []
    for index, lobby in enumerate(configured[:MAX_CONFIGURED_LOBBIES]):
        try:
            guild_id = int(lobby['guild'])
            guild = bot.get_guild(guild_id)
            if guild is None:
                logger.warning(
                    'Bot is not a member of configured vacant lobby guild %s.',
                    guild_id,
                )
                skipped.append(index)
                continue
            request = _freeze_request(lobby, guild=guild, as_of=frozen_time)
            result = await game_lobby_workers.run_ensure_configured_lobby(
                request,
            )
            outcomes.append(result)
            if result.status == game_lobby_workers.CREATED:
                logger.info(
                    'Created configured vacant lobby game %s in guild %s.',
                    result.game_id,
                    result.guild_id,
                )
        except Exception:
            logger.exception(
                'Configured vacant lobby definition %s failed; later '
                'definitions will still be processed.',
                index,
            )
            skipped.append(index)
    return LobbyCycleResult(
        outcomes=tuple(outcomes),
        skipped_indexes=tuple(skipped),
        truncated=truncated,
    )
