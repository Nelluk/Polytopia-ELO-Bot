"""Small helpers for committed interaction output lifecycle edges."""

from __future__ import annotations

import asyncio
import logging

import discord


logger = logging.getLogger('polybot.' + __name__)


def _is_already_cleared(error: BaseException) -> bool:
    """Return whether Discord says the private placeholder is already gone."""

    # ``discord.NotFound`` exposes the API error code, while lightweight test
    # doubles and a few HTTP wrappers may expose only ``code``. Error 10008 is
    # specifically the post-commit placeholder-cleanup case.
    return (
        isinstance(error, discord.NotFound)
        or getattr(error, 'code', None) == 10008
    ) and getattr(error, 'code', None) == 10008


async def _clear_private_original(interaction) -> None:
    """Best-effort deletion whose failure cannot change commit semantics."""

    delete_original = getattr(interaction, 'delete_original_response', None)
    if delete_original is None:
        return
    try:
        await delete_original()
    except Exception as exc:
        if _is_already_cleared(exc):
            logger.debug(
                'Private interaction placeholder was already cleared '
                '(Discord error 10008).'
            )
            return
        logger.exception(
            'Could not clear private interaction placeholder before public '
            'post-commit output'
        )


def public_interaction_sender(interaction):
    """Return an idempotent public sender after one private cleanup attempt.

    This helper is intentionally limited to the registration/timezone
    public-success paths. Cleanup is best effort: an already-cleared
    ``Unknown Message`` placeholder is benign, while other cleanup failures
    are logged and the public send still proceeds. A successful public send
    is cached so one committed operation cannot publish two successes if its
    caller accidentally invokes the sender twice.
    """

    cleanup_done = False
    public_sent = False
    public_message = None
    send_lock = asyncio.Lock()

    async def send(content, **kwargs):
        nonlocal cleanup_done, public_sent, public_message
        async with send_lock:
            if public_sent:
                return public_message
            if not cleanup_done:
                cleanup_done = True
                await _clear_private_original(interaction)
            channel = getattr(interaction, 'channel', None)
            channel_send = getattr(channel, 'send', None)
            if channel_send is None:
                raise RuntimeError(
                    'The interaction has no public channel sender.'
                )
            public_message = await channel_send(content, **kwargs)
            public_sent = True
            return public_message

    return send
