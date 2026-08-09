"""Event-loop adapters for the bounded pending-game start workers."""

from __future__ import annotations

import logging

import settings
from modules import (
    game_broadcasts,
    game_join_leave,
    game_start_channels,
    game_start_workers,
    league,
    models,
    nova_graduation,
)
from modules import exceptions


logger = logging.getLogger('polybot.' + __name__)


def _member_description(member) -> str:
    return models.GameLog.member_string(member)


def snapshot_start_member(
    member,
    *,
    identity: game_start_workers.StartParticipantIdentity | None = None,
    member_present: bool = True,
) -> game_start_workers.StartMemberSnapshot:
    """Capture cached Discord state without passing the member to a worker."""

    roles = tuple(getattr(member, 'roles', ()) or ())
    member_id = int(member.id)
    return game_start_workers.StartMemberSnapshot(
        guild_id=int(member.guild.id),
        discord_id=member_id,
        discord_name=str(member.name),
        discord_nick=getattr(member, 'nick', None),
        display_name=str(member.display_name),
        role_ids=tuple(int(role.id) for role in roles),
        role_names=tuple(str(role.name) for role in roles),
        level=settings.get_user_level(member),
        is_mod=settings.is_mod(member),
        is_staff=settings.is_staff(member),
        description=_member_description(member),
        side_position=identity.side_position if identity else 0,
        lineup_id=identity.lineup_id if identity else None,
        player_id=identity.player_id if identity else None,
        player_name=identity.player_name if identity else str(member.display_name),
        member_present=member_present,
    )


def missing_start_member_snapshot(
    guild_id: int,
    identity: game_start_workers.StartParticipantIdentity,
) -> game_start_workers.StartMemberSnapshot:
    """Represent a participant absent from the current guild cache."""

    return game_start_workers.StartMemberSnapshot(
        guild_id=int(guild_id),
        discord_id=identity.discord_id,
        discord_name=identity.discord_name,
        discord_nick=None,
        display_name=identity.player_name,
        role_ids=(),
        role_names=(),
        level=0,
        is_mod=False,
        is_staff=False,
        description=f'**{identity.player_name}** (`{identity.discord_id}`)',
        side_position=identity.side_position,
        lineup_id=identity.lineup_id,
        player_id=identity.player_id,
        player_name=identity.player_name,
        member_present=False,
    )


def build_start_preflight_request(
    *,
    game_id: int,
    guild,
    requester,
    name: str | None,
    prefix: str | None = None,
    require_teams: bool | None = None,
    invoked_with: str = 'start',
) -> game_start_workers.StartPreflightRequest:
    """Build the primitive first-stage request from event-loop values."""

    member_snapshot = snapshot_start_member(requester)
    if prefix is None:
        prefix = settings.guild_setting(guild.id, 'command_prefix')
    if require_teams is None:
        require_teams = bool(settings.guild_setting(guild.id, 'require_teams'))
    return game_start_workers.StartPreflightRequest(
        game_id=int(game_id),
        guild_id=int(guild.id),
        name=(str(name) if name is not None else None),
        prefix=str(prefix),
        requester=member_snapshot,
        require_teams=bool(require_teams),
        invoked_with=str(invoked_with),
    )


async def execute_start(
    *,
    game_id: int,
    guild,
    requester,
    name: str | None,
    prefix: str | None = None,
    require_teams: bool | None = None,
    invoked_with: str = 'start',
) -> game_start_workers.StartResult:
    """Run preflight, resolve cached members, and commit the start."""

    preflight_request = build_start_preflight_request(
        game_id=game_id,
        guild=guild,
        requester=requester,
        name=name,
        prefix=prefix,
        require_teams=require_teams,
        invoked_with=invoked_with,
    )
    preflight = await game_start_workers.run_start_preflight(
        preflight_request,
    )

    participant_snapshots = []
    for identity in preflight.participants:
        member = guild.get_member(identity.discord_id)
        if member is None:
            participant_snapshots.append(
                missing_start_member_snapshot(guild.id, identity)
            )
        else:
            participant_snapshots.append(
                snapshot_start_member(member, identity=identity)
            )

    request = game_start_workers.StartRequest(
        game_id=preflight.game_id,
        guild_id=preflight.guild_id,
        name=str(name),
        prefix=preflight_request.prefix,
        requester=preflight_request.requester,
        participants=tuple(participant_snapshots),
        preflight=preflight,
        require_teams=preflight_request.require_teams,
        invoked_with=preflight_request.invoked_with,
    )
    return await game_start_workers.run_start(request)


class _FollowupContext:
    """Small public-output context for the native interaction adapter."""

    def __init__(self, interaction, *, prefix: str):
        self.guild = interaction.guild
        self.author = interaction.user
        self.prefix = prefix
        self.message = getattr(interaction, 'message', None)
        self._followup = interaction.followup

    async def send(self, content=None, **kwargs):
        kwargs.pop('wait', None)
        kwargs['ephemeral'] = False
        return await self._followup.send(content, **kwargs)


async def _safe_public_send(send, content: str, *, game_id: int, effect: str):
    """Send a committed public effect and keep reconciliation visible."""

    try:
        return await send(content)
    except Exception:
        logger.exception(
            'Committed started game %s public %s failed',
            game_id,
            effect,
        )
        warning = (
            f':warning: Game {game_id} started successfully, but the '
            f'{effect} could not be published. An operator must reconcile '
            'the public Discord state.'
        )
        try:
            await send(warning)
        except Exception:
            logger.exception(
                'Committed started game %s reconciliation warning failed '
                'after %s failure',
                game_id,
                effect,
            )
        return None


async def _safe_effect_warning(send, *, game_id: int, effect: str, error=None):
    if error is not None:
        logger.exception(
            'Committed started game %s %s failed',
            game_id,
            effect,
            exc_info=error if isinstance(error, BaseException) else None,
        )
    return await _safe_public_send(
        send,
        f':warning: Game {game_id} started successfully, but {effect} '
        'failed. An operator must reconcile the public state.',
        game_id=game_id,
        effect=f'{effect} reconciliation',
    )


async def publish_start_result(
    result: game_start_workers.StartResult,
    *,
    output_context,
    guild,
    prefix: str,
    bot_guilds,
    presentation: str = 'prefix',
) -> None:
    """Run independent post-commit effects over immutable result/card data."""

    send = output_context.send
    if result.broadcast_targets:
        try:
            outcomes = await game_broadcasts.reconcile_started_broadcasts(
                bot=settings.bot,
                targets=result.broadcast_targets,
            )
            retained = tuple(
                outcome
                for outcome in outcomes
                if outcome.status == game_broadcasts.RETAINED
            )
            if retained:
                shown = retained[:12]
                targets = ', '.join(
                    f'`{item.target.channel_id}/{item.target.message_id}`'
                    for item in shown
                )
                remaining = len(retained) - len(shown)
                if remaining:
                    targets += f', plus {remaining} additional target(s)'
                await _safe_public_send(
                    send,
                    f':warning: Game {result.game_id} started successfully, '
                    'but external game announcement reconciliation remains '
                    f'pending for {targets}. An operator should review the '
                    'bot log; the hourly recovery cycle may retry it.',
                    game_id=result.game_id,
                    effect='external game broadcast reconciliation',
                )
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='external game broadcasts',
                error=exc,
            )

    # These committed lifecycle warnings depend only on frozen worker output.
    for warning in result.missing_member_warnings:
        await _safe_public_send(
            send,
            warning,
            game_id=result.game_id,
            effect='missing-member warning',
        )
    if result.name_warning:
        await _safe_public_send(
            send,
            result.name_warning,
            game_id=result.game_id,
            effect='name override warning',
        )
    if result.league_warning:
        await _safe_public_send(
            send,
            result.league_warning,
            game_id=result.game_id,
            effect='season warning',
        )

    async def publish_frozen_channels():
        try:
            if (
                result.channel_plan is not None
                and settings.guild_setting(
                    guild.id,
                    'game_channel_categories',
                )
            ):
                channel_result = (
                    await game_start_channels.create_started_game_channels(
                        plan=result.channel_plan,
                        source_guild=guild,
                        bot_guilds=bot_guilds,
                    )
                )
                for warning in channel_result.warnings:
                    await _safe_public_send(
                        send,
                        warning,
                        game_id=result.game_id,
                        effect='game-channel creation reconciliation',
                    )
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='game-channel creation',
                error=exc,
            )

    ranked_str = 'unranked ' if not result.is_ranked else ''
    season = bool(result.league_season)
    season_str = ''
    if season:
        try:
            tier_name = settings.tier_lookup(result.league_tier)[1]
        except exceptions.NoMatches:
            tier_name = 'Unknown'
        except Exception as exc:
            tier_name = 'Unknown'
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='season tier-name preparation',
                error=exc,
            )
        season_str = f'**{tier_name} Season {result.league_season}** '
    announce_str = (
        f'New {season_str}{ranked_str}game ID **{result.game_id}** started! '
        f'Roster: {" ".join(result.mentions)}'
    )

    announce_channel_id = None
    channel = None
    try:
        announce_channel_id = settings.guild_setting(
            guild.id,
            'game_announce_channel',
        )
        channel = (
            guild.get_channel(announce_channel_id)
            if announce_channel_id else None
        )
    except Exception as exc:
        await _safe_effect_warning(
            send,
            game_id=result.game_id,
            effect='the configured game announcement channel lookup',
            error=exc,
        )
    if announce_channel_id and channel is None:
        await _safe_effect_warning(
            send,
            game_id=result.game_id,
            effect='the configured game announcement channel lookup',
        )

    card_destination = channel or output_context
    if channel is not None:
        try:
            await channel.send(announce_str)
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='game announcement',
                error=exc,
            )
    else:
        await _safe_public_send(
            send,
            announce_str,
            game_id=result.game_id,
            effect='game announcement',
        )

    card = None
    try:
        card = await game_join_leave.load_post_commit_game_card(
            game_id=result.game_id,
            guild=guild,
            bot=settings.bot,
            prefix=prefix,
            presentation=presentation,
            requester_id=result.requester_id,
            channel_id=int(getattr(card_destination, 'id', 0) or 0),
        )
    except Exception as exc:
        await _safe_effect_warning(
            send,
            game_id=result.game_id,
            effect='the committed game card reload',
            error=exc,
        )

    announcement = None
    if card is not None:
        try:
            announcement = await game_join_leave.send_post_commit_game_card(
                card_destination,
                card,
                content=card.rendered.content,
            )
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='the started game card',
                error=exc,
            )

    if channel is not None:
        await _safe_public_send(
            send,
            f'New {ranked_str}game ID **{result.game_id}** started! See '
            f'{channel.mention} for full details.',
            game_id=result.game_id,
            effect='game-start summary',
        )
    if announcement is not None and channel is not None:
        try:
            await game_start_workers.run_announcement_persistence(
                game_start_workers.AnnouncementReferenceRequest(
                    game_id=result.game_id,
                    guild_id=result.guild_id,
                    channel_id=int(announcement.channel.id),
                    message_id=int(announcement.id),
                )
            )
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='announcement metadata persistence',
                error=exc,
            )

    await publish_frozen_channels()

    if result.uncaught_season_game:
        await _safe_public_send(
            send,
            ':bulb: This game looks like an incorrectly named '
            '**Season Game**! You might want to use '
            f'`{prefix}rename` and include the season tag at the beginning.',
            game_id=result.game_id,
            effect='season-name warning',
        )
    if season and result.first_side_team_hidden:
        await _safe_public_send(
            send,
            ':warning: This game is marked as a **Season Game** but is not '
            'associated with a League Team. There are probably players with '
            'mixed roles on a side. I suggest you '
            f'`{prefix}unstart`, fix the roles, and re-`{prefix}start`.',
            game_id=result.game_id,
            effect='league-team warning',
        )

    try:
        if (
            result.guild_id == settings.server_ids['polychampions']
            and result.side_sizes
            and min(result.side_sizes) > 1
        ):
            await league.refresh_league_team_channels(
                settings.server_ids['polychampions']
            )
    except Exception as exc:
        await _safe_effect_warning(
            send,
            game_id=result.game_id,
            effect='league channel refresh',
            error=exc,
        )

    try:
        nova_result = await nova_graduation.run_nova_graduation(
            guild=guild,
            game_id=result.game_id,
            participant_ids=result.participant_ids,
            output_channel=output_context,
            nova_role_name=league.novas_role_name,
            grad_role_name=league.grad_role_name,
        )
        for warning in nova_result.warnings:
            await _safe_public_send(
                send,
                warning,
                game_id=result.game_id,
                effect='Nova graduation reconciliation',
            )
    except Exception as exc:
        await _safe_effect_warning(
            send,
            game_id=result.game_id,
            effect='Nova follow-up',
            error=exc,
        )

    await _safe_public_send(
        send,
        f'Game {result.game_id} is now being tracked for ELO.',
        game_id=result.game_id,
        effect='start confirmation',
    )


def native_output_context(interaction, *, prefix: str):
    return _FollowupContext(interaction, prefix=prefix)
