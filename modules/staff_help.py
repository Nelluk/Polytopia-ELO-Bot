"""Environment-explicit delivery policy for the shared ``/staffhelp`` form."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import logging
import os
import re
from typing import Any, Mapping

import discord

from modules import beta_feedback
from modules import staff_help_workers
from runtime_config import RuntimeProfile, get_runtime_profile


logger = logging.getLogger('polybot.' + __name__)
_CHECKPOINT = re.compile(r'^[0-9a-f]{7,40}$')
_DISCORD_MESSAGE_LINK = re.compile(
    r'https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/'
    r'\d{15,22}/\d{15,22}/\d{15,22}(?!\d)',
    re.IGNORECASE,
)


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
class PolyEloFeedbackRoute:
    """One bot-level production maintainer destination without a role ping."""

    guild_id: int
    channel_id: int
    channel: Any


@dataclass(frozen=True, slots=True)
class StaffHelpSubmission:
    """Environment-neutral outcome used by the shared modal."""

    environment: str
    delivered: bool
    stored: bool
    report_id: str | None = None
    relay_message_id: int | None = None
    destination: str = 'server_staff'


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
    helper_role = settings.resolve_configured_role(
        guild,
        'helper_roles',
    )
    if helper_role is None:
        raise StaffHelpConfigurationError(
            'This server helper role is unavailable.'
        )
    helper_role_name = getattr(helper_role, 'name', None)
    if not isinstance(helper_role_name, str) or not helper_role_name.strip():
        raise StaffHelpConfigurationError(
            'This server has no valid configured helper role.'
        )
    helper_role_name = helper_role_name.strip()
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


def resolve_polyelo_feedback_route(
        bot: Any,
        *,
        profile: RuntimeProfile | None = None) -> PolyEloFeedbackRoute:
    """Resolve the explicit bot-level maintainer channel from server settings."""

    selected = _selected_profile(profile)
    value = getattr(selected.server_settings, 'polyelo_feedback_route', None)
    if not isinstance(value, Mapping) or set(value) != {'guild_id', 'channel_id'}:
        raise StaffHelpConfigurationError(
            'The PolyELO maintainer feedback route is not configured.'
        )
    guild_id = value.get('guild_id')
    channel_id = value.get('channel_id')
    for field, item in (('guild_id', guild_id), ('channel_id', channel_id)):
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise StaffHelpConfigurationError(
                f'The PolyELO maintainer feedback {field} is invalid.'
            )
    if int(guild_id) not in selected.allowed_guild_ids:
        raise StaffHelpConfigurationError(
            'The PolyELO maintainer feedback server is not allowlisted.'
        )
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise StaffHelpConfigurationError(
            'The PolyELO maintainer feedback server is unavailable.'
        )
    channel = guild.get_channel(int(channel_id))
    if channel is None or not callable(getattr(channel, 'send', None)):
        raise StaffHelpConfigurationError(
            'The PolyELO maintainer feedback channel is unavailable.'
        )
    return PolyEloFeedbackRoute(
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        channel=channel,
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
    local_error = None
    maintainer_error = None
    try:
        resolve_production_route(bot, guild_id)
    except StaffHelpConfigurationError as exc:
        local_error = exc
    try:
        resolve_polyelo_feedback_route(bot, profile=selected)
    except StaffHelpConfigurationError as exc:
        maintainer_error = exc
    if local_error is not None and maintainer_error is not None:
        logger.warning(
            'Production staffhelp preflight failed (guild=%s local=%s central=%s).',
            guild_id,
            local_error,
            maintainer_error,
        )
        return (
            'Staff help is not configured for this server. '
            'Please ping a server staff member directly.'
        )
    return None


def _production_embed(
        draft: beta_feedback.FeedbackReportDraft,
        *,
        destination: str,
        source_guild: Any,
        related_game: staff_help_workers.RelatedGame | None) -> discord.Embed:
    titles = {
        'server_staff': 'Server staff request',
        'polyelo_bug': 'PolyELO bug report',
        'polyelo_feature': 'PolyELO improvement suggestion',
    }
    embed = discord.Embed(
        title=f'{titles[destination]}: {draft.summary}',
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
        name='Source server',
        value=(
            f'{getattr(source_guild, "name", "Unknown server")} '
            f'(`{draft.guild_id}`)'
        ),
        inline=False,
    )
    embed.add_field(
        name='Source channel',
        value=(
            f'<#{draft.channel_id}> (`{draft.channel_id}`) — '
            f'[Open source channel]('
            f'https://discord.com/channels/{draft.guild_id}/{draft.channel_id})'
        ),
        inline=False,
    )
    if draft.context:
        embed.add_field(name='Related context', value=draft.context, inline=False)
        message_link = _DISCORD_MESSAGE_LINK.search(draft.context)
        if message_link is not None:
            embed.add_field(
                name='Related message',
                value=f'[Open related message]({message_link.group(0)})',
                inline=False,
            )
    if draft.game_id is not None and related_game is None:
        embed.add_field(name='Game', value=str(draft.game_id), inline=True)
    if related_game is not None:
        game_value = f'`{related_game.game_id}` — {related_game.status}'
        if related_game.name:
            game_value += f' — {related_game.name}'
        embed.add_field(name='Related game', value=game_value, inline=False)
    if draft.command_reference:
        embed.add_field(
            name='Command',
            value=draft.command_reference,
            inline=True,
        )
    checkpoint = str(
        draft.git_checkpoint
        or os.environ.get('POLYBOT_GIT_CHECKPOINT', '')
        or os.environ.get('POLYBOT_BUILD_CHECKPOINT', '')
    ).strip()
    if _CHECKPOINT.fullmatch(checkpoint):
        embed.add_field(name='Bot checkpoint', value=f'`{checkpoint}`', inline=False)
    embed.set_footer(text='Submitted through /staffhelp')
    return embed


async def _send_production_relay(
        route: ProductionStaffHelpRoute,
        draft: beta_feedback.FeedbackReportDraft,
        *,
        source_guild: Any,
        related_game: staff_help_workers.RelatedGame | None) -> Any:
    files = [
        discord.File(BytesIO(attachment.data), filename=attachment.filename)
        for attachment in draft.attachments
    ]
    try:
        return await route.channel.send(
            f'Attention {route.helper_role.mention} — new staff-help request.',
            embed=_production_embed(
                draft,
                destination='server_staff',
                source_guild=source_guild,
                related_game=related_game,
            ),
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
        draft: beta_feedback.FeedbackReportDraft,
        *,
        profile: RuntimeProfile | None = None) -> tuple[int | None, str]:
    """Deliver one report to the invoking guild without local persistence."""

    related_game = await staff_help_workers.run_find_related_game(
        channel_id=draft.channel_id,
        game_id=draft.game_id,
    )
    source_guild = bot.get_guild(draft.guild_id)
    if draft.category == 'help':
        destination = 'server_staff'
        route = resolve_production_route(
            bot,
            related_game.guild_id if related_game is not None else draft.guild_id,
        )
        send = _send_production_relay(
            route,
            draft,
            source_guild=source_guild,
            related_game=related_game,
        )
    else:
        destination = (
            'polyelo_bug' if draft.category == 'bug' else 'polyelo_feature'
        )
        route = resolve_polyelo_feedback_route(bot, profile=profile)
        files = [
            discord.File(BytesIO(item.data), filename=item.filename)
            for item in draft.attachments
        ]

        async def send_polyelo_feedback():
            try:
                return await route.channel.send(
                    'New PolyELO feedback report.',
                    embed=_production_embed(
                        draft,
                        destination=destination,
                        source_guild=source_guild,
                        related_game=related_game,
                    ),
                    files=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                for file in files:
                    file.close()

        send = send_polyelo_feedback()
    relay_task = asyncio.create_task(send)
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
    return (int(message_id) if message_id is not None else None, destination)


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
            destination='development_feedback',
            report_id=result.report.report_id,
        )

    message_id, destination = await relay_production(
        bot,
        draft,
        profile=selected,
    )
    return StaffHelpSubmission(
        environment='production',
        delivered=True,
        stored=False,
        destination=destination,
        relay_message_id=message_id,
    )
