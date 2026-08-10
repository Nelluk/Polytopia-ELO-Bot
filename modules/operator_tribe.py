"""Interaction-facing service for owner-only global Tribe emoji access."""

from __future__ import annotations

import logging

import discord

import settings
from modules import operator_tribe_workers
from modules.interaction_lifecycle import public_interaction_sender


logger = logging.getLogger('polybot.' + __name__)


class OperatorTribePublicationError(RuntimeError):
    """A committed mutation could not publish its public result."""


def _actor_description(member) -> str:
    member_id = int(member.id)
    name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{member_id}'
    )
    safe_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(name)
    )
    return f'**{safe_name}** (`{member_id}`)'


def read_request(interaction, tribe: str):
    return operator_tribe_workers.OperatorTribeReadRequest(
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        tribe_lookup=str(tribe),
    )


def mutation_request(interaction, tribe: str, emoji: str):
    return operator_tribe_workers.OperatorTribeMutationRequest(
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        requester_description=_actor_description(interaction.user),
        tribe_lookup=str(tribe),
        emoji=str(emoji),
    )


def _safe(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def result_message(result, *, actor) -> str:
    actor_label = f'<@{int(actor.id)}> / {_actor_description(actor)}'
    tribe_name = _safe(result.tribe_name)
    emoji = _safe(result.emoji) if result.emoji else 'None'
    if not result.changed:
        return (
            f'Global Tribe emoji for **{tribe_name}** is {emoji}.\n'
            f'Requested by {actor_label}.'
        )
    old_emoji = _safe(result.old_emoji) if result.old_emoji else 'None'
    return (
        f'{actor_label} updated the global Tribe emoji for '
        f'**{tribe_name}** from {old_emoji} to {emoji}.'
    )


async def publish_result(interaction, result) -> None:
    try:
        await public_interaction_sender(interaction)(
            result_message(result, actor=interaction.user)
        )
    except Exception as exc:
        logger.exception(
            'Operator Tribe emoji result could not publish for Tribe %s',
            result.tribe_id,
        )
        if result.changed:
            raise OperatorTribePublicationError(
                'The Tribe emoji was committed, but its public confirmation '
                'could not be sent. Reconcile the displayed value before '
                'retrying.'
            ) from exc
        raise


async def autocomplete_tribes(
    interaction: discord.Interaction,
    current: str,
) -> list[discord.app_commands.Choice[str]]:
    if int(interaction.user.id) != int(settings.owner_id):
        return []
    try:
        results = await operator_tribe_workers.run_autocomplete(
            operator_tribe_workers.OperatorTribeAutocompleteRequest(
                requester_id=int(interaction.user.id),
                current=str(current or ''),
            )
        )
    except Exception:
        logger.exception('Operator Tribe autocomplete failed')
        return []
    return [
        discord.app_commands.Choice(name=result.tribe_name, value=result.tribe_name)
        for result in results
    ]
