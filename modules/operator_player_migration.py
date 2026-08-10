"""Discord-facing service for configured-superuser player migration."""

from __future__ import annotations

import logging

import discord

from modules import operator_player_migration_workers as workers
from modules.interaction_lifecycle import public_interaction_sender


logger = logging.getLogger('polybot.' + __name__)


class PlayerMigrationPublicationError(RuntimeError):
    """A committed migration could not publish its public result."""


def actor_description(member) -> str:
    name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{member.id}'
    )
    safe = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
    return f'**{safe}** (`{int(member.id)}`)'


def preview_request(interaction, *, source_id: int, destination) -> workers.PlayerMigrationPreviewRequest:
    return workers.PlayerMigrationPreviewRequest(
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        source_id=int(source_id),
        destination_id=int(destination.id),
        destination_name=str(destination.name),
    )


def commit_request(interaction, preview) -> workers.PlayerMigrationCommitRequest:
    return workers.PlayerMigrationCommitRequest(
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        requester_description=actor_description(interaction.user),
        source_id=preview.source_id,
        destination_id=preview.destination_id,
        destination_name=preview.destination_name,
        expected_fingerprint=preview.fingerprint,
    )


def completion_message(result, actor) -> str:
    return (
        f'<@{int(actor.id)}> / {actor_description(actor)} migrated stored '
        f'player **{discord.utils.escape_markdown(result.source_name)}** '
        f'(`{result.source_id}`) to '
        f'**{discord.utils.escape_markdown(result.destination_name)}** '
        f'(<@{result.destination_id}> / `{result.destination_id}`).\n'
        f'Reparented {result.players_reparented} player(s); merged '
        f'{result.players_merged}; moved {result.lineups_reassigned} lineup(s), '
        f'{result.hosts_reassigned} host reference(s), '
        f'{result.squad_memberships_reassigned} squad membership(s), '
        f'{result.house_preferences_reassigned} House preference(s), and '
        f'{result.bids_reassigned} bid reference(s).'
    )


async def publish_result(interaction, result) -> None:
    try:
        await public_interaction_sender(interaction)(
            completion_message(result, interaction.user)
        )
    except Exception as exc:
        logger.exception(
            'Committed player migration could not publish: %s -> %s',
            result.source_id,
            result.destination_id,
        )
        raise PlayerMigrationPublicationError(
            'The migration committed, but its public confirmation failed. '
            'Reconcile the destination identity before retrying.'
        ) from exc
