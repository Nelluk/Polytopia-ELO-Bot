"""Discord-facing helpers for owner delegation policy management."""

from __future__ import annotations

from typing import Any

import settings
from modules import operator_guild_delegation_workers as workers


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


def target_guild(bot: Any, target_guild_id: int) -> Any:
    guild = bot.get_guild(int(target_guild_id))
    if guild is None:
        raise workers.OperatorGuildDelegationValidationError(
            'The selected server is not visible to this development bot.'
        )
    return guild


def assignable_role_names(guild: Any) -> dict[int, str]:
    values = {}
    for role in tuple(getattr(guild, 'roles', ())):
        role_id = int(role.id)
        is_default = getattr(role, 'is_default', None)
        if (
                role_id <= 0
                or bool(getattr(role, 'managed', False))
                or (callable(is_default) and bool(is_default()))
        ):
            continue
        values[role_id] = str(role.name)
    return values


def build_request(
    *,
    bot: Any,
    interaction: Any,
    target_guild_id: int,
    operation: str,
    expected_policy_version: int | None = None,
    manager_role_ids: tuple[int, ...] | None = None,
    allow_activation: bool | None = None,
    expected_plan_digest: str | None = None,
    confirmation_text: str | None = None,
) -> workers.GuildDelegationRequest:
    guild = target_guild(bot, target_guild_id)
    return workers.request_from_profile(
        profile=settings.runtime_profile,
        requester_id=int(interaction.user.id),
        guild_id=int(target_guild_id),
        operation=operation,
        role_evidence=_role_evidence(guild),
        runtime_guild_ids=settings.database_guild_ids(),
        expected_policy_version=expected_policy_version,
        manager_role_ids=manager_role_ids,
        allow_activation=allow_activation,
        expected_plan_digest=expected_plan_digest,
        confirmation_text=confirmation_text,
    )


__all__ = [
    'assignable_role_names', 'build_request', 'target_guild',
]
