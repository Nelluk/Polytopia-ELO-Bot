from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import modules.image_storage as image_storage
import modules.channels as channels
import settings
import logging
import peewee
import modules.exceptions as exceptions
import datetime
import asyncio
import discord
import re
import functools
from modules.games import PolyGame, post_win_messaging
import modules.achievements as achievements
from modules import elo_workers, game_workers
from modules.elo_jobs import EloJobConflict
from modules import team_emoji as team_emoji_service
from modules import team_emoji_workers
from modules import team_creation as team_creation_service
from modules import team_creation_workers
from modules import team_attributes as team_attributes_service
from modules import team_attributes_workers
from modules import team_image as team_image_service
from modules import team_image_workers
from modules import team_show as team_show_service
from modules import team_show_workers
from modules import incomplete_game_purge

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')

def load_unconfirmed_game_summaries(guild_id: int):
    """Load display-only unconfirmed game data on a local connection."""

    with models.db.connection_context():
        game_query = models.Game.search(
            status_filter=5,
            guild_id=guild_id,
        ).order_by(models.Game.win_claimed_ts)
        return utilities.summarize_game_list(game_query)


def format_elo_job_status(active_job, now=None):
    """Render coordinator state without performing database work."""

    if active_job is None:
        return 'No ELO mutation job is currently running.'

    now = now or discord.utils.utcnow()
    elapsed_seconds = max(
        0,
        int((now - active_job.started_at).total_seconds()),
    )
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    elapsed_parts = []
    if hours:
        elapsed_parts.append(f'{hours}h')
    if minutes or hours:
        elapsed_parts.append(f'{minutes}m')
    elapsed_parts.append(f'{seconds}s')

    requester = active_job.requester_name
    if active_job.requester_id is not None:
        requester += f' (`{active_job.requester_id}`)'
    game = (
        active_job.game_id
        if active_job.game_id is not None
        else 'all games'
    )
    started_timestamp = int(active_job.started_at.timestamp())
    return (
        f'Active ELO job: `{active_job.operation}`\n'
        f'Game: `{game}`\n'
        f'Requester: {requester}\n'
        f'Started: <t:{started_timestamp}:F> (<t:{started_timestamp}:R>)\n'
        f'Elapsed: {" ".join(elapsed_parts)}'
    )


class administration(commands.Cog):
    elo_group = discord.app_commands.Group(
        name='elo',
        description='Inspect and maintain ELO calculations.',
        guild_only=True,
    )
    team_group = discord.app_commands.Group(
        name='team',
        description='View and manage competitive team attributes.',
        guild_only=True,
    )

    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = asyncio.create_task(self.task_confirm_auto())
            self.bg_task2 = asyncio.create_task(self.task_purge_incomplete())

    async def cog_check(self, ctx):

        if settings.is_staff(ctx.author):
            return True
        else:
            if ctx.invoked_with == 'help' and ctx.command.name != 'help':
                return False
            else:
                await ctx.send('You do not have permission to use this command.')
                return False

    async def _run_confirm_game_job(
        self,
        *,
        game_id: int,
        guild_id: int,
        requester_id: int | None,
        requester_name: str,
    ):
        utilities.lock_game(game_id)
        try:
            return await settings.elo_job_coordinator.run(
                operation='confirm_game',
                game_id=game_id,
                requester_id=requester_id,
                requester_name=requester_name,
                worker=elo_workers.confirm_game,
                worker_args=(game_id, guild_id),
            )
        finally:
            utilities.unlock_game(game_id)

    async def _confirm_game_and_post(
        self,
        *,
        game_id: int,
        guild,
        prefix: str,
        channel,
        requester,
    ):
        result = await self._run_confirm_game_job(
            game_id=game_id,
            guild_id=guild.id,
            requester_id=requester.id,
            requester_name=requester.display_name,
        )
        winning_game = models.Game.load_full_game(result.game_id)
        await post_win_messaging(
            guild,
            prefix,
            channel,
            winning_game,
        )
        return result

    async def _run_recalculation_job(
        self,
        *,
        game_id: int,
        requester_id: int,
        requester_name: str,
    ):
        return await settings.elo_job_coordinator.run(
            operation='recalc_games_from',
            game_id=game_id,
            requester_id=requester_id,
            requester_name=requester_name,
            worker=elo_workers.recalculate_games_from,
            worker_args=(game_id,),
        )

    async def _set_ranked_state_and_post(
        self,
        *,
        game_id: int,
        guild,
        is_ranked: bool,
        requester,
    ):
        utilities.lock_game(game_id)
        try:
            result = await game_workers.run_ranked_state_correction(
                game_id,
                guild.id,
                is_ranked,
                models.GameLog.member_string(requester),
            )
            game = models.Game.load_full_game(result.game_id)
            state = 'ranked' if result.is_ranked else 'unranked'
            await game.update_squad_channels(
                guild_list=settings.bot.guilds,
                guild_id=guild.id,
                message=(
                    f'Staff member **{requester.display_name}** has set this '
                    f'game to be *{state}*.'
                ),
            )
            return (
                f'Game {game.id} is now marked as {state}.\n'
                f'Notifying players: {" ".join(game.mentions())}'
            )
        finally:
            utilities.unlock_game(game_id)

    async def _extend_pending_game(
        self,
        *,
        game_id: int,
        guild_id: int,
        requester,
    ):
        utilities.lock_game(game_id)
        try:
            return await game_workers.run_pending_game_extension(
                game_id,
                guild_id,
                models.GameLog.member_string(requester),
            )
        finally:
            utilities.unlock_game(game_id)

    async def _unstart_game_and_post(
        self,
        *,
        game_id: int,
        guild,
        prefix: str,
        requester,
        invocation_channel_id: int | None,
        invoked_with: str | None = None,
    ):
        utilities.lock_game(game_id)
        try:
            result = await game_workers.run_game_unstart(
                game_id,
                guild.id,
                models.GameLog.member_string(requester),
                invoked_with or f'{prefix}unstart',
                invocation_channel_id,
            )

            warnings = []
            if (
                result.announcement_channel_id is not None
                and result.announcement_message_id is not None
            ):
                try:
                    game = models.Game.load_full_game(result.game_id)
                    # Render the same cancelled in-progress card as the
                    # legacy command without persisting the display-only name.
                    game.is_pending = False
                    game.name = f'~~{result.game_name}~~ GAME CANCELLED'
                    await game.update_announcement(
                        guild=guild,
                        prefix=prefix,
                    )
                except (peewee.PeeweeException, exceptions.MyBaseException):
                    logger.exception(
                        'Could not update the cancelled announcement for '
                        'unstarted game %s',
                        result.game_id,
                    )
                    warnings.append('the game announcement was not updated')

            deleted_targets = []
            for target in result.channel_targets:
                target_guild = discord.utils.get(
                    self.bot.guilds,
                    id=target.guild_id,
                )
                if target_guild is None:
                    logger.warning(
                        'Could not find guild %s for game %s channel %s',
                        target.guild_id,
                        result.game_id,
                        target.channel_id,
                    )
                    continue
                if await channels.delete_game_channel(
                    target_guild,
                    target.channel_id,
                ):
                    deleted_targets.append(target)

            if deleted_targets:
                try:
                    await game_workers.run_deleted_channel_reconciliation(
                        result.game_id,
                        guild.id,
                        tuple(deleted_targets),
                    )
                except peewee.PeeweeException:
                    logger.exception(
                        'Could not reconcile deleted channels for unstarted '
                        'game %s',
                        result.game_id,
                    )
                    warnings.append(
                        'deleted channel references need reconciliation'
                    )

            if len(deleted_targets) != len(result.channel_targets):
                warnings.append('one or more game channels were not deleted')

            message = (
                f'Game {result.game_id} is now an open game and no longer in '
                f'progress.\nNotifying players: {" ".join(result.mentions)}'
            )
            if warnings:
                message += f'\n:warning: {"; ".join(warnings)}.'
            return message
        finally:
            utilities.unlock_game(game_id)

    @settings.is_superuser_check()
    @commands.command(aliases=['quit', 'restart_force'])
    async def restart(self, ctx):
        """ *Owner*: Close database connection and quit bot gracefully """

        if settings.elo_job_coordinator.is_active and ctx.invoked_with != 'restart_force':
            logger.info('Skipping command due to active ELO job')
            return await ctx.send(f':warning: {ctx.author.mention} - I am currently recalculating the results of prior games. A restart seems like a bad idea. Force restart with `{ctx.prefix}restart_force`')

        settings.maintenance_mode = True
        logger.debug(f'Purging message list {self.bot.purgable_messages}')
        try:
            if models.db.close():
                close_message = 'db connection closing normally'
            else:
                close_message = 'db connection was already closed'

        except peewee.PeeweeException as e:
            message = f'Error during post_invoke_cleanup db.close(): {e}'
        finally:
            logger.info(close_message)

        if settings.run_tasks and self.bot.purgable_messages:
            async with ctx.typing():
                for guild_id, channel_id, message_id in reversed(self.bot.purgable_messages):
                    # purge messages created by Misc.task_broadcast_newbie_message() so they arent duplicated when bot restarts
                    guild = self.bot.get_guild(guild_id)
                    channel = guild.get_channel(channel_id)
                    try:
                        logger.debug(f'Purging message {message_id} from channel {channel.id if channel else "NONE"}')
                        message = await channel.fetch_message(message_id)
                        await message.delete()
                    except discord.DiscordException:
                        pass

            await ctx.send('Cleaning up temporary announcement messages...')
            await asyncio.sleep(3)  # to make sure message deletes go through

        await ctx.send('Shutting down')
        await self.bot.close()

    @commands.is_owner()
    @commands.command()
    async def purge_game_channels(self, ctx, *, arg: str = None):

        purged_channels = 0
        current_number_of_channels = len(ctx.guild.text_channels)

        if not settings.guild_setting(ctx.guild.id, 'game_channel_categories'):
            return await ctx.send('Cannot purge - this guild has no `game_channel_categories` setting')

        category_channels = [chan.id for chan in ctx.guild.channels if chan.category_id in settings.guild_setting(ctx.guild.id, 'game_channel_categories')]

        common_game_channels = models.Game.select(models.Game.game_chan).where(
            (models.Game.is_completed == 0) &
            (models.Game.guild_id == ctx.guild.id) &
            (models.Game.game_chan > 0)
        ).tuples()

        game_side_channels = models.GameSide.select(models.GameSide.team_chan).join(models.Game).where(
            (models.Game.is_completed == 0) &
            (models.Game.guild_id == ctx.guild.id) &
            (models.GameSide.team_chan > 0) &
            (models.GameSide.team_chan_external_server.is_null(True))
        ).tuples()

        game_side_channels = [gc[0] for gc in game_side_channels]
        common_game_channels = [gc[0] for gc in common_game_channels]

        logger.debug(f'game_side_channels: {game_side_channels}\ncommon_game_channels:{common_game_channels}')

        potential_channels = set(category_channels + common_game_channels + game_side_channels)
        channels = [chan for chan in ctx.guild.channels if chan.id in potential_channels]

        logger.debug(f'list of purge candidate channels: {channels}')

        await ctx.send(f'Returned {len(channels)} channels (of {len(potential_channels)} potential channels)')

        old_30d = (discord.utils.utcnow() + datetime.timedelta(days=-30))

        async def delete_channel(channel, game=None):
            nonlocal purged_channels
            logger.warning(f'Deleting channel {chan.name}')
            if not game:
                try:
                    logger.debug('Deleting channel with no associated game')
                    await chan.delete(reason='Purging game channels with inactive history')
                    purged_channels += 1
                except discord.DiscordException as e:
                    logger.error(f'Could not delete channel: {e}')
            else:
                models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'Game channel *{channel.name}* deleted during purge of unused or unneeded channels.')
                await game.delete_game_channels(self.bot.guilds, channel.guild.id, channel_id_to_delete=channel.id)
                purged_channels += 1

        async with ctx.typing():
            for chan in channels:
                logger.debug(f'Evaluating channel {chan.name} {chan.id} for deletion.')
                try:
                    game = models.Game.by_channel_id(chan_id=chan.id)
                except exceptions.MyBaseException:
                    logger.debug(f'Channel {chan.name} {chan.id} has no associated game. deleting.')
                    await ctx.send(f'Deleting channel **{chan.name}** - it has no associated game in the database')
                    await delete_channel(chan)
                    continue

                if chan.id in common_game_channels and current_number_of_channels > 425:
                    logger.debug(f'Channel {chan.name} {chan.id} is a common game channel, being purged since server is too full.')
                    await ctx.send(f'Deleting channel **{chan.name}** - it is a common game channel, being purged since server is too full.')
                    await delete_channel(chan, game)
                    await game.update_squad_channels(self.bot.guilds, game.guild_id, message='The central game channel for this game has been purged to free up room on the server')
                    continue
                if chan.last_message_id:
                    try:
                        # messages = await chan.history(limit=5, oldest_first=False).flatten()
                        messages = [message async for message in chan.history(limit=5, oldest_first=False)]
                    except discord.DiscordException as e:
                        logger.error(f'Could not load channel history: {e}')
                        continue
                    # if len(messages) > 3:
                    #     logger.debug(f'{chan.name} not eligible for deletion - has at least 4 messages in history')
                    #     continue
                    if messages[0].created_at > old_30d:
                        logger.debug(f'{chan.name} not eligible for deletion - has a recent message in history')
                        continue
                    logger.warning(f'{chan.name} {chan.id} is eligible for deletion - few messages and no recent messages in history')
                    await ctx.send(f'Deleting channel **{chan.name}** - few messages and no recent messages in history')
                    await delete_channel(chan, game)
                    models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'Game channel *{chan.name}* deleted during purge of unused or unneeded channels.')
                else:
                    logger.debug(f'Channel {chan.name} {chan.id} has no last_message_id. deleting.')
                    await delete_channel(chan, game)
                    continue

        await ctx.send(f'Channel cleanup complete. {purged_channels} channels purged.')

    @commands.command(aliases=['confirmgame'], usage='game_id')
    # async def confirm(self, ctx, winning_game: PolyGame = None):
    async def confirm(self, ctx, *, arg: str = None):
        """ *Staff*: List unconfirmed games, or let staff confirm winners
         **Examples**
        `[p]confirm` - List unconfirmed games
        `[p]confirm 5` - Confirms the winner of game 5 and performs ELO changes
        """

        if arg is None:
            # display list of unconfirmed games
            game_list = await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(
                    load_unconfirmed_game_summaries,
                    ctx.guild.id,
                ),
            )
            if len(game_list) == 0:
                return await ctx.send('No unconfirmed games found.')
            await utilities.paginate(self.bot, ctx, title=f'{len(game_list)} unconfirmed games', message_list=game_list, page_start=0, page_end=15, page_size=15)
            return

        if settings.elo_job_coordinator.is_active:
            logger.info('Skipping command due to active ELO job')
            return await ctx.send(f':warning: {ctx.author.mention} - I am currently recalculating the results of prior games. No new game results can be logged. Please try again in a few minutes.')

        if arg.lower() == 'auto':
            (unconfirmed_count, games_confirmed) = await self.confirm_auto(ctx.guild, ctx.prefix, ctx.channel)
            return await ctx.send(f'Autoconfirm process complete. {games_confirmed} games auto-confirmed. {unconfirmed_count - games_confirmed} games left unconfirmed.')

        # else confirming a specific game ie. $confirm 1234
        game_converter = PolyGame()
        winning_game = await game_converter.convert(ctx, arg)

        if not winning_game.is_completed:
            return await ctx.send(f'Game {winning_game.id} has no declared winner yet.')
        if winning_game.is_confirmed:
            return await ctx.send(f'Game with ID {winning_game.id} is already confirmed as completed with winner **{winning_game.winner.name()}**')

        try:
            async with ctx.typing():
                result = await self._confirm_game_and_post(
                    game_id=winning_game.id,
                    guild=ctx.guild,
                    prefix=ctx.prefix,
                    channel=ctx.channel,
                    requester=ctx.author,
                )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await ctx.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except elo_workers.WinValidationError as exc:
            return await ctx.send(str(exc))
        except exceptions.CheckFailedError as exc:
            return await ctx.send(f'*Error*: {exc}')
        except peewee.PeeweeException:
            logger.exception(
                'Database failure confirming game %s', winning_game.id
            )
            return await ctx.send(
                'Game confirmation failed and rolled back. No Discord '
                'channel updates were made.'
            )

        await ctx.send(
            f'**Game {result.game_id}** winner has been confirmed as '
            f'**{result.winner_name}**'
        )

    async def confirm_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        try:
            result = await self._confirm_game_and_post(
                game_id=game_id,
                guild=interaction.guild,
                prefix=prefix,
                channel=interaction.channel,
                requester=interaction.user,
            )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await interaction.followup.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except elo_workers.WinValidationError as exc:
            return await interaction.followup.send(str(exc))
        except exceptions.CheckFailedError as exc:
            return await interaction.followup.send(f'*Error*: {exc}')
        except peewee.PeeweeException:
            logger.exception(
                'Database failure confirming game %s from slash command',
                game_id,
            )
            return await interaction.followup.send(
                'Game confirmation failed and rolled back. No Discord '
                'channel updates were made.'
            )

        await interaction.followup.send(
            f'**Game {result.game_id}** winner has been confirmed as '
            f'**{result.winner_name}**'
        )

    async def confirm_auto(self, guild, prefix, current_channel):
        logger.info(f'in confirm_auto with guild {guild} prefix {prefix} current_channel {current_channel}')

        if settings.elo_job_coordinator.is_active:
            logger.info('Skipping confirm_auto due to active ELO job')
            return (0, 0)

        game_query = models.Game.search(status_filter=5, guild_id=guild.id).order_by(models.Game.win_claimed_ts)
        old_24h = (datetime.datetime.now() + datetime.timedelta(hours=-24))
        old_6h = (datetime.datetime.now() + datetime.timedelta(hours=-6))
        games_confirmed = 0
        unconfirmed_count = len(game_query)

        for game in game_query:

            logger.debug(f'auto_confirm checking game {game.id}')
            (confirmed_count, side_count, _) = game.confirmations_count()

            if not game.win_claimed_ts:
                logger.error(f'Game {game.id} does not have a value for win_claimed_ts - cannot auto confirm.')
                continue

            confirmation_reason = None
            if game.is_ranked and game.win_claimed_ts < old_24h:
                confirmation_reason = (
                    'Ranked win claimed more than 24 hours ago.'
                )
            elif not game.is_ranked and game.win_claimed_ts < old_6h:
                confirmation_reason = (
                    'Unranked win claimed more than 6 hours ago.'
                )
            elif side_count < 5 and confirmed_count > 1:
                confirmation_reason = 'Due to partial confirmations.'
            elif side_count >= 5 and confirmed_count > 2:
                confirmation_reason = 'Due to partial confirmations.'

            if confirmation_reason is None:
                continue

            try:
                result = await self._run_confirm_game_job(
                    game_id=game.id,
                    guild_id=guild.id,
                    requester_id=None,
                    requester_name='automatic confirmation task',
                )
            except exceptions.RecordLocked:
                logger.info(
                    'Cannot auto-confirm game %s - it is locked', game.id
                )
                continue
            except EloJobConflict:
                logger.info(
                    'Stopping auto-confirm because another ELO job started'
                )
                break
            except (
                elo_workers.WinValidationError,
                exceptions.CheckFailedError,
                peewee.PeeweeException,
            ):
                logger.exception('Could not auto-confirm game %s', game.id)
                continue

            confirmed_game = models.Game.load_full_game(result.game_id)
            await post_win_messaging(
                guild,
                prefix,
                current_channel,
                confirmed_game,
            )
            games_confirmed += 1
            await current_channel.send(
                f'Game {game.id} auto-confirmed. {confirmation_reason} '
                f'{confirmed_count} of {side_count} sides had confirmed.'
            )

        logger.debug(f'confirm_auto processed {unconfirmed_count} and confirmed {games_confirmed} games.')
        return (unconfirmed_count, games_confirmed)

    async def task_confirm_auto(self):
        await self.bot.wait_until_ready()
        sleep_cycle = (60 * 60 * 0.5)  # half hour cycle

        while not self.bot.is_closed():
            await asyncio.sleep(8)
            logger.debug('Task running: task_confirm_auto')

            if settings.elo_job_coordinator.is_active:
                logger.debug('Skipping task_confirm_auto while an ELO job is active.')
            else:
                utilities.connect()
                for guild in self.bot.guilds:
                    staff_output_channel = guild.get_channel(settings.guild_setting(guild.id, 'log_channel'))
                    if not staff_output_channel:
                        logger.debug(f'Could not load log_channel for server {guild.id} - skipping')
                        continue

                    logger.debug(f'Loaded log_channel for server {guild.id}')
                    prefix = settings.guild_setting(guild.id, 'command_prefix')
                    (unconfirmed_count, games_confirmed) = await self.confirm_auto(guild, prefix, staff_output_channel)
                    if games_confirmed:
                        await staff_output_channel.send(f'Autoconfirm process complete. {games_confirmed} games auto-confirmed. {unconfirmed_count - games_confirmed} games left unconfirmed.')
                        logger.debug(f'Autoconfirm process complete. {games_confirmed} games auto-confirmed. {unconfirmed_count - games_confirmed} games left unconfirmed.')
                    else:
                        logger.debug(f'No games_confirmed for guild {guild.id}')

            await asyncio.sleep(sleep_cycle)

    async def task_purge_incomplete(self):
        await self.bot.wait_until_ready()
        sleep_cycle = (60 * 60 * 5)  # 5 hour cycle

        while not self.bot.is_closed():
            await asyncio.sleep(900)
            logger.debug('Task running: task_purge_incomplete')
            as_of = datetime.date.today()
            for guild in self.bot.guilds:
                try:
                    await (
                        incomplete_game_purge
                        .purge_incomplete_games_for_guild(
                            bot=self.bot,
                            guild=guild,
                            as_of=as_of,
                        )
                    )
                except Exception:
                    logger.exception(
                        'Unhandled incomplete-game purge failure for guild %s',
                        guild.id,
                    )

            await asyncio.sleep(sleep_cycle)

    @commands.command(usage='game_id')
    async def rankset(self, ctx, game: PolyGame = None):
        """ *Staff*: Marks an incomplete game as ranked
        Turns an incomplete unranked game into a ranked game
         **Examples**
        `[p]rankset 50`
        """
        if game is None:
            return await ctx.send('No matching game was found.')

        try:
            message = await self._set_ranked_state_and_post(
                game_id=game.id,
                guild=ctx.guild,
                is_ranked=True,
                requester=ctx.author,
            )
        except game_workers.RankedStateValidationError as exc:
            return await ctx.send(str(exc))
        return await ctx.send(message)

    @commands.command(usage='game_id')
    async def rankunset(self, ctx, game: PolyGame = None):
        """ *Staff*: Marks an incomplete game as unranked
        Turns an incomplete ranked game into an unranked game
         **Examples**
        `[p]rankunset 50`
        """
        if game is None:
            return await ctx.send('No matching game was found.')

        try:
            message = await self._set_ranked_state_and_post(
                game_id=game.id,
                guild=ctx.guild,
                is_ranked=False,
                requester=ctx.author,
            )
        except game_workers.RankedStateValidationError as exc:
            return await ctx.send(str(exc))
        return await ctx.send(message)

    async def set_ranked_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        ranked: bool,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        # Successful competitive-state corrections are intentionally public
        # so server members can see the same audit-facing result as the
        # preserved prefix commands.
        await interaction.response.defer()
        try:
            message = await self._set_ranked_state_and_post(
                game_id=game_id,
                guild=interaction.guild,
                is_ranked=ranked,
                requester=interaction.user,
            )
        except game_workers.RankedStateValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except (peewee.PeeweeException, exceptions.RecordLocked):
            logger.exception('Failed ranked-state correction for %s', game_id)
            return await interaction.followup.send(
                'Ranked-state correction failed; no Discord update was made.',
                ephemeral=True,
            )
        await interaction.followup.send(message)

    @settings.in_bot_channel()
    @commands.command(usage='game_id')
    async def unstart(self, ctx, game: PolyGame = None):
        """ *Staff*: Resets an in progress game to a pending matchmaking sesson

         **Examples**
        `[p]unstart 1234`
        """

        if game is None:
            return await ctx.send('No matching game was found.')

        if game.uses_channel_id(ctx.channel.id):
            return await ctx.send(':warning: This command must be used from a channel that is not related to the game.')

        try:
            message = await self._unstart_game_and_post(
                game_id=game.id,
                guild=ctx.guild,
                prefix=ctx.prefix,
                requester=ctx.author,
                invocation_channel_id=ctx.channel.id,
            )
        except game_workers.GameUnstartValidationError as exc:
            return await ctx.send(str(exc))
        await ctx.send(message)

    async def unstart_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        try:
            message = await self._unstart_game_and_post(
                game_id=game_id,
                guild=interaction.guild,
                prefix=prefix,
                requester=interaction.user,
                invocation_channel_id=interaction.channel_id,
                invoked_with='/game manage unstart',
            )
        except game_workers.GameUnstartValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except exceptions.RecordLocked as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Failed to unstart game %s', game_id)
            return await interaction.followup.send(
                'Game restoration failed and rolled back. No Discord cleanup '
                'was performed.',
                ephemeral=True,
            )
        await interaction.followup.send(message)

    @commands.command(usage='game_id')
    async def extend(self, ctx, game: PolyGame = None):
        """ *Staff*: Extends the timer of an open game by 24 hours

         **Examples**
        `[p]extend 1234`
        """

        if not game:
            return await ctx.send('No game ID provided.')

        try:
            result = await self._extend_pending_game(
                game_id=game.id,
                guild_id=ctx.guild.id,
                requester=ctx.author,
            )
        except game_workers.GameExtensionValidationError as exc:
            return await ctx.send(str(exc))
        return await ctx.send(
            f'Game {result.game_id}\'s deadline has been extended to '
            f'**{result.new_expiration}**. Previous expiration was '
            f'**{result.old_expiration}**.'
        )

    async def extend_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        await interaction.response.defer()
        try:
            result = await self._extend_pending_game(
                game_id=game_id,
                guild_id=interaction.guild.id,
                requester=interaction.user,
            )
        except game_workers.GameExtensionValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except exceptions.RecordLocked as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Failed pending-game extension for %s', game_id)
            return await interaction.followup.send(
                'Game extension failed and rolled back.',
                ephemeral=True,
            )
        await interaction.followup.send(
            f'Game {result.game_id}\'s deadline has been extended to '
            f'**{result.new_expiration}**. Previous expiration was '
            f'**{result.old_expiration}**.'
        )

    @commands.command(usage='tribe_name new_emoji')
    @commands.is_owner()
    async def tribe_emoji(self, ctx, tribe_name: str, emoji):
        """*Mod*: Assign an emoji to a tribe
        The emoji chosen will be used on *all* servers that this bot is on.
        It can only be triggered by an admin on a server that contributes to the Global ELO leaderboard.
        **Example:**
        `[p]tribe_emoji Bardur :new_bardur_emoji:`
        """
        if not settings.guild_setting(ctx.guild.id, 'include_in_global_lb') and ctx.author.id != settings.owner_id:
            return await ctx.send('This command can only be run in a Global ELO server (ie. PolyChampions or Polytopia Main')

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}tribe_emoji Tribename :my_custom_emoji:`'.format(ctx.prefix))

        try:
            tribe = models.Tribe.update_emoji(name=tribe_name, emoji=emoji)
        except exceptions.CheckFailedError as e:
            return await ctx.send(e)

        await ctx.send(f'Tribe {tribe.name} updated with new emoji: {tribe.emoji}')

    @team_group.command(
        name='create',
        description='Create a competitive team.',
    )
    @discord.app_commands.describe(
        name='Team name; this becomes the exact Discord membership role name.',
    )
    async def team_create_slash(
        self,
        interaction: discord.Interaction,
        name: str,
    ):
        """Create one team and its actor-attributed audit entry after commit."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_creation_service.native_access_error(
            interaction.user,
            guild_id,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        actor = team_creation_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            request = team_creation_service.build_request(
                member=interaction.user,
                guild_id=guild_id,
                name=name,
                native=True,
                invoked_with='/team create',
            )
            result = await team_creation_service.run_create(request)
            await team_creation_service.publish_success(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
            )
            return result
        except team_creation_workers.TeamCreationValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team create command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team creation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team create failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team creation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='show',
        description='View a competitive team card and roster history.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
    )
    async def team_show_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
    ):
        """Publish the shared dense team card from a bounded read worker."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        channel_id = getattr(interaction, 'channel_id', None)
        if channel_id is None:
            channel_id = getattr(getattr(interaction, 'channel', None), 'id', None)
        access_error = team_show_service.native_access_error(
            interaction.user,
            int(guild_id),
            channel_id,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            request = team_show_service.build_request(
                member=interaction.user,
                guild=interaction.guild,
                team_lookup=team,
                activity_mode=team_show_workers.TEAM_ACTIVITY_RECENT,
                native=True,
                invoked_with='/team show',
                prefix=str(
                    settings.guild_setting(int(guild_id), 'command_prefix')
                ),
                channel_id=channel_id,
            )
            result = await team_show_service.run(request)
            return await team_show_service.publish_native(interaction, result)
        except team_show_workers.TeamShowLookupError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except team_show_workers.TeamShowPermissionError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team show command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'The team card could not be loaded because the database read '
                'failed.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team show failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'The team card could not be loaded. Please try again.',
                ephemeral=True,
            )

    @commands.command(usage='team_name new_emoji')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_emoji(self, ctx, team_name: str, emoji: str = None):
        """*Mod*: Assign an emoji to a team
        **Example:**
        `[p]team_emoji Ronin :my_fancy_emoji:` - Set new emoji. Currently requires a custom emoji.
        `[p]team_emoji Ronin` - Display currently saved emoji
        """

        if emoji is not None and not team_emoji_workers.is_valid_emoji(emoji):
            return await ctx.send(f'Valid emoji not detected. Example: `{ctx.prefix}team_emoji name :my_custom_emoji:`')

        try:
            if emoji is None:
                result = await team_emoji_service.run_read(
                    team_emoji_service.build_read_request(
                        member=ctx.author,
                        guild_id=ctx.guild.id,
                        team_lookup=team_name,
                        invoked_with=ctx.invoked_with,
                    )
                )
                return await ctx.send(
                    team_emoji_service.legacy_read_message(result),
                )

            request = team_emoji_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                team_lookup=team_name,
                emoji=emoji,
                native=False,
                invoked_with=ctx.invoked_with,
            )

            async def publish(result):
                await team_emoji_service.publish_mutation_result(
                    result,
                    send=ctx.send,
                )

            await team_emoji_service.run_mutation(
                request,
                after_commit=publish,
            )
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_emoji name :my_custom_emoji:`')
        except team_emoji_workers.TeamEmojiLookupError as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_emoji name :my_custom_emoji:`')
        except team_emoji_workers.TeamEmojiValidationError as ex:
            return await ctx.send(str(ex))
        except peewee.PeeweeException:
            logger.exception(
                'Database failure reading or updating team emoji for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team emoji operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team emoji failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team emoji operation failed and rolled back.')

    @team_group.command(
        name='emoji',
        description='View or update a team emoji.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        emoji='Unicode or custom emoji to set; omit this to view it.',
        clear='Explicitly remove the configured team emoji.',
    )
    async def team_emoji_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        emoji: str | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team emoji with post-commit public output."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        try:
            teams_enabled = bool(
                settings.guild_setting(guild_id, 'allow_teams')
            )
            requester_is_mod = bool(settings.is_mod(interaction.user))
        except (AttributeError, TypeError, exceptions.CheckFailedError):
            return await interaction.response.send_message(
                'This command is not available on this server.',
                ephemeral=True,
            )
        if not teams_enabled:
            return await interaction.response.send_message(
                'Teams are not enabled on this server.',
                ephemeral=True,
            )
        if not requester_is_mod:
            return await interaction.response.send_message(
                'You do not have permission to manage team emojis.',
                ephemeral=True,
            )
        if clear and emoji is not None:
            return await interaction.response.send_message(
                'Choose either an emoji or `clear`, not both.',
                ephemeral=True,
            )
        if emoji is not None and not team_emoji_workers.is_valid_emoji(emoji):
            return await interaction.response.send_message(
                'Valid Unicode or custom emoji syntax was not detected.',
                ephemeral=True,
            )

        actor = team_emoji_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if emoji is None and not clear:
                result = await team_emoji_service.run_read(
                    team_emoji_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        team_lookup=team,
                        invoked_with='/team emoji',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_emoji_service.read_message(result, actor=actor)
                )
                return result

            request = team_emoji_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                team_lookup=team,
                emoji=emoji,
                clear=clear,
                native=True,
                invoked_with='/team emoji',
            )
            result = await team_emoji_service.run_mutation(request)
            await team_emoji_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(
                    interaction
                ),
                actor=actor,
            )
            return result
        except team_emoji_workers.TeamEmojiValidationError as ex:
            return await interaction.followup.send(str(ex), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team emoji command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team emoji operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team emoji failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team emoji operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='name',
        description='View or update a team name.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        name='Replacement name; omit this to view the current name.',
    )
    async def team_name_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        name: str | None = None,
    ):
        """Read or mod-edit one team name with public committed output."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_NAME,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if name is None:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_NAME,
                        team_lookup=team,
                        invoked_with='/team name',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_attributes_service.read_message(result, actor=actor)
                )
                return result

            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_NAME,
                team_lookup=team,
                name=name,
                native=True,
                invoked_with='/team name',
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team name command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team name operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team name failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team name operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='server',
        description='View or update a team external server ID.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        server_id='Raw external Discord server ID; omit this to view it.',
        clear='Explicitly clear the nullable external server ID.',
    )
    async def team_server_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        server_id: int | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team external server ID."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )
        if clear and server_id is not None:
            return await interaction.response.send_message(
                'Choose either a server ID or `clear`, not both.',
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if server_id is None and not clear:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                        team_lookup=team,
                        invoked_with='/team server',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_attributes_service.read_message(result, actor=actor)
                )
                return result

            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                team_lookup=team,
                server_id=server_id,
                clear=clear,
                native=True,
                invoked_with='/team server',
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team server command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team server operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team server failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team server operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='tier',
        description='View or update a team tier.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.choices(
        tier=team_attributes_service.TEAM_TIER_CHOICES,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        tier='Configured tier number; omit this to view the current tier.',
    )
    async def team_tier_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        tier: str | None = None,
    ):
        """Read or mod-edit a team tier with post-commit role refresh."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_TIER,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if tier is None:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_TIER,
                        team_lookup=team,
                        invoked_with='/team tier',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_attributes_service.read_message(
                        result,
                        actor=actor,
                    )
                )
                return result

            preflight = await team_attributes_service.run_tier_preflight(
                member=interaction.user,
                guild=interaction.guild,
                team_lookup=team,
                invoked_with='/team tier',
            )

            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_TIER,
                team_lookup=team,
                tier=tier,
                expected_team_id=preflight.current.team_id,
                expected_value=preflight.current.value,
                expected_value_present=True,
                team_role_id=preflight.team_role_id,
                team_role_name=preflight.team_role_name,
                team_member_ids=preflight.member_ids,
                native=True,
                invoked_with='/team tier',
            )
            result = await team_attributes_service.run_mutation(request)
            try:
                reconciliation = await team_attributes_service.reconcile_tier_roles(
                    interaction.guild,
                    result,
                )
            except Exception:
                logger.exception(
                    'Committed team tier %s could not reconcile roles',
                    result.team_id,
                )
                reconciliation = team_attributes_service.TierRoleReconciliation(
                    team_id=result.team_id,
                    attempted=0,
                    updated=0,
                    team_role_missing=True,
                )
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
                reconciliation=reconciliation,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team tier command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team tier operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team tier failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team tier operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='house',
        description='View or update a team House affiliation.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_house_teams,
        house=team_attributes_service.autocomplete_houses,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        house='House name to assign; omit this to view the current House.',
        clear='Explicitly remove the team House affiliation.',
    )
    async def team_house_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        house: str | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team House affiliation after commit."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        mutation = house is not None or clear
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
            mutation=mutation,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )
        if clear and house is not None:
            return await interaction.response.send_message(
                'Choose either a House or `clear`, not both.',
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if not mutation:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
                        team_lookup=team,
                        invoked_with='/team house',
                    )
                )
                await team_emoji_service.public_interaction_sender(interaction)(
                    team_attributes_service.read_message(result, actor=actor)
                )
                return result

            preflight = await team_attributes_service.run_house_preflight(
                member=interaction.user,
                guild=interaction.guild,
                team_lookup=team,
                invoked_with='/team house',
            )
            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
                team_lookup=team,
                house=house,
                clear=clear,
                expected_team_id=preflight.current.team_id,
                expected_value=preflight.current.value,
                expected_value_present=True,
                team_role_id=preflight.team_role_id,
                team_role_name=preflight.team_role_name,
                team_member_ids=preflight.member_ids,
                native=True,
                invoked_with='/team house',
            )
            result = await team_attributes_service.run_mutation(request)
            try:
                reconciliation = await team_attributes_service.reconcile_tier_roles(
                    interaction.guild,
                    result,
                )
            except Exception:
                logger.exception(
                    'Committed team house %s could not reconcile roles',
                    result.team_id,
                )
                reconciliation = team_attributes_service.TierRoleReconciliation(
                    team_id=result.team_id,
                    attempted=0,
                    updated=0,
                    team_role_missing=True,
                    attribute=team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
                )
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
                reconciliation=reconciliation,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team house command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team House operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team house failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team House operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='image',
        description='View or update a team image.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        image='PNG, JPEG, or WebP attachment; omit this to view the image.',
        clear='Explicitly clear the team image.',
    )
    async def team_image_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        image: discord.Attachment | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team image with staged publication."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_image_service.native_access_error(
            interaction.user,
            guild_id,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )
        if clear and image is not None:
            return await interaction.response.send_message(
                'Choose either an image replacement or `clear`, not both.',
                ephemeral=True,
            )

        actor = team_image_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            current = await team_image_service.run_read(
                team_image_service.build_read_request(
                    member=interaction.user,
                    guild_id=guild_id,
                    team_lookup=team,
                    invoked_with='/team image',
                )
            )
            if image is None and not clear:
                await team_image_service.publish_read(
                    current,
                    send=team_image_service.public_interaction_sender(
                        interaction,
                    ),
                    actor=actor,
                )
                return current

            staged = None
            operation = team_image_workers.TEAM_IMAGE_CLEAR
            if image is not None:
                staged = await team_image_service.stage_attachment(
                    image,
                    team_id=current.team_id,
                )
                operation = team_image_workers.TEAM_IMAGE_LOCAL
            request = team_image_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                team_id=current.team_id,
                operation=operation,
                staged_path=(staged.path if staged is not None else None),
                expected_image_url=current.image_url,
                expected_local_digest=current.local_digest,
                native=True,
                invoked_with='/team image',
            )
            result = await team_image_service.run_mutation(
                request,
                staged=staged,
            )
            await team_image_service.publish_mutation_result(
                result,
                send=team_image_service.public_interaction_sender(
                    interaction,
                ),
                actor=actor,
            )
            return result
        except team_image_service.TeamImagePublicationError as exc:
            logger.exception(
                'Committed native team image %s requires publication '
                'reconciliation for guild %s',
                exc.result.team_id,
                guild_id,
            )
            warning = team_image_service.publication_failure_message(
                exc,
                actor=actor,
            )
            try:
                await team_image_service.public_interaction_sender(
                    interaction,
                )(warning)
            except Exception:
                logger.exception(
                    'Committed native team image %s could not publish its '
                    'public reconciliation warning; using an ephemeral '
                    'fallback for guild %s',
                    exc.result.team_id,
                    guild_id,
                )
                return await interaction.followup.send(
                    warning,
                    ephemeral=True,
                )
            return None
        except team_image_workers.TeamImageValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except (team_image_service.TeamImageDownloadError, image_storage.ImageStorageError) as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team image command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team image operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team image failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team image operation failed and rolled back.',
                ephemeral=True,
            )
    @commands.command(usage='team_name [image_url or attachment]')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_image(self, ctx, team_name: str, image_url: str = None):
        """*Mod*: Set a team's logo image

        **Example:**
        `[p]team_image Ronin http://www.path.to/image.png`
        `[p]team_image Ronin` with an attached PNG, JPEG, or WebP image
        `[p]team_image Ronin` - Display currently saved image
        """
        attachments = ctx.message.attachments
        if len(attachments) > 1:
            return await ctx.send('Please attach exactly one image.')
        try:
            current = await team_image_service.run_read(
                team_image_service.build_read_request(
                    member=ctx.author,
                    guild_id=ctx.guild.id,
                    team_lookup=team_name,
                    invoked_with=getattr(ctx, 'invoked_with', 'team_image'),
                )
            )
            if not attachments and not image_url:
                return await team_image_service.publish_legacy_read(
                    current,
                    send=ctx.send,
                )

            staged = None
            operation = team_image_workers.TEAM_IMAGE_URL
            if attachments:
                staged = await team_image_service.stage_attachment(
                    attachments[0],
                    team_id=current.team_id,
                )
                operation = team_image_workers.TEAM_IMAGE_LOCAL
            request = team_image_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                team_id=current.team_id,
                operation=operation,
                image_url=(image_url if operation == team_image_workers.TEAM_IMAGE_URL else None),
                staged_path=(staged.path if staged is not None else None),
                expected_image_url=current.image_url,
                expected_local_digest=current.local_digest,
                ignored_url=bool(attachments and image_url),
                native=False,
                invoked_with=getattr(ctx, 'invoked_with', 'team_image'),
            )
            result = await team_image_service.run_mutation(
                request,
                staged=staged,
            )
            return await team_image_service.publish_mutation_result(
                result,
                send=ctx.send,
            )
        except team_image_service.TeamImagePublicationError as exc:
            logger.exception(
                'Committed prefix team image %s requires publication '
                'reconciliation for guild %s',
                exc.result.team_id,
                ctx.guild.id,
            )
            return await ctx.send(
                team_image_service.publication_failure_message(
                    exc,
                    actor=team_image_service.capture_actor(ctx.author),
                )
            )
        except team_image_workers.TeamImageLookupError as exc:
            return await ctx.send(
                f'{exc}\nExample: `{ctx.prefix}team_image name '
                'http://url_to_image.png`'
            )
        except team_image_workers.TeamImageValidationError as exc:
            return await ctx.send(str(exc))
        except (team_image_service.TeamImageDownloadError, image_storage.ImageStorageError) as exc:
            if image_url and not attachments:
                return await ctx.send(
                    f'{exc} Example: `{ctx.prefix}team_image name '
                    'http://url_to_image.png`'
                )
            return await ctx.send(f'Unable to save team image: {exc}')
        except peewee.PeeweeException:
            logger.exception(
                'Database failure reading or updating team image for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team image operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team image failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team image operation failed and rolled back.')

    @commands.command(usage='old_name new_name')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_name(self, ctx, old_team_name: str, new_team_name: str):
        """*Mod*: Change a team's name
        The team should have a Role with an identical name.
        Old name doesn't need to be precise, but new name does. Include quotes if it's more than one word.
        **Example:**
        `[p]team_name Amazeballs "The Wowbaggers"`
        """

        try:
            request = team_attributes_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_NAME,
                team_lookup=old_team_name,
                name=new_team_name,
                native=False,
                invoked_with=ctx.invoked_with,
                prefix=ctx.prefix,
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=ctx.send,
            )
        except team_attributes_workers.TeamAttributeValidationError as ex:
            return await ctx.send(
                f'{ex}\nExample: `{ctx.prefix}team_name "Current name" '
                '"New Team Name"`'
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure reading or updating team name for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team name operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team name failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team name operation failed and rolled back.')

    @commands.command(usage='team_name server_id')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_server(self, ctx, team_name: str = None, team_server_id: str = None):
        """*Mod*: Change a team's external server

        **Example:**
        `[p]team_server Ronin` Check existing server setting
        `[p]team_server Ronin 572885616656908288` Update the server setting
        """
        # TODO: better input handling (display server_id if new ID not provided)
        example = (
            f'`{ctx.prefix}team_server "Team Name" '
            '447883341463814144` (Use the raw numeric ID of the team\'s server)'
        )
        if not team_name:
            return await ctx.send(f'Example: {example}')

        if not team_server_id:
            try:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=ctx.author,
                        guild_id=ctx.guild.id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                        team_lookup=team_name,
                        invoked_with=ctx.invoked_with,
                    )
                )
            except team_attributes_workers.TeamAttributeValidationError as ex:
                return await ctx.send(f'{ex}\nExample: {example}')
            except peewee.PeeweeException:
                logger.exception(
                    'Database failure reading team server for guild %s',
                    ctx.guild.id,
                )
                return await ctx.send(
                    'Team server operation failed and rolled back.'
                )
            return await ctx.send(
                team_attributes_service.legacy_server_read_message(result)
            )

        try:
            server_id = int(team_server_id)
        except (TypeError, ValueError):
            return await ctx.send(
                f'Server ID was invalid.\nExample: {example}'
            )

        try:
            request = team_attributes_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                team_lookup=team_name,
                server_id=server_id,
                native=False,
                invoked_with=ctx.invoked_with,
                prefix=ctx.prefix,
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=ctx.send,
            )
        except team_attributes_workers.TeamAttributeValidationError as ex:
            return await ctx.send(f'{ex}\nExample: {example}')
        except peewee.PeeweeException:
            logger.exception(
                'Database failure updating team server for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team server operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team server failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team server operation failed and rolled back.')

    @commands.command(usage='@Player <New Trophies Value>', hidden=True)
    @settings.is_mod_check()
    async def ptrophies(self, ctx, *, args=None):
        """*Mod*: Set the trophies earned during the 2021 Polympics. Can only be used by mods on the server that the bot has tagged as named "Polympics"

        **Example:**
        `[p]ptrophies @Nelluk` 🥇🥈🥈🥉
        `[p]ptrophies @koric None` - Clear existing trophies
        """

        if settings.guild_setting(ctx.guild.id, 'display_name') != 'Polympics' and settings.get_user_level(ctx.author) < 7:
            return await ctx.send('This command must be used from the "Polympics" server or by the bot owner.')

        usage = f'**Example Usage**: {ctx.prefix}{ctx.invoked_with} @PlayerName 🥇🥈🥈🥉\nUse "None" to clear trophies.\nPlayer can be a raw discord ID. Command must be used by a mod on Polympics server.'
        args = args.split() if args else []
        if len(args) != 2:
            return await ctx.send(f'Wrong number of arguments.\n{usage}')

        p_id = utilities.string_to_user_id(args[0])
        if not p_id:
            return await ctx.send(f'Could not parse a discord ID or player mention.\n{usage}')

        trophies_key = 'polympics2021'

        try:
            dm = models.DiscordMember.select().where(models.DiscordMember.discord_id == p_id).get()
            if dm.trophies:
                old_trophies = dm.trophies.get(trophies_key, None)
            else:
                old_trophies = None
        except peewee.DoesNotExist:
            return await ctx.send(f'Could not find a DiscordMember in the database matching discord id `{p_id}`')

        if args[1].upper() == 'NONE':
            new_trophies = None
        else:
            new_trophies = str(args[1])

        logger.debug(f'Attempting to update Polympics 2021 trophies of user {dm.name} from {old_trophies} to {new_trophies}')
        if new_trophies:
            if dm.trophies:
                dm.trophies[trophies_key] = new_trophies
            else:
                dm.trophies = {trophies_key: new_trophies}
        else:
            if dm.trophies and trophies_key in dm.trophies:
                del dm.trophies[trophies_key]
                
        if not dm.trophies:
            dm.trophies = None
        dm.save()

        await ctx.send(f'Polympics 2021 trophies field for *{dm.name}* updated with new value "{new_trophies}". The previous value was "{old_trophies}".')
   
    
    @commands.command(aliases=['boost_from_norole'])
    @commands.is_owner()
    async def boost_from(self, ctx, p_string: str):
        """*Owner*: Award booster roles to a member who has donated
        Use a @Mention or raw user ID as an argument. This will attempt to set the role on ALL servers the bot shares with the player.
        It will look for a role that contains the word 'ELO' and 'BOOST' in the name. It will also mark the player as a booster in the database.

        **Examples**
        `[p]boost_from @FrontDoor Matt`

        Use `[p]boost_from_norole` to skip the role setting (for the shy).
        """

        p_id = utilities.string_to_user_id(p_string)
        if not p_id:
            return await ctx.send(f'Could not parse a discord ID. Usage: `{ctx.prefix}{ctx.invoked_with} @BoostingUser`')

        try:
            dm = models.DiscordMember.select().where(models.DiscordMember.discord_id == p_id).get()
        except peewee.DoesNotExist:
            return await ctx.send(f'Could not find a DiscordMember in the database matching discord id `{p_id}`')

        if ctx.invoked_with == 'boost_from_norole':
            counter = 0
        else:
            counter = await achievements.award_booster_role(dm)

        dm.boost_level = 1
        dm.save()

        await ctx.send(f'Marking **{dm.name}** as a booster and successfully applied the role across {counter} server(s).')

    @commands.command(hidden=True)
    @commands.is_owner()
    async def recalc_games_from(self, ctx, *, arg: str = None):
        """*Owner*: Recalculate games from a specific timestamp

        Give a game ID, and the bot will *recalculate_elo_since* all games completed after that game was completed.
        """

        try:
            game_id = int(arg)
        except (TypeError, ValueError):
            return await ctx.send(f'no game found for id {arg}')

        await ctx.send('This may take a while...')
        try:
            async with ctx.typing():
                timestamp = await self._run_recalculation_job(
                    game_id=game_id,
                    requester_id=ctx.author.id,
                    requester_name=ctx.author.display_name,
                )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await ctx.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except elo_workers.RecalculationValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception(
                'Database failure recalculating games from %s', game_id
            )
            return await ctx.send('Database recalculation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected failure recalculating games from %s', game_id
            )
            return await ctx.send('Database recalculation failed and rolled back.')

        await ctx.send(f'DB has been refreshed from {timestamp} onward')

    @elo_group.command(
        name='recalculate',
        description='Recalculate ELO from a completed game onward.',
    )
    @discord.app_commands.describe(
        game_id='Completed game that establishes the recalculation timestamp.',
        confirm='Must be true to start this destructive maintenance job.',
    )
    async def recalc_games_from_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        confirm: bool,
    ):
        if interaction.user.id != settings.owner_id:
            return await interaction.response.send_message(
                'Only the bot owner can use this command.',
                ephemeral=True,
            )
        if not confirm:
            return await interaction.response.send_message(
                'Recalculation was not started. Set `confirm` to true after '
                'checking the game ID.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            timestamp = await self._run_recalculation_job(
                game_id=game_id,
                requester_id=interaction.user.id,
                requester_name=interaction.user.display_name,
            )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await interaction.followup.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.',
                ephemeral=True,
            )
        except elo_workers.RecalculationValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure recalculating games from %s from slash',
                game_id,
            )
            return await interaction.followup.send(
                'Database recalculation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected failure recalculating games from %s from slash',
                game_id,
            )
            return await interaction.followup.send(
                'Database recalculation failed and rolled back.',
                ephemeral=True,
            )

        await interaction.followup.send(
            f'DB has been refreshed from {timestamp} onward.',
            ephemeral=True,
        )

    @elo_group.command(
        name='status',
        description='Show the currently running ELO mutation job.',
    )
    async def elo_job_status_slash(
        self,
        interaction: discord.Interaction,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        await interaction.response.send_message(
            format_elo_job_status(settings.elo_job_coordinator.active_job),
            ephemeral=True,
        )

    @commands.command(aliases=['migrate'])
    @settings.is_superuser_check()
    async def migrate_player(self, ctx, from_string: str, to_string: str):
        """*Owner*: Migrate games from player's old account to new account
        Target player cannot have any completed games associated with their profile. Use a @Mention or raw user ID as an argument.

        **Examples**
        `[p]migrate_player @NellukOld @NellukNew`
        """

        from_id, to_id = utilities.string_to_user_id(from_string), utilities.string_to_user_id(to_string)
        if not from_id or not to_id:
            return await ctx.send(f'Could not parse a discord ID. Usage: `{ctx.prefix}{ctx.invoked_with} @FromUser @ToUser`')

        try:
            old_discord_member = models.DiscordMember.select().where(models.DiscordMember.discord_id == from_id).get()
            old_name = old_discord_member.name
        except peewee.DoesNotExist:
            return await ctx.send(f'Could not find a DiscordMember in the database matching discord id `{from_id}`')

        new_guild_member = discord.utils.get(ctx.guild.members, id=to_id)
        if not new_guild_member:
            return await ctx.send(f'Could not find a guild member matching ID {to_id}. The migration must be to an existing member of this server.')

        new_discord_member = models.DiscordMember.get_or_none(discord_id=new_guild_member.id)
        if new_discord_member:
            # New player is already registered with the bot
            if new_discord_member.completed_game_count(only_ranked=False) > 0:
                return await ctx.send(f'Found a DiscordMember *{new_discord_member.name}* in the database matching discord id `{new_guild_member.id}`. Cannot migrate to an existing player with completed games!')

            # but has no completed games - proceeding to migrate
            logger.warning(f'Migrating player profile of ID {from_id} {old_discord_member.name} to new guild member {new_guild_member.id}{new_guild_member.name} with existing incomplete games')

            with models.db.atomic():
                for gm in new_discord_member.guildmembers:
                    old_gm = models.Player.get_or_none(discord_member=old_discord_member, guild_id=gm.guild_id)
                    if old_gm:
                        # Both old account and new account are registered in this guild
                        for l in gm.lineup:
                            # cycle through new incomplete games and switch to the old player
                            l.player = old_gm
                            l.save()
                    else:
                        # New account in this guild but old account not
                        # associate its player in this guild with the old account
                        gm.discord_member = old_discord_member
                        gm.save()

                new_discord_member.delete_instance()

                # set old account with new discord ID and refresh name
                old_discord_member.discord_id = new_guild_member.id
                old_discord_member.save()
                old_discord_member.update_name(new_name=new_guild_member.name)

            await ctx.send('Migration complete!')

        else:
            # New player has no presence in the bot
            logger.warning(f'Migrating player profile of ID {from_id} {old_discord_member.name} to new guild member {new_guild_member.id}{new_guild_member.name}')

            await ctx.send(f'The games from DiscordMember `{from_id}` *{old_discord_member.name}* will be migrated and become associated with {new_guild_member.mention}')

            with models.db.atomic():

                old_discord_member.discord_id = new_guild_member.id
                old_discord_member.save()
                old_discord_member.update_name(new_name=new_guild_member.name)

            await ctx.send('Migration complete!')

        models.GameLog.write(game_id=0, guild_id=0, message=f'**{ctx.author.display_name}** migrated old ELO player **{old_name}** `{from_id}` to {models.GameLog.member_string(new_guild_member)}')

    @commands.command(aliases=['delplayer'])
    @commands.is_owner()
    async def delete_player(self, ctx, *, args=None):
        """*Owner*: Delete a player entry from the bot's database
        Target player cannot have any games associated with their profile. Use a @Mention or raw user ID as an argument.

        **Examples**
        [p]delete_player @Nelluk
        [p]delete_player 272510639124250625
        """

        player_id = utilities.string_to_user_id(args)
        if not player_id:
            return await ctx.send(f'Could not parse a discord ID. Usage: `{ctx.prefix}{ctx.invoked_with} [<@Mention> / <Raw ID>]`')

        discord_member = models.DiscordMember.get_or_none(discord_id=player_id)
        if not discord_member:
            return await ctx.send(f'Could not find a DiscordMember in the database matching discord id `{player_id}`')

        player_games = discord_member.games_played(in_days=None).count()

        if player_games > 0:
            return await ctx.send(f'DiscordMember {discord_member.name} was found but has {player_games} associated ELO games. Can only delete players with zero games.')

        name = discord_member.name
        discord_member.delete_instance()
        await ctx.send(f'Deleting DiscordMember {name} with discord ID `{player_id}` from ELO database. They have zero games associated with their profile.')

    @commands.command(aliases=['dbb'])
    @commands.is_owner()
    async def backup_db(self, ctx):
        """*Owner*: Backup PSQL database to a file
        """
        import subprocess
        from subprocess import PIPE

        async with ctx.typing():
            await ctx.send('Executing backup script')
            process = subprocess.run(['/home/nelluk/backup_db.sh'], stdout=PIPE, stderr=PIPE)
            if process.returncode == 0:
                logger.info('Backup script executed')
                return await ctx.send(f'Execution successful: {str(process.stdout)}')
            else:
                logger.error('Error during execution')
                return await ctx.send(f'Error during execution: {str(process.stderr)}')


async def setup(bot):
    await bot.add_cog(administration(bot))
