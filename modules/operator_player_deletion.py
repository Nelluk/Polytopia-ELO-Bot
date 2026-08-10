"""Discord-facing service for owner-only orphan player deletion."""

from __future__ import annotations

import logging

import discord

from modules import operator_player_deletion_workers as workers
from modules.interaction_lifecycle import public_interaction_sender


logger = logging.getLogger('polybot.' + __name__)


class PlayerDeletionPublicationError(RuntimeError):
    """A committed player deletion could not publish its public result."""


def actor_description(member) -> str:
    name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{member.id}'
    )
    safe = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
    return f'**{safe}** (`{int(member.id)}`)'


def preview_request(interaction, *, target_id: int) -> workers.PlayerDeletionPreviewRequest:
    return workers.PlayerDeletionPreviewRequest(
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        target_id=int(target_id),
    )


def commit_request(
    interaction,
    preview,
    *,
    confirmation_text: str,
) -> workers.PlayerDeletionCommitRequest:
    return workers.PlayerDeletionCommitRequest(
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        requester_description=actor_description(interaction.user),
        target_id=int(preview.target_id),
        expected_fingerprint=str(preview.fingerprint),
        confirmation_text=str(confirmation_text),
    )


def completion_message(result, actor) -> str:
    target_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(result.target_name)
    )
    return (
        f'<@{int(actor.id)}> / {actor_description(actor)} deleted orphan '
        f'stored player **{target_name}** (`{result.target_id}`). '
        f'Deleted {result.players_deleted} guild Player row(s), '
        f'{result.squad_memberships_deleted} squad membership(s), and '
        f'{result.house_preferences_deleted} House preference(s). '
        'Historical games, bids, API ownership, and external privacy records '
        'were not deleted by this command.'
    )


async def publish_result(interaction, result) -> None:
    try:
        await public_interaction_sender(interaction)(
            completion_message(result, interaction.user)
        )
    except Exception as exc:
        logger.exception(
            'Committed player deletion could not publish for %s',
            result.target_id,
        )
        raise PlayerDeletionPublicationError(
            'The deletion committed, but its public confirmation failed. '
            'Reconcile the stored identity before retrying.'
        ) from exc
