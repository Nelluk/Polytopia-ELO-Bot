"""Discord-cache adapter for owner-only quarantined guild enrollment."""

from __future__ import annotations

from typing import Any

import runtime_config
import settings
from modules import guild_configuration_shadow as shadow
from modules import operator_guild_enrollment_workers as workers


def access_error(interaction: Any) -> str | None:
    if getattr(interaction, 'guild_id', None) is None:
        return 'This command can only be used in a server.'
    if int(interaction.user.id) != int(settings.owner_id):
        return 'Only the configured bot owner can enroll or reconfigure a guild.'
    profile = settings.runtime_profile
    if (
        profile.environment not in {'development', 'production'}
        or profile.guild_configuration_source != 'database'
    ):
        return 'Guild enrollment requires database authority.'
    if not settings.guild_configuration_ready():
        return 'The running database guild configuration is not published.'
    if settings.database_guild_configuration(int(interaction.guild_id)) is None:
        return 'Run guild enrollment from an already active guild.'
    return None


def _target_guild(bot: Any, target_guild_id: int) -> Any:
    target = bot.get_guild(int(target_guild_id))
    if target is None:
        raise workers.OperatorGuildEnrollmentValidationError(
            'The target guild is not currently visible to this bot.'
        )
    return target


def _bot_permissions(target: Any) -> tuple[str, ...]:
    member = getattr(target, 'me', None)
    permissions = getattr(member, 'guild_permissions', None)
    if permissions is None:
        raise workers.OperatorGuildEnrollmentValidationError(
            'The bot membership or permissions are unavailable in the target guild.'
        )
    return tuple(sorted(
        name for name in workers.REQUIRED_BOT_PERMISSIONS
        if bool(getattr(permissions, name, False))
    ))


def build_request(
    *,
    bot: Any,
    interaction: Any,
    target_guild_id: int,
    template: str,
    guild_type: str,
    include_in_global_leaderboard: bool | None,
    operation: str = workers.PREVIEW,
    expected_document_digest: str | None = None,
    confirmation_text: str | None = None,
) -> workers.GuildEnrollmentRequest:
    target_guild_id = int(target_guild_id)
    current_ids = settings.database_guild_ids()
    records = tuple(
        settings.database_guild_configuration(guild_id)
        for guild_id in current_ids
    )
    if any(record is None for record in records):
        raise workers.OperatorGuildEnrollmentValidationError(
            'The running database guild inventory is incomplete.'
        )
    target = _target_guild(bot, target_guild_id)
    snapshot_guild_ids = (
        current_ids
        if target_guild_id in current_ids
        else (*current_ids, target_guild_id)
    )
    snapshot = shadow.capture_discord_snapshot(
        profile=settings.runtime_profile,
        guilds=tuple(bot.guilds),
        guild_ids=snapshot_guild_ids,
    )
    return workers.request_from_profile(
        profile=settings.runtime_profile,
        requester_id=int(interaction.user.id),
        invoking_guild_id=int(interaction.guild_id),
        target_guild_id=target_guild_id,
        target_guild_name=str(target.name),
        template=template,
        guild_type=guild_type,
        include_in_global_leaderboard=include_in_global_leaderboard,
        bot_permissions=_bot_permissions(target),
        current_runtime_records=records,
        forbidden_guild_ids=runtime_config.KNOWN_PRODUCTION_GUILD_IDS,
        discord_snapshot=snapshot,
        operation=operation,
        expected_document_digest=expected_document_digest,
        confirmation_text=confirmation_text,
    )


__all__ = ['access_error', 'build_request']
