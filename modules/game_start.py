"""Event-loop adapters for the bounded pending-game start workers."""

from __future__ import annotations

import logging

import settings
from modules import game_start_workers, image_storage, league, models
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
) -> None:
    """Run all post-commit effects independently over the classic card.

    ``Game.load_full_game`` and the legacy channel-reference writes remain a
    post-commit compatibility seam.  The transition itself is complete before
    this function starts; failures here therefore produce reconciliation text
    and never roll back or suppress later effects.
    """

    send = output_context.send
    game = None
    try:
        game = models.Game.load_full_game(game_id=result.game_id)
    except Exception as exc:
        logger.exception(
            'Committed started game %s could not be reloaded for effects',
            result.game_id,
        )
        await _safe_effect_warning(
            send,
            game_id=result.game_id,
            effect='the committed game card reload',
            error=exc,
        )

    if game is not None:
        try:
            await game.update_external_broadcasts(deleted=False)
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='external game broadcasts',
                error=exc,
            )

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

        season = None
        try:
            season = game.is_season_game()
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='season-state lookup',
                error=exc,
            )

        try:
            embed, content = game.embed(guild=guild, prefix=prefix)
            ranked_str = 'unranked ' if not game.is_ranked else ''
            platform_str = '' if game.is_mobile else 'Steam '
            season_str = ''
            if season:
                try:
                    tier_name = settings.tier_lookup(game.league_tier)[1]
                except exceptions.NoMatches:
                    tier_name = 'Unknown'
                season_str = f'**{tier_name} Season {season[0]}** '
            announce_str = (
                f'New {season_str}{ranked_str}{platform_str}game ID '
                f'**{game.id}** started! Roster: {" ".join(game.mentions())}'
            )
            announce_channel = settings.guild_setting(
                guild.id,
                'game_announce_channel',
            )
            channel = guild.get_channel(announce_channel) if announce_channel else None
            if announce_channel and channel is None:
                await _safe_effect_warning(
                    send,
                    game_id=result.game_id,
                    effect='the configured game announcement channel lookup',
                )

            card_destination = channel or output_context
            if channel is not None:
                await _safe_public_send(
                    channel.send,
                    announce_str,
                    game_id=result.game_id,
                    effect='game announcement',
                )
            else:
                await _safe_public_send(
                    send,
                    announce_str,
                    game_id=result.game_id,
                    effect='game announcement',
                )

            announcement = None
            try:
                announcement = await image_storage.send_game_embed(
                    card_destination,
                    game,
                    embed=embed,
                    content=content,
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
                    f'New {ranked_str}game ID **{game.id}** started! See '
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
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='game announcement/card preparation',
                error=exc,
            )

        try:
            if settings.guild_setting(guild.id, 'game_channel_categories'):
                await game.create_game_channels(
                    bot_guilds,
                    guild.id,
                )
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='game-channel creation',
                error=exc,
            )

        try:
            if game.is_uncaught_season_game():
                await _safe_public_send(
                    send,
                    ':bulb: This game looks like an incorrectly named '
                    '**Season Game**! You might want to use '
                    f'`{prefix}rename` and include the season tag at the '
                    'beginning.',
                    game_id=result.game_id,
                    effect='season-name warning',
                )
            if season and game.gamesides[0].team.is_hidden:
                await _safe_public_send(
                    send,
                    ':warning: This game is marked as a **Season Game** but '
                    'is not associated with a League Team. There are probably '
                    'players with mixed roles on a side. I suggest you '
                    f'`{prefix}unstart`, fix the roles, and re-`{prefix}start`.',
                    game_id=result.game_id,
                    effect='league-team warning',
                )
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='season warning preparation',
                error=exc,
            )

        try:
            if game.guild_id == settings.server_ids['polychampions'] and game.smallest_team() > 1:
                league.populate_league_team_channels()
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='league channel refresh',
                error=exc,
            )

        try:
            await league.auto_grad_novas(guild, game, output_context)
        except Exception as exc:
            await _safe_effect_warning(
                send,
                game_id=result.game_id,
                effect='Nova follow-up',
                error=exc,
            )
    else:
        # Keep lifecycle output public even if a compatibility reload fails.
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

    await _safe_public_send(
        send,
        f'Game {result.game_id} is now being tracked for ELO.',
        game_id=result.game_id,
        effect='start confirmation',
    )


def native_output_context(interaction, *, prefix: str):
    return _FollowupContext(interaction, prefix=prefix)
