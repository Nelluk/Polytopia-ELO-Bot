"""Discord-side creation and reconciliation for started-game channels."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord

from modules import channels, exceptions, game_start_channel_workers as workers


logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class StartedChannelCreationResult:
    """Public reconciliation warnings produced by one channel plan."""

    game_id: int
    warnings: tuple[str, ...]


def _guild_by_id(guilds, guild_id: int):
    return discord.utils.get(guilds, id=int(guild_id))


def _warning(game_id: int, detail: str) -> str:
    return f':warning: Game {game_id} channel reconciliation: {detail}'


async def _compensate_channel(*, channel, plan, target, reason: str) -> str:
    channel_id = int(channel.id)
    guild_id = int(channel.guild.id)
    label = (
        f'game {plan.game.id}, {target.kind} target '
        f'{target.side_id if target.side_id is not None else "central"}, '
        f'guild {guild_id}, channel {channel_id}'
    )
    try:
        await channel.delete(
            reason='Unclaimed started-game channel reconciliation',
        )
    except discord.NotFound:
        return _warning(
            plan.game.id,
            f'{label} was not claimed ({reason}); the channel was already '
            'absent.',
        )
    except Exception as exc:
        logger.exception('Could not compensate unclaimed %s', label)
        return _warning(
            plan.game.id,
            f'{label} was not claimed ({reason}) and deletion is uncertain: '
            f'{exc}',
        )
    return _warning(
        plan.game.id,
        f'{label} was not claimed ({reason}); the new channel was removed.',
    )


async def _persist_and_greet(*, target_guild, channel, plan, target):
    """Claim a concrete Discord channel or compensate it before returning."""

    request = workers.PersistStartedChannelRequest(
        game_id=plan.game.id,
        guild_id=plan.game.guild_id,
        channel_id=int(channel.id),
        channel_guild_id=int(target_guild.id),
        kind=target.kind,
        side_id=target.side_id,
    )
    try:
        await workers.run_persist_started_channel(request)
    except Exception as exc:
        logger.exception(
            'Could not persist created channel %s for game %s target %s',
            channel.id,
            plan.game.id,
            target.side_id if target.side_id is not None else 'central',
        )
        return await _compensate_channel(
            channel=channel,
            plan=plan,
            target=target,
            reason=str(exc),
        )

    try:
        greeted = await channels.greet_game_channel(
            target_guild,
            chan=channel,
            player_list=target.players,
            roster_names=plan.roster_names,
            game=plan.game,
            full_game=target.kind == 'central',
        )
    except Exception as exc:
        # The channel reference is committed; greeting failure is a repairable
        # post-commit presentation issue and must never delete the channel.
        logger.exception(
            'Could not greet persisted channel %s for game %s',
            channel.id,
            plan.game.id,
        )
        return _warning(
            plan.game.id,
            f'channel {channel.id} in guild {target_guild.id} was created and '
            f'tracked, but its greeting failed: {exc}',
        )
    if greeted is False:
        return _warning(
            plan.game.id,
            f'channel {channel.id} in guild {target_guild.id} was created and '
            'tracked, but Discord rejected its greeting or topic update.',
        )
    return None


async def _create_target(
    *,
    plan: workers.StartedGameChannelPlan,
    target: workers.StartedChannelTarget,
    target_guild,
    using_team_server: bool,
):
    try:
        channel = await channels.create_game_channel(
            target_guild,
            game=plan.game,
            team_name=target.team_name or None,
            player_list=target.players,
            using_team_server_flag=using_team_server,
        )
    except exceptions.MyBaseException as exc:
        return _warning(
            plan.game.id,
            f'{target.kind} channel creation in guild {target_guild.id} '
            f'failed: {exc}',
        )
    except Exception as exc:
        logger.exception(
            'Unexpected channel creation failure for game %s target %s',
            plan.game.id,
            target.side_id if target.side_id is not None else 'central',
        )
        return _warning(
            plan.game.id,
            f'{target.kind} channel creation in guild {target_guild.id} '
            f'failed: {exc}',
        )
    if channel is None:
        return _warning(
            plan.game.id,
            f'{target.kind} channel creation in guild {target_guild.id} '
            'could not find an eligible channel category.',
        )
    return await _persist_and_greet(
        target_guild=target_guild,
        channel=channel,
        plan=plan,
        target=target,
    )


async def _finish_concrete_operation(operation):
    """Drain concrete channel effects after Discord work has begun."""

    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        while not task.done():
            if current is not None:
                while current.cancelling():
                    current.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except BaseException:
            logger.exception(
                'Cancelled started-game channel finalization completed with '
                'an error'
            )
        raise asyncio.CancelledError


def _managed_topic(topic: str | None, game_id: int) -> bool:
    if not topic:
        return True
    first_line = topic.split('\n', 1)[0]
    return (
        first_line.startswith((
            'This is the **allied team channel** for game **',
            'This is the **full game channel** for game **',
        ))
        and first_line.endswith(f'**, ID {game_id}.')
    )


async def _reuse_target(*, plan, target, target_guild) -> tuple[str, ...]:
    """Refresh a retained channel without replacing it or resetting its ACL."""

    warnings = []
    label = f'retained channel {target.channel_id} in guild {target.channel_guild_id}'
    try:
        # Fetch rather than trusting a stale cached channel/topic.
        channel = await target_guild.fetch_channel(target.channel_id)
        if (
            channel.id != target.channel_id
            or channel.guild.id != target.channel_guild_id
            or channel.type != discord.ChannelType.text
        ):
            raise workers.StartedChannelConflictError('Discord channel identity does not match.')
        await workers.run_validate_reused_channel(plan, target)
    except Exception:
        logger.exception('Could not validate %s for game %s', label, plan.game.id)
        return (_warning(
            plan.game.id,
            f'{label} could not be verified; staff must reconcile it. '
            'The game has already started; do not start it again.',
        ),)

    for player in target.players:
        member_id = player.discord_member.discord_id
        try:
            member = target_guild.get_member(member_id)
            if member is None:
                member = await target_guild.fetch_member(member_id)
            permissions = channel.permissions_for(member)
            required = ('view_channel', 'send_messages', 'read_message_history')
            missing = [name for name in required if not getattr(permissions, name)]
            if not missing:
                continue
            overwrite = channel.overwrites_for(member)
            # The default-role privacy gate can be bypassed for a participant;
            # member/other-role denials and timeouts remain staff decisions.
            role_denied = any(
                getattr(channel.overwrites_for(role), name) is False
                for role in getattr(member, 'roles', ())
                if role.id != target_guild.id
                for name in required
            )
            timed_out = getattr(member, 'is_timed_out', lambda: False)()
            if (
                timed_out or role_denied
                or any(getattr(overwrite, name) is False for name in required)
            ):
                warnings.append(_warning(
                    plan.game.id,
                    f'{label}: player `{member_id}` lacks access; a timeout or '
                    'explicit permission denial was preserved. Staff must review it.',
                ))
                continue
            for name in missing:
                setattr(overwrite, name, True)
            await channel.set_permissions(
                member,
                overwrite=overwrite,
                reason=f'Reconcile current roster for game {plan.game.id}',
            )
        except Exception:
            logger.exception('Could not reconcile player %s in %s', member_id, label)
            warnings.append(_warning(
                plan.game.id,
                f'{label}: access for player `{member_id}` could not be '
                'reconciled. Staff must review it.',
            ))

    # Moderators may customize names, categories, or topics. Only replace
    # empty topics or the bot's recognizable topic for this exact game.
    if _managed_topic(channel.topic, plan.game.id):
        topic = channels.game_channel_greeting(
            plan.roster_names, plan.game, target.players,
            full_game=target.kind == 'central',
        )[:1024]
        if topic != channel.topic:
            try:
                await channel.edit(
                    topic=topic,
                    reason=f'Refresh roster for restarted game {plan.game.id}',
                )
            except Exception:
                logger.exception('Could not refresh topic for %s', label)
                warnings.append(_warning(
                    plan.game.id, f'{label}: its roster topic could not be updated.',
                ))
    return tuple(warnings)


async def create_started_game_channels(
    *,
    plan: workers.StartedGameChannelPlan | None,
    source_guild,
    bot_guilds,
) -> StartedChannelCreationResult:
    """Create or refresh frozen targets without passing ORM objects to Discord."""

    if plan is None:
        return StartedChannelCreationResult(game_id=0, warnings=())
    if int(source_guild.id) != int(plan.game.guild_id):
        return StartedChannelCreationResult(
            game_id=plan.game.id,
            warnings=(
                _warning(
                    plan.game.id,
                    'the invoking Discord server no longer matches the '
                    'committed game.',
                ),
            ),
        )

    warnings = []
    source_channel_count = len(getattr(source_guild, 'text_channels', ()) or ())
    guilds = tuple(bot_guilds or ())

    for target in plan.reused_targets:
        target_guild = (
            source_guild if target.channel_guild_id == source_guild.id
            else _guild_by_id(guilds, target.channel_guild_id)
        )
        if target_guild is None:
            warnings.append(_warning(
                plan.game.id,
                f'retained channel {target.channel_id}: guild '
                f'{target.channel_guild_id} is unavailable; staff must review it.',
            ))
            continue
        warnings.extend(await _finish_concrete_operation(
            _reuse_target(plan=plan, target=target, target_guild=target_guild)
        ))

    for target in plan.side_targets:
        target_guild = source_guild
        using_team_server = False
        if target.force_pcplus_guild:
            target_guild = _guild_by_id(guilds, workers.PCPLUS_GUILD_ID)
            if target_guild is None:
                warnings.append(
                    _warning(
                        plan.game.id,
                        f'side {target.side_id} requires the PCPLUS server, '
                        'but that server is unavailable.',
                    )
                )
                continue
        elif target.preferred_guild_id:
            preferred = _guild_by_id(guilds, target.preferred_guild_id)
            if preferred is not None:
                target_guild = preferred
                using_team_server = True

        if (
            source_channel_count > 460
            and len(target.players) < 3
            and not using_team_server
            and 'nova' not in target.team_name.casefold()
        ):
            warnings.append(
                _warning(
                    plan.game.id,
                    'a two-player side channel was skipped because the '
                    f'source server has {source_channel_count}/500 channels.',
                )
            )
            continue

        warning = await _finish_concrete_operation(
            _create_target(
                plan=plan,
                target=target,
                target_guild=target_guild,
                using_team_server=using_team_server,
            )
        )
        if warning:
            warnings.append(warning)

    if plan.central_target is not None:
        if source_channel_count >= 425:
            warnings.append(
                _warning(
                    plan.game.id,
                    'the central game channel was skipped because the source '
                    f'server has {source_channel_count}/500 channels.',
                )
            )
        else:
            warning = await _finish_concrete_operation(
                _create_target(
                    plan=plan,
                    target=plan.central_target,
                    target_guild=source_guild,
                    using_team_server=False,
                )
            )
            if warning:
                warnings.append(warning)

    return StartedChannelCreationResult(
        game_id=plan.game.id,
        warnings=tuple(warnings),
    )
