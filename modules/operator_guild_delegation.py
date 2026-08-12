"""Discord-facing helpers for owner delegation policy management."""

from __future__ import annotations

from typing import Any

import settings
from modules import operator_guild_delegation_workers as workers


def access_error(interaction: Any) -> str | None:
    if getattr(interaction, 'guild_id', None) is None:
        return 'This command can only be used in a server.'
    if int(interaction.user.id) != int(settings.owner_id):
        return 'Only the configured bot owner can grant configuration delegation.'
    profile = settings.runtime_profile
    if (
            profile.environment != 'development'
            or profile.guild_configuration_source != 'database'
    ):
        return 'Guild delegation requires development database authority.'
    if not settings.guild_configuration_ready():
        return 'The running database guild configuration is not published.'
    if settings.database_guild_configuration(int(interaction.guild_id)) is None:
        return 'This server is not active in the running configuration snapshot.'
    return None


def _role_evidence(guild: Any) -> tuple[workers.DiscordRoleEvidence, ...]:
    values = []
    for role in tuple(getattr(guild, 'roles', ())):
        role_id = int(role.id)
        is_default = getattr(role, 'is_default', None)
        values.append(workers.DiscordRoleEvidence(
            role_id=role_id,
            managed=bool(getattr(role, 'managed', False)),
            everyone=(
                bool(is_default()) if callable(is_default)
                else role_id == int(guild.id)
            ),
        ))
    return tuple(sorted(values, key=lambda value: value.role_id))


def build_request(
    *,
    interaction: Any,
    operation: str,
    expected_policy_version: int | None = None,
    manager_role_ids: tuple[int, ...] | None = None,
    allow_activation: bool | None = None,
    expected_plan_digest: str | None = None,
    confirmation_text: str | None = None,
) -> workers.GuildDelegationRequest:
    return workers.request_from_profile(
        profile=settings.runtime_profile,
        requester_id=int(interaction.user.id),
        guild_id=int(interaction.guild_id),
        operation=operation,
        role_evidence=_role_evidence(interaction.guild),
        runtime_guild_ids=settings.database_guild_ids(),
        expected_policy_version=expected_policy_version,
        manager_role_ids=manager_role_ids,
        allow_activation=allow_activation,
        expected_plan_digest=expected_plan_digest,
        confirmation_text=confirmation_text,
    )


__all__ = ['access_error', 'build_request']
