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
    """Drain creation/claim/compensation after Discord work has begun."""

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


async def create_started_game_channels(
    *,
    plan: workers.StartedGameChannelPlan | None,
    source_guild,
    bot_guilds,
) -> StartedChannelCreationResult:
    """Create every frozen target without passing ORM objects to Discord."""

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
