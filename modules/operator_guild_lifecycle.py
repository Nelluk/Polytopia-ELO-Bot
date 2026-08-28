"""Discord-cache adapter for owner-only guild suspend/resume controls."""

from __future__ import annotations

from typing import Any

import settings
from modules import guild_configuration_shadow as shadow
from modules import operator_guild_lifecycle_workers as workers
from modules.application_command_policy import build_capability_policy


def access_error(interaction: Any) -> str | None:
    guild_id = getattr(interaction, 'guild_id', None)
    if guild_id is None:
        return 'This command can only be used in a server.'
    if int(interaction.user.id) != int(settings.owner_id):
        return 'Only the configured bot owner can suspend or resume a guild.'
    profile = settings.runtime_profile
    if (
            profile.environment not in {'development', 'production'}
            or profile.guild_configuration_source != 'database'
    ):
        return 'Guild lifecycle controls require database authority.'
    if not settings.guild_configuration_ready():
        return 'The running database guild configuration is not published.'
    if settings.database_guild_configuration(int(guild_id)) is None:
        return 'Run guild lifecycle controls from an active guild.'
    return None


def target_guild(bot: Any, target_guild_id: int) -> Any:
    guild = bot.get_guild(int(target_guild_id))
    if guild is None:
        raise workers.OperatorGuildLifecycleValidationError(
            'The exact target guild is not visible to this bot.'
        )
    return guild


def build_request(
    *,
    bot: Any,
    interaction: Any,
    target_guild_id: int,
    action: str,
    operation: str = workers.PREVIEW,
    expected_state: str | None = None,
    expected_revision: int | None = None,
    expected_generation: int | None = None,
    expected_document_digest: str | None = None,
    command_plan_digest: str | None = None,
    confirmation_text: str | None = None,
) -> workers.GuildLifecycleRequest:
    target_guild_id = int(target_guild_id)
    guild = target_guild(bot, target_guild_id)
    current_ids = settings.database_guild_ids()
    records = tuple(
        settings.database_guild_configuration(guild_id)
        for guild_id in current_ids
    )
    if any(record is None for record in records):
        raise workers.OperatorGuildLifecycleValidationError(
            'The running database guild inventory is incomplete.'
        )
    snapshot = shadow.capture_discord_snapshot(
        profile=settings.runtime_profile,
        guilds=tuple(bot.guilds),
        guild_ids=tuple(sorted(set(current_ids) | {target_guild_id})),
    )
    return workers.request_from_profile(
        profile=settings.runtime_profile,
        requester_id=int(interaction.user.id),
        invoking_guild_id=int(interaction.guild_id),
        target_guild_id=target_guild_id,
        target_guild_name=str(guild.name),
        current_runtime_records=records,
        discord_snapshot=snapshot,
        action=action,
        operation=operation,
        expected_state=expected_state,
        expected_revision=expected_revision,
        expected_generation=expected_generation,
        expected_document_digest=expected_document_digest,
        command_plan_digest=command_plan_digest,
        confirmation_text=confirmation_text,
    )


def planning_policy(preview: workers.GuildLifecyclePreview):
    """Return the pre-transition policy used to describe remote current state."""

    policy = settings.application_command_policy
    assignments = {
        guild_id: policy.capabilities_for_guild(guild_id)
        for guild_id in policy.allowed_guild_ids
    }
    if preview.guild_id not in assignments:
        assignments[preview.guild_id] = ()
    return build_capability_policy(
        assignments,
        tuple(sorted(assignments)),
        families=tuple(policy.families.values()),
    )


__all__ = [
    'access_error',
    'build_request',
    'planning_policy',
    'target_guild',
]
