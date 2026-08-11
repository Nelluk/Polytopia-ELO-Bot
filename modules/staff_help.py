"""Environment-explicit delivery policy for the shared ``/staffhelp`` form."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import logging
from typing import Any

import discord

from modules import beta_feedback
from runtime_config import RuntimeProfile, get_runtime_profile


logger = logging.getLogger('polybot.' + __name__)


class StaffHelpError(Exception):
    """Base class for expected staff-help delivery failures."""


class StaffHelpConfigurationError(StaffHelpError):
    """The production guild has no complete configured relay destination."""


class StaffHelpDeliveryError(StaffHelpError):
    """The production relay did not complete successfully."""


@dataclass(frozen=True, slots=True)
class ProductionStaffHelpRoute:
    """One event-loop-local production channel and exact helper role."""

    guild_id: int
    channel_id: int
    helper_role_name: str
    channel: Any
    helper_role: Any


@dataclass(frozen=True, slots=True)
class StaffHelpSubmission:
    """Environment-neutral outcome used by the shared modal."""

    environment: str
    delivered: bool
    stored: bool
    report_id: str | None = None
    relay_message_id: int | None = None


def _selected_profile(profile: RuntimeProfile | None) -> RuntimeProfile:
    selected = profile or get_runtime_profile()
    if selected.environment not in {'development', 'production'}:
        raise StaffHelpConfigurationError(
            'Staff help requires an explicit development or production profile.'
        )
    return selected


def resolve_production_route(bot: Any, guild_id: int) -> ProductionStaffHelpRoute:
    """Resolve the invoking guild's configured channel and first helper role."""

    import settings

    normalized_guild_id = int(guild_id)
    guild = bot.get_guild(normalized_guild_id)
    if guild is None:
        raise StaffHelpConfigurationError(
            'The configured server is not available to the bot.'
        )

    try:
        channel_id = settings.guild_setting(
            normalized_guild_id,
            'staff_help_channel',
        )
        configured_roles = settings.guild_setting(
            normalized_guild_id,
            'helper_roles',
        )
    except Exception as exc:
        raise StaffHelpConfigurationError(
            'This server staff-help configuration is unavailable.'
        ) from exc
    if isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id <= 0:
        raise StaffHelpConfigurationError(
            'This server has no configured staff-help channel.'
        )
    channel = guild.get_channel(channel_id)
    if channel is None or not callable(getattr(channel, 'send', None)):
        raise StaffHelpConfigurationError(
            'This server staff-help channel is unavailable.'
        )

    if not isinstance(configured_roles, (list, tuple)) or not configured_roles:
        raise StaffHelpConfigurationError(
            'This server has no configured helper role.'
        )
    helper_role_name = configured_roles[0]
    if not isinstance(helper_role_name, str) or not helper_role_name.strip():
        raise StaffHelpConfigurationError(
            'This server has no valid configured helper role.'
        )
    helper_role_name = helper_role_name.strip()
    helper_role = discord.utils.get(
        getattr(guild, 'roles', ()),
        name=helper_role_name,
    )
    if helper_role is None:
        raise StaffHelpConfigurationError(
            'This server helper role is unavailable.'
        )
    is_default = getattr(helper_role, 'is_default', None)
    if helper_role_name == '@everyone' or (
            callable(is_default) and is_default()):
        raise StaffHelpConfigurationError(
            'The everyone role cannot be used for staff help.'
        )
    if not isinstance(getattr(helper_role, 'mention', None), str):
        raise StaffHelpConfigurationError(
            'This server helper role cannot be mentioned.'
        )

    return ProductionStaffHelpRoute(
        guild_id=normalized_guild_id,
        channel_id=channel_id,
        helper_role_name=helper_role_name,
        channel=channel,
        helper_role=helper_role,
    )


def availability_error(
        bot: Any,
        guild_id: int,
        *,
        profile: RuntimeProfile | None = None) -> str | None:
    """Return a private preflight error without weakening submit-time checks."""

    selected = _selected_profile(profile)
    if selected.environment == 'development':
        return None
    try:
        resolve_production_route(bot, guild_id)
    except StaffHelpConfigurationError as exc:
        logger.warning(
            'Production staffhelp preflight failed (guild=%s error=%s).',
            guild_id,
            exc,
        )
        return (
            'Staff help is not configured for this server. '
            'Please ping a server staff member directly.'
        )
    return None


def _production_embed(draft: beta_feedback.FeedbackReportDraft) -> discord.Embed:
    embed = discord.Embed(
        title=f'Staff help: {draft.summary}',
        description=draft.details,
    )
    embed.add_field(name='Category', value=draft.category, inline=True)
    embed.add_field(
        name='Requester',
        value=(
            f'{draft.requester_display_name} '
            f'(`<@{draft.requester_id}>` / `{draft.requester_id}`)'
        ),
        inline=False,
    )
    embed.add_field(
        name='Source channel',
        value=f'<#{draft.channel_id}> (`{draft.channel_id}`)',
        inline=False,
    )
    if draft.context:
        embed.add_field(name='Related context', value=draft.context, inline=False)
    if draft.game_id is not None:
        embed.add_field(name='Game', value=str(draft.game_id), inline=True)
    if draft.command_reference:
        embed.add_field(
            name='Command',
            value=draft.command_reference,
            inline=True,
        )
    embed.set_footer(text='Submitted through /staffhelp')
    return embed


async def _send_production_relay(
        route: ProductionStaffHelpRoute,
        draft: beta_feedback.FeedbackReportDraft) -> Any:
    files = [
        discord.File(BytesIO(attachment.data), filename=attachment.filename)
        for attachment in draft.attachments
    ]
    try:
        return await route.channel.send(
            f'Attention {route.helper_role.mention} — new staff-help request.',
            embed=_production_embed(draft),
            files=files,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=(route.helper_role,),
                replied_user=False,
            ),
        )
    finally:
        for file in files:
            file.close()


async def relay_production(
        bot: Any,
        draft: beta_feedback.FeedbackReportDraft) -> int | None:
    """Deliver one report to the invoking guild without local persistence."""

    route = resolve_production_route(bot, draft.guild_id)
    relay_task = asyncio.create_task(_send_production_relay(route, draft))
    try:
        message = await asyncio.shield(relay_task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        while not relay_task.done():
            if current is not None and hasattr(current, 'uncancel'):
                current.uncancel()
            await asyncio.sleep(0.001)
        try:
            completed = relay_task.result()
        except Exception:
            logger.warning(
                'Cancelled production staffhelp relay completed with failure '
                '(guild=%s requester=%s).',
                draft.guild_id,
                draft.requester_id,
                exc_info=True,
            )
        else:
            logger.warning(
                'Cancelled production staffhelp relay completed successfully '
                '(guild=%s requester=%s message=%s); no acknowledgement was sent.',
                draft.guild_id,
                draft.requester_id,
                getattr(completed, 'id', 'unknown'),
            )
        raise
    except Exception as exc:
        logger.warning(
            'Production staffhelp relay failed '
            '(guild=%s channel=%s requester=%s error=%s).',
            draft.guild_id,
            route.channel_id,
            draft.requester_id,
            type(exc).__name__,
        )
        raise StaffHelpDeliveryError(
            'The staff-help message could not be delivered.'
        ) from exc
    message_id = getattr(message, 'id', None)
    return int(message_id) if message_id is not None else None


async def submit(
        bot: Any,
        draft: beta_feedback.FeedbackReportDraft,
        *,
        profile: RuntimeProfile | None = None) -> StaffHelpSubmission:
    """Select exactly one reviewed backend from the explicit runtime profile."""

    selected = _selected_profile(profile)
    if selected.environment == 'development':
        result = await beta_feedback.submit_native_report(bot, draft)
        return StaffHelpSubmission(
            environment='development',
            delivered=result.relay_ok,
            stored=True,
            report_id=result.report.report_id,
        )

    message_id = await relay_production(bot, draft)
    return StaffHelpSubmission(
        environment='production',
        delivered=True,
        stored=False,
        relay_message_id=message_id,
    )
