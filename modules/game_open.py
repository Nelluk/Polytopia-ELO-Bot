"""Shared parsing and post-commit presentation helpers for open games."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

import settings
from modules import game_open_workers


logger = logging.getLogger('polybot.' + __name__)


async def publish_open_game_result(
    result: game_open_workers.OpenGameResult,
    *,
    prefix: str,
    send: Callable[[str], Awaitable[object]],
    broadcast: Callable[[], Awaitable[None]] | None = None,
    add_completion_reaction: Callable[[object], Awaitable[None]] | None = None,
) -> None:
    """Publish warnings/result only after the worker transaction commits."""

    for warning in result.warnings:
        await _send_with_reconciliation(
            send,
            warning,
            result.game_id,
            'open-game warning',
        )

    completion = (
        f'Starting new {"__Steam__ " if not result.is_mobile else ""}'
        f'{"unranked " if not result.is_ranked else ""}open game ID '
        f'{result.game_id}. Size: {result.size_string}. Expiration: '
        f'{result.expiration_hours} hours.\nNotes: *{result.notes_display}*\n'
        f'Other players can join this game with `{prefix}join '
        f'{result.game_id}` or join game {result.game_id} by reacting with '
        f'{settings.emoji_join_game}.')
    sent_completion = await _send_with_reconciliation(
        send,
        completion,
        result.game_id,
        'open-game completion',
    )
    if sent_completion is not None and add_completion_reaction is not None:
        try:
            await add_completion_reaction(sent_completion)
        except Exception:
            logger.exception(
                'Discord join reaction failed for committed open game %s; '
                'operator reconciliation is required',
                result.game_id,
            )
            await _send_with_reconciliation(
                send,
                f':warning: Game {result.game_id} was created, but the '
                f'{settings.emoji_join_game} join reaction could not be '
                'added. An operator must reconcile the announcement for '
                f'game {result.game_id}.',
                result.game_id,
                'open-game join-reaction reconciliation',
            )

    if broadcast is not None and any(
        side.required_role_id for side in result.role_locks
    ):
        try:
            await broadcast()
        except Exception:
            logger.exception(
                'Committed open game %s needs Discord broadcast '
                'reconciliation',
                result.game_id,
            )
            await _send_with_reconciliation(
                send,
                f':warning: Game {result.game_id} was created, but its '
                'team-server broadcast could not be completed. An operator '
                'must reconcile the announcement.',
                result.game_id,
                'open-game broadcast reconciliation',
            )


async def _send_with_reconciliation(
    send: Callable[[str], Awaitable[object]],
    message: str,
    game_id: int,
    effect_name: str,
) -> object | None:
    """Keep committed state visible when a Discord send fails."""

    try:
        return await send(message)
    except Exception:
        logger.exception(
            'Discord %s failed for committed open game %s; operator '
            'reconciliation is required',
            effect_name,
            game_id,
        )
        return None
