from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import settings
import logging
import peewee
import modules.exceptions as exceptions
import datetime
import asyncio
import discord
from modules.games import PolyGame, post_win_messaging

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')


class administration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = bot.loop.create_task(self.task_confirm_auto())
            self.bg_task2 = bot.loop.create_task(self.task_purge_incomplete())

    async def cog_check(self, ctx):

        if settings.is_staff(ctx):
            return True
        else:
            if ctx.invoked_with == 'help' and ctx.command.name != 'help':
                return False
            else:
                await ctx.send('You do not have permission to use this command.')
                return False

    @commands.is_owner()
    @commands.command()
    async def quit(self, ctx):
        """ *Owner*: Close database connection and quit bot gracefully """

        await ctx.send('Shutting down')
        await self.bot.close()
        message = ''

        # TODO: use this clean quit to delete messages currently waiting for deletion (misc.task_broadcast_newbie_message())
        # would need to store the message/channel IDs in memory as they are created (not sure how to purge the ones that are being deleted)
        try:
            if models.db.close():
                message = 'db connecton closing normally'
            else:
                message = 'db connection was already closed'

        except peewee.PeeweeException as e:
            message = f'Error during post_invoke_cleanup db.close(): {e}'
        finally:
            logger.info(message)
            print(message)

    @settings.is_mod_check()
    @commands.command()
    async def purge_game_channels(self, ctx, *, arg: str = None):
        # TODO: Remove references to deleted channels from database.  i dont -think- having ghost references will cause any usability problems

        if not settings.guild_setting(ctx.guild.id, 'game_channel_categories'):
            return await ctx.send(f'Cannot purge - this guild has no `game_channel_categories` setting')

        channels = [chan for chan in ctx.guild.channels if chan.category_id in settings.guild_setting(ctx.guild.id, 'game_channel_categories')]
        await ctx.send(f'Returned {len(channels)} channels')
        old_30d = (datetime.datetime.today() + datetime.timedelta(days=-30))

        for chan in channels:
            if chan.last_message_id:
                try:
                    messages = await chan.history(limit=5, oldest_first=False).flatten()
                except discord.DiscordException as e:
                    logger.error(f'Could not load channel history: {e}')
                    continue
                if len(messages) > 3:
                    logger.debug(f'{chan.name} not eligible for deletion - has at least 4 messages in history')
                    continue
                if messages[0].created_at > old_30d:
                    logger.debug(f'{chan.name} not eligible for deletion - has a recent message in history')
                    continue
                logger.warn(f'{chan.name} {chan.id} is eligible for deletion - few messages and no recent messages in history')
                await ctx.send(f'Deleting channel **{chan.name}** - few messages and no recent messages in history')
                try:
                    logger.warn(f'Deleting channel {chan.name}')
                    await chan.delete(reason='Purging game channels with inactive history')
                except discord.DiscordException as e:
                    logger.error(f'Could not delete channel: {e}')

        await ctx.send(f'Channel cleanup complete')

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
            game_query = models.Game.search(status_filter=5, guild_id=ctx.guild.id).order_by(models.Game.win_claimed_ts)
            game_list = utilities.summarize_game_list(game_query)
            if len(game_list) == 0:
                return await ctx.send(f'No unconfirmed games found.')
            await utilities.paginate(self.bot, ctx, title=f'{len(game_list)} unconfirmed games', message_list=game_list, page_start=0, page_end=15, page_size=15)
            return

        if arg.lower() == 'auto':
            (unconfirmed_count, games_confirmed) = await self.confirm_auto(ctx.guild, ctx.prefix, ctx.channel)
            return await ctx.send(f'Autoconfirm process complete. {games_confirmed} games auto-confirmed. {unconfirmed_count - games_confirmed} games left unconfirmed.')

        if arg.lower() == 'auto' and False:
            game_query = models.Game.search(status_filter=5, guild_id=ctx.guild.id).order_by(models.Game.win_claimed_ts)
            old_24h = (datetime.datetime.now() + datetime.timedelta(hours=-24))
            old_6h = (datetime.datetime.now() + datetime.timedelta(hours=-6))
            games_confirmed = 0
            unconfirmed_count = len(game_query)

            for game in game_query:
                (confirmed_count, side_count, _) = game.confirmations_count()

                if not game.win_claimed_ts:
                    logger.error(f'Game {game.id} does not have a value for win_claimed_ts - cannot auto confirm.')
                    continue

                if game.is_ranked and game.win_claimed_ts < old_24h:
                    game.declare_winner(winning_side=game.winner, confirm=True)
                    await post_win_messaging(ctx.guild, ctx.prefix, ctx.channel, game)
                    games_confirmed += 1
                    await ctx.send(f'Game {game.id} auto-confirmed. Ranked win claimed more than 24 hours ago. {confirmed_count} of {side_count} sides had confirmed.')
                elif not game.is_ranked and game.win_claimed_ts < old_6h:
                    game.declare_winner(winning_side=game.winner, confirm=True)
                    await post_win_messaging(ctx.guild, ctx.prefix, ctx.channel, game)
                    games_confirmed += 1
                    await ctx.send(f'Game {game.id} auto-confirmed. Unranked win claimed more than 6 hours ago. {confirmed_count} of {side_count} sides had confirmed.')
                elif side_count < 5 and confirmed_count > 1:
                    game.declare_winner(winning_side=game.winner, confirm=True)
                    await post_win_messaging(ctx.guild, ctx.prefix, ctx.channel, game)
                    games_confirmed += 1
                    await ctx.send(f'Game {game.id} auto-confirmed due to partial confirmations. {confirmed_count} of {side_count} sides had confirmed.')
                elif side_count >= 5 and confirmed_count > 2:
                    game.declare_winner(winning_side=game.winner, confirm=True)
                    await post_win_messaging(ctx.guild, ctx.prefix, ctx.channel, game)
                    games_confirmed += 1
                    await ctx.send(f'Game {game.id} auto-confirmed due to partial confirmations. {confirmed_count} of {side_count} sides had confirmed.')

            return await ctx.send(f'Autoconfirm process complete. {games_confirmed} games auto-confirmed. {unconfirmed_count - games_confirmed} games left unconfirmed.')

        # else confirming a specific game ie. $confirm 1234
        game_converter = PolyGame()
        winning_game = await game_converter.convert(ctx, arg)

        if not winning_game.is_completed:
            return await ctx.send(f'Game {winning_game.id} has no declared winner yet.')
        if winning_game.is_confirmed:
            return await ctx.send(f'Game with ID {winning_game.id} is already confirmed as completed with winner **{winning_game.winner.name()}**')

        winning_game.declare_winner(winning_side=winning_game.winner, confirm=True)
        winner_name = winning_game.winner.name()  # storing here trying to solve cursor closed error
        await post_win_messaging(ctx.guild, ctx.prefix, ctx.channel, winning_game)
        await ctx.send(f'**Game {winning_game.id}** winner has been confirmed as **{winner_name}**')  # Added here to try to fix InterfaceError Cursor Closed - seems to fix if there is output at the end

    async def confirm_auto(self, guild, prefix, current_channel):
        logger.debug('in confirm_auto')
        game_query = models.Game.search(status_filter=5, guild_id=guild.id).order_by(models.Game.win_claimed_ts)
        old_24h = (datetime.datetime.now() + datetime.timedelta(hours=-24))
        old_6h = (datetime.datetime.now() + datetime.timedelta(hours=-6))
        games_confirmed = 0
        unconfirmed_count = len(game_query)

        for game in game_query:
            (confirmed_count, side_count, _) = game.confirmations_count()

            if not game.win_claimed_ts:
                logger.error(f'Game {game.id} does not have a value for win_claimed_ts - cannot auto confirm.')
                continue

            if game.is_ranked and game.win_claimed_ts < old_24h:
                game.declare_winner(winning_side=game.winner, confirm=True)
                await post_win_messaging(guild, prefix, current_channel, game)
                games_confirmed += 1
                await current_channel.send(f'Game {game.id} auto-confirmed. Ranked win claimed more than 24 hours ago. {confirmed_count} of {side_count} sides had confirmed.')
            elif not game.is_ranked and game.win_claimed_ts < old_6h:
                game.declare_winner(winning_side=game.winner, confirm=True)
                await post_win_messaging(guild, prefix, current_channel, game)
                games_confirmed += 1
                await current_channel.send(f'Game {game.id} auto-confirmed. Unranked win claimed more than 6 hours ago. {confirmed_count} of {side_count} sides had confirmed.')
            elif side_count < 5 and confirmed_count > 1:
                game.declare_winner(winning_side=game.winner, confirm=True)
                await post_win_messaging(guild, prefix, current_channel, game)
                games_confirmed += 1
                await current_channel.send(f'Game {game.id} auto-confirmed due to partial confirmations. {confirmed_count} of {side_count} sides had confirmed.')
            elif side_count >= 5 and confirmed_count > 2:
                game.declare_winner(winning_side=game.winner, confirm=True)
                await post_win_messaging(guild, prefix, current_channel, game)
                games_confirmed += 1
                await current_channel.send(f'Game {game.id} auto-confirmed due to partial confirmations. {confirmed_count} of {side_count} sides had confirmed.')

        logger.debug(f'confirm_auto processed {unconfirmed_count} and confirmed {games_confirmed} games.')
        return (unconfirmed_count, games_confirmed)

    async def task_confirm_auto(self):
        await self.bot.wait_until_ready()
        sleep_cycle = (60 * 60 * 0.5)  # half hour cycle

        while not self.bot.is_closed():
            await asyncio.sleep(8)
            logger.debug('Task running: task_confirm_auto')

            utilities.connect()
            for guild in self.bot.guilds:
                staff_output_channel = guild.get_channel(settings.guild_setting(guild.id, 'game_request_channel'))
                if not staff_output_channel:
                    logger.debug(f'Could not load game_request_channel for server {guild.id} - skipping')
                    continue

                prefix = settings.guild_setting(guild.id, 'command_prefix')
                (unconfirmed_count, games_confirmed) = await self.confirm_auto(guild, prefix, staff_output_channel)
                if games_confirmed:
                    await staff_output_channel.send(f'Autoconfirm process complete. {games_confirmed} games auto-confirmed. {unconfirmed_count - games_confirmed} games left unconfirmed.')

            await asyncio.sleep(sleep_cycle)

    async def task_purge_incomplete(self):
        await self.bot.wait_until_ready()
        sleep_cycle = (60 * 60 * 2)  # 2 hour cycle

        while not self.bot.is_closed():
            await asyncio.sleep(20)
            logger.debug('Task running: task_purge_incomplete')

            old_60d = (datetime.date.today() + datetime.timedelta(days=-60))
            old_90d = (datetime.date.today() + datetime.timedelta(days=-90))
            old_120d = (datetime.date.today() + datetime.timedelta(days=-120))
            old_150d = (datetime.date.today() + datetime.timedelta(days=-150))

            for guild in self.bot.guilds:
                staff_output_channel = guild.get_channel(settings.guild_setting(guild.id, 'game_request_channel'))
                if not staff_output_channel:
                    logger.debug(f'Could not load game_request_channel for server {guild.id} {guild.name} - skipping')
                    continue

                utilities.connect()

                def async_game_search():
                    utilities.connect()
                    query = models.Game.search(status_filter=2, guild_id=guild.id)
                    query = list(query)  # reversing 'Incomplete' queries so oldest is at top
                    query.reverse()
                    return query

                game_list = await self.bot.loop.run_in_executor(None, async_game_search)

                delete_result = []
                for game in game_list[:500]:
                    game_size = len(game.lineup)
                    rank_str = ' - *Unranked*' if not game.is_ranked else ''
                    if game_size == 2 and game.date < old_60d and not game.is_completed:
                        delete_result.append(f'Deleting incomplete 1v1 game older than 60 days. - {game.get_headline()} - {game.date}{rank_str}')
                        # await self.bot.loop.run_in_executor(None, game.delete_game)
                        models.GameLog.create(game_id=game, guild_id=guild.id, message=f'I purged the game during cleanup of old incomplete games.')
                        game.delete_game()

                    if game_size == 3 and game.date < old_90d and not game.is_completed:
                        delete_result.append(f'Deleting incomplete 3-player game older than 90 days. - {game.get_headline()} - {game.date}{rank_str}')
                        await game.delete_game_channels(self.bot.guilds, guild.id)
                        # await self.bot.loop.run_in_executor(None, game.delete_game)
                        models.GameLog.create(game_id=game, guild_id=guild.id, message=f'I purged the game during cleanup of old incomplete games.')
                        game.delete_game()

                    if game_size == 4:
                        if game.date < old_90d and not game.is_completed and not game.is_ranked:
                            delete_result.append(f'Deleting incomplete 4-player game older than 90 days. - {game.get_headline()} - {game.date}{rank_str}')
                            await game.delete_game_channels(self.bot.guilds, guild.id)
                            await self.bot.loop.run_in_executor(None, game.delete_game)
                            models.GameLog.create(game_id=game, guild_id=guild.id, message=f'I purged the game during cleanup of old incomplete games.')
                            game.delete_game()
                        if game.date < old_120d and not game.is_completed and game.is_ranked:
                            delete_result.append(f'Deleting incomplete ranked 4-player game older than 120 days. - {game.get_headline()} - {game.date}{rank_str}')
                            await game.delete_game_channels(self.bot.guilds, guild.id)
                            models.GameLog.create(game_id=game, guild_id=guild.id, message=f'I purged the game during cleanup of old incomplete games.')
                            # await self.bot.loop.run_in_executor(None, game.delete_game)
                            game.delete_game()

                    if (game_size == 5 or game_size == 6) and game.is_ranked and game.date < old_150d and not game.is_completed:
                        # Max out ranked game deletion at game_size==6
                        delete_result.append(f'Deleting incomplete ranked {game_size}-player game older than 150 days. - {game.get_headline()} - {game.date}{rank_str}')
                        await game.delete_game_channels(self.bot.guilds, guild.id)
                        # await self.bot.loop.run_in_executor(None, game.delete_game)
                        models.GameLog.create(game_id=game, guild_id=guild.id, message=f'I purged the game during cleanup of old incomplete games.')
                        game.delete_game()

                    if game_size >= 5 and not game.is_ranked and game.date < old_120d and not game.is_completed:
                        # no cap on unranked game deletion above 120 days old
                        delete_result.append(f'Deleting incomplete unranked {game_size}-player game older than 120 days. - {game.get_headline()} - {game.date}{rank_str}')
                        await game.delete_game_channels(self.bot.guilds, guild.id)
                        # await self.bot.loop.run_in_executor(None, game.delete_game)
                        models.GameLog.create(game_id=game, guild_id=guild.id, message=f'I purged the game during cleanup of old incomplete games.')
                        game.delete_game()

                delete_str = '\n'.join(delete_result)
                logger.info(f'Purging incomplete games for guild {guild.name}:\n{delete_str}')
                if len(delete_result):
                    await staff_output_channel.send(f'{delete_str[:1900]}\nFinished - purged {len(delete_result)} games')

            await asyncio.sleep(sleep_cycle)

    @commands.command(usage='game_id')
    async def rankset(self, ctx, game: PolyGame = None):
        """ *Staff*: Marks an incomplete game as ranked
        Turns an incomplete unranked game into a ranked game
         **Examples**
        `[p]rankset 50`
        """
        if game is None:
            return await ctx.send(f'No matching game was found.')

        if game.is_completed or game.is_confirmed:
            return await ctx.send(f'This can only be used on a pending game. You can use `{ctx.prefix}unwin` to turn a completed game into a pending game.')

        if game.is_ranked:
            return await ctx.send(f'Game {game.id} is already marked as ranked.')

        game.is_ranked = True
        game.save()

        logger.info(f'Game {game.id} is now marked as ranked.')
        models.GameLog.create(game_id=game, guild_id=ctx.guild.id, message=f'**{discord.utils.escape_markdown(ctx.author.display_name)}** (`{ctx.author.id}`) set game to be ranked.')
        return await ctx.send(f'Game {game.id} is now marked as ranked.')

    @commands.command(usage='game_id')
    async def rankunset(self, ctx, game: PolyGame = None):
        """ *Staff*: Marks an incomplete game as unranked
        Turns an incomplete ranked game into an unranked game
         **Examples**
        `[p]rankunset 50`
        """
        if game is None:
            return await ctx.send(f'No matching game was found.')

        if game.is_completed or game.is_confirmed:
            return await ctx.send(f'This can only be used on a pending game. You can use `{ctx.prefix}unwin` to turn a completed game into a pending game.')

        if not game.is_ranked:
            return await ctx.send(f'Game {game.id} is already marked as unranked.')

        game.is_ranked = False
        game.save()

        logger.info(f'Game {game.id} is now marked as unranked.')
        models.GameLog.create(game_id=game, guild_id=ctx.guild.id, message=f'**{ctx.author.display_name}** (`{ctx.author.id}`) set game to be unranked.')
        return await ctx.send(f'Game {game.id} is now marked as unranked.')

    @settings.in_bot_channel()
    @commands.command(usage='game_id')
    async def unstart(self, ctx, game: PolyGame = None):
        """ *Staff*: Resets an in progress game to a pending matchmaking sesson

         **Examples**
        `[p]unstart 1234`
        """

        if game is None:
            return await ctx.send(f'No matching game was found.')
        if game.is_completed or game.is_confirmed:
            return await ctx.send(f'Game {game.id} is marked as completed already.')
        if game.is_pending:
            return await ctx.send(f'Game {game.id} is already a pending matchmaking session.')

        if game.announcement_message:
            game.name = f'~~{game.name}~~ GAME CANCELLED'
            await game.update_announcement(guild=ctx.guild, prefix=ctx.prefix)

        await game.delete_game_channels(self.bot.guilds, ctx.guild.id)

        game.is_pending = True
        tomorrow = (datetime.datetime.now() + datetime.timedelta(hours=24))
        game.expiration = tomorrow if game.expiration < tomorrow else game.expiration
        game.save()
        models.GameLog.create(game_id=game, guild_id=ctx.guild.id, message=f'**{discord.utils.escape_markdown(ctx.author.display_name)}** (`{ctx.author.id}`) changed in-progress game to an open game. ({ctx.prefix}unstart)')

        try:
            await ctx.send(f'Game {game.id} is now an open game and no longer in progress.')
        except discord.errors.NotFound:
            logger.warn('Game unstarted while in game-related channel')

    @commands.command(usage='search_term', aliases=['gamelogs'])
    # @commands.cooldown(1, 20, commands.BucketType.user)
    async def gamelog(self, ctx, *, search_term: str = None):
        """ *Staff*: Lists or searches log entries

         **Examples**
        `[p]gamelog 1234` - See all entries related to a specific game
        `[p]gamelog Nelluk` - See all entries containing the term Nelluk
        `[p]gamelog Nelluk join` - See all entries containing both words

        `[p]gamelogs` - *Mod only*: List last 50 log messages, regardless of game.
        """

        # TODO: Might have issue with log entries leaking across servers. Could add a guild_id field to the log table and limit
        # searches to ctx.guild.id. Would need a one-time command to populate guild_id on old entries
        if ctx.invoked_with == 'gamelog':
            # look up history of one game

            if search_term:
                try:
                    game_id = int(search_term)
                except (ValueError):
                    game_id = None  # search_term is string, used for text search of log contents
                else:
                    # Numeric search term passed, if its <= 7 chars assume its a game ID and search that way
                    if len(search_term) > 7:
                        # if numeric string > 7 chars passed, assuming its not a game ID but will be used for a text search
                        game_id = None
            else:
                return await ctx.send(f'No search term was entered')

            if game_id:
                message_list = [f'Listing all entries for game # {game_id}...']
                entries = models.GameLog.select().where(
                    (models.GameLog.game_id == game_id) & (models.GameLog.guild_id == ctx.guild.id)
                ).order_by(-models.GameLog.message_ts)
                for entry in entries:
                    message_list.append(f'`{entry.message_ts.strftime("%Y-%m-%d %H:%M:%S")}` - {entry.message}')
            else:
                # Keyword search will also return log entries not tied to a game or server, specifically code/name sets
                message_list = [f'Listing the 50 most recent entries containing *{search_term}*...']
                entries = models.GameLog.select().where(
                    (models.GameLog.message.contains(search_term.replace(' ', '%'))) & (
                        (models.GameLog.guild_id == ctx.guild.id) | (models.GameLog.guild_id == 0)
                    )
                ).order_by(-models.GameLog.message_ts).limit(50)
                for entry in entries:
                    message_list.append(f'`{entry.message_ts.strftime("%Y-%m-%d %H:%M:%S")}` - {entry.game_id} - {entry.message}')

        elif ctx.invoked_with == 'gamelogs' and settings.is_mod(ctx.author):
            # List 50 more recent logged actions
            if search_term and search_term.upper() == 'ALL':
                message_list = [f'Listing the 50 most recent log items (across all guilds)...']
                entries = models.GameLog.select().order_by(-models.GameLog.message_ts).limit(50)
            else:
                message_list = [f'Listing the 50 most recent log items...']
                entries = models.GameLog.select().where(models.GameLog.guild_id == ctx.guild.id).order_by(-models.GameLog.message_ts).limit(50)

            for entry in entries:
                message_list.append(f'`{entry.message_ts.strftime("%Y-%m-%d %H:%M:%S")}` - {entry.game_id} - {entry.message}')

        await utilities.buffered_send(destination=ctx, content='\n'.join(message_list))

    @commands.command(usage='game_id')
    async def extend(self, ctx, game: PolyGame = None):
        """ *Staff*: Extends the timer of an open game by 24 hours

         **Examples**
        `[p]extend 1234`
        """

        if not game.is_pending:
            return await ctx.send(f'Game {game.id} is no longer an open game so cannot be extended.')

        old_expiration = game.expiration

        if game.expiration < datetime.datetime.now():
            new_expiration = datetime.datetime.now() + datetime.timedelta(hours=24)
        else:
            new_expiration = game.expiration + datetime.timedelta(hours=24)

        game.expiration = new_expiration
        game.save()
        return await ctx.send(f'Game {game.id}\'s deadline has been extended to **{game.expiration}**. Previous expiration was **{old_expiration}**.')

    @commands.command(usage='tribe_name new_emoji')
    @settings.is_mod_check()
    async def tribe_emoji(self, ctx, tribe_name: str, emoji):
        """*Mod*: Assign an emoji to a tribe
        The emoji chosen will be used on *all* servers that this bot is on.
        It can only be triggered by an admin on a server that contributes to the Global ELO leaderboard.
        **Example:**
        `[p]tribe_emoji Bardur :new_bardur_emoji:`
        """
        if not settings.guild_setting(ctx.guild.id, 'include_in_global_lb') and ctx.author.id != settings.owner_id:
            return await ctx.send(f'This command can only be run in a Global ELO server (ie. PolyChampions or Polytopia Main')

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}tribe_emoji Tribename :my_custom_emoji:`'.format(ctx.prefix))

        try:
            tribe = models.Tribe.update_emoji(name=tribe_name, emoji=emoji)
        except exceptions.CheckFailedError as e:
            return await ctx.send(e)

        await ctx.send(f'Tribe {tribe.name} updated with new emoji: {tribe.emoji}')

    @commands.command(aliases=['team_add_junior'], usage='new_team_name')
    @settings.is_mod_check()
    @settings.teams_allowed()
    async def team_add(self, ctx, *, team_name: str):
        """*Mod*: Create new server Team
        The team should have a Role with an identical name.
        **Example:**
        `[p]team_add The Amazeballs`
        `[p]team_add The Amazeballs hidden` - Team will be excluded from leaderboards
        `[p]team_add_junior The Little Amazeballs` - Team added in "junior" league
        """
        if ' hidden' in team_name:
            hidden_flag = True
            team_name = team_name.replace('hidden', '').strip()
        else:
            hidden_flag = False

        if ctx.invoked_with == 'team_add_junior':
            pro_league = False
            pro_str = 'Junior '
        else:
            pro_league = True
            pro_str = ''

        try:
            team = models.Team.create(name=team_name, guild_id=ctx.guild.id, is_hidden=hidden_flag, pro_league=pro_league)
        except peewee.IntegrityError:
            return await ctx.send('That team already exists!')

        await ctx.send(f'{pro_str}Team {team_name} created! Starting ELO: {team.elo}. Players with a Discord Role exactly matching \"{team_name}\" will be considered team members. '
                f'You can now set the team flair with `{ctx.prefix}`team_emoji and `{ctx.prefix}team_image`.')

    @commands.command(usage='team_name new_emoji')
    @settings.is_mod_check()
    @settings.teams_allowed()
    async def team_emoji(self, ctx, team_name: str, emoji):
        """*Mod*: Assign an emoji to a team
        **Example:**
        `[p]team_emoji Amazeballs :my_fancy_emoji:`
        """

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

        matching_teams = models.Team.get_by_name(team_name, ctx.guild.id)
        if len(matching_teams) != 1:
            return await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

        team = matching_teams[0]
        team.emoji = emoji
        team.save()

        await ctx.send('Team {0.name} updated with new emoji: {0.emoji}'.format(team))

    @commands.command(usage='team_name image_url')
    @settings.is_mod_check()
    @settings.teams_allowed()
    async def team_image(self, ctx, team_name: str, image_url):
        """*Mod*: Set a team's logo image

        **Example:**
        `[p]team_image Amazeballs http://www.path.to/image.png`
        """

        if 'http' not in image_url:
            return await ctx.send(f'Valid image url not detected. Example usage: `{ctx.prefix}team_image name http://url_to_image.png`')
            # This is a very dumb check to make sure user is passing a URL and not a random string. Assumes mod can figure it out from there.

        try:
            team = models.Team.get_or_except(team_name, ctx.guild.id)
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_emoji name :my_custom_emoji:`')

        team.image_url = image_url
        team.save()

        await ctx.send(f'Team {team.name} updated with new image_url (image should appear below)')
        await ctx.send(team.image_url)

    @commands.command(usage='old_name new_name')
    @settings.is_mod_check()
    @settings.teams_allowed()
    async def team_name(self, ctx, old_team_name: str, new_team_name: str):
        """*Mod*: Change a team's name
        The team should have a Role with an identical name.
        Old name doesn't need to be precise, but new name does. Include quotes if it's more than one word.
        **Example:**
        `[p]team_name Amazeballs "The Wowbaggers"`
        """

        try:
            team = models.Team.get_or_except(old_team_name, ctx.guild.id)
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_name \"Current name\" \"New Team Name\"`')

        old_name = team.name
        team.name = new_team_name
        team.save()

        await ctx.send(f'Team **{old_name}** has been renamed to **{team.name}**.')

    @commands.command(aliases=['deactivate'])
    @settings.is_mod_check()
    @settings.on_polychampions()
    async def deactivate_players(self, ctx):
        """*Mods*: Add Inactive role to inactive players
        Apply the 'Inactive' role to any player who has not been activate lately.
        - No games started in 45 days, and does not have a protected role (Team Leadership or Mod roles)
        """

        inactive_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        protected_roles = [discord.utils.get(ctx.guild.roles, name='Team Recruiter'), discord.utils.get(ctx.guild.roles, name='Mod'),
                           discord.utils.get(ctx.guild.roles, name='Team Leader'), discord.utils.get(ctx.guild.roles, name='Team Co-Leader')]

        activity_time = (datetime.datetime.now() + datetime.timedelta(days=-45))
        if not inactive_role:
            return await ctx.send('Error loading Inactive role')

        active_players = models.Player.select(models.DiscordMember.discord_id).join(models.Lineup).join(models.Game).join_from(models.Player, models.DiscordMember).where(
            (models.Lineup.player == models.Player.id) & (models.Game.guild_id == ctx.guild.id) & (
                (models.Game.date > activity_time) | (models.Game.is_completed == 0)
            )
        ).group_by(models.DiscordMember.discord_id).having(
            peewee.fn.COUNT(models.Lineup.id) > 0
        )
        # players who are in an active game or any game started within 45 days

        list_of_active_player_ids = [p[0] for p in active_players.tuples()]

        defunct_members = []
        async with ctx.typing():
            for member in ctx.guild.members:
                if member.id in list_of_active_player_ids or inactive_role in member.roles:
                    continue
                if any(protected_role in member.roles for protected_role in protected_roles):
                    await ctx.send(f'Skipping inactive member **{member.name}** because they have a protected role.')
                    logger.debug(f'Skipping inactive member **{member.name}** because they have a protected role.')
                    continue
                if member.joined_at > activity_time:
                    logger.debug(f'Skipping {member.name} since they joined recently.')
                    continue

                defunct_members.append(member.mention)
                await member.add_roles(inactive_role, reason='Appeared inactive via deactivate_players command')
                logger.debug(f'{member.name} is inactive')

        if not defunct_members:
            return await ctx.send(f'No inactive members found!')

        members_str = ' / '.join(defunct_members)
        await utilities.buffered_send(destination=ctx, content=f'Found {len(defunct_members)} inactive members - *{inactive_role.name}* has been applied to each: {members_str}')

    @commands.command()
    @settings.is_mod_check()
    @settings.on_polychampions()
    async def kick_inactive(self, ctx, *, arg=None):
        """*Mods*: Kick players from server who don't meet activity requirements

        Kicks members from server who either:
        - Joined the server more than a week ago but have not registered a Poly code, or
        - Joined more than a month ago but have played zero ELO games in the last month.

        If a member has any role assigned they will not be kicked, beyond this list of 'kickable' roles:
        Inactive, The Novas, Novas Red, Novas Blue, ELO Rookie, ELO Player

        For example, Someone with role The Novas that has played zero games in the last month will be kicked.
        """

        count = 0
        last_week = (datetime.datetime.now() + datetime.timedelta(days=-7))
        last_month = (datetime.datetime.now() + datetime.timedelta(days=-30))
        inactive_role_name = settings.guild_setting(ctx.guild.id, 'inactive_role')

        kickable_role_name = [
            settings.guild_setting(ctx.guild.id, 'inactive_role'),
            '@everyone',
            'The Novas',
            'Nova Red',
            'Nova Blue'
            'Nova Grad'
            'ELO Rookie',
            'ELO Player',
            'The Bombers',
            'The Dynamite',
            'Bombers',
            'The Cosmonauts',
            'The Space Cadets',
            'Cosmonauts',
            'The Crawfish',
            'The Shrimps',
            'Crawfish',
            'The Dragons',
            'The Narwhals',
            'Dragons',
            'The Jets',
            'The Cropdusters',
            'Jets',
            'The Lightning',
            'The Pulse',
            'Lightning',
            'The Mallards',
            'The Drakes',
            'Mallards',
            'The Plague',
            'The Rats',
            'Plague',
            'The Ronin',
            'The Bandits',
            'Ronin',
            'The Sparkies',
            'The Pups',
            'Sparkies',
            'The Wildfire',
            'The Flames',
            'Wildfire',
            'ELO Banned',
            'Newbie',
        ]
        kickable_roles = [discord.utils.get(ctx.guild.roles, name=inactive_role_name), discord.utils.get(ctx.guild.roles, name='The Novas'),
                          discord.utils.get(ctx.guild.roles, name='ELO Rookie'), discord.utils.get(ctx.guild.roles, name='ELO Player'),
                          discord.utils.get(ctx.guild.roles, name='@everyone'), discord.utils.get(ctx.guild.roles, name='Nova Blue'),
                          discord.utils.get(ctx.guild.roles, name='Nova Red'), discord.utils.get(ctx.guild.roles, name='Nova Grad')]

        async with ctx.typing():
            for member in ctx.guild.members:
                remaining_member_roles = [x for x in member.roles if x not in kickable_roles]
                if len(remaining_member_roles) > 0:
                    continue  # Skip if they have any assigned roles beyond a 'purgable' role
                logger.debug(f'Member {member.name} qualifies based on roles...')
                if member.joined_at > last_week:
                    logger.debug(f'Joined in the previous week. Skipping.')
                    continue

                try:
                    dm = models.DiscordMember.get(discord_id=member.id)
                except peewee.DoesNotExist:
                    logger.debug(f'Player {member.name} has not registered with PolyELO Bot.')

                    if member.joined_at < last_week:
                        logger.info(f'Joined more than a week ago with no code on file. Kicking from server')
                        await member.kick(reason='No role, no code on file')
                        count += 1
                    continue
                else:
                    if member.joined_at < last_month:
                        if dm.games_played(in_days=30):
                            logger.debug('Has played recent ELO game on at least one server. Skipping.')
                        else:
                            logger.info(f'Joined more than a month ago and has played zero ELO games. Kicking from server')
                            await member.kick(reason='No role, no ELO games in at least 30 days.')
                            count += 1

        await ctx.send(f'Kicking {count} members without any assigned role and have insufficient ELO history.')

    @commands.command(aliases=['migrate'])
    @commands.is_owner()
    async def migrate_player(self, ctx, from_string: str, to_string: str):
        """*Owner*: Migrate games from player's old account to new account
        Target player cannot have any completed games associated with their profile. Use a @Mention or raw user ID as an argument.

        **Examples**
        [p]migrate_player @NellukOld @NellukNew
        """

        from_id, to_id = utilities.string_to_user_id(from_string), utilities.string_to_user_id(to_string)
        if not from_id or not to_id:
            return await ctx.send(f'Could not parse a discord ID. Usage: `{ctx.prefix}{ctx.invoked_with} @FromUser @ToUser`')

        try:
            old_discord_member = models.DiscordMember.select().where(models.DiscordMember.discord_id == from_id).get()
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
            logger.warn(f'Migrating player profile of ID {from_id} {old_discord_member.name} to new guild member {new_guild_member.id}{new_guild_member.name} with existing incomplete games')

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

            return await ctx.send('Migration complete!')

        else:
            # New player has no presence in the bot
            logger.warn(f'Migrating player profile of ID {from_id} {old_discord_member.name} to new guild member {new_guild_member.id}{new_guild_member.name}')

            await ctx.send(f'The games from DiscordMember `{from_id}` *{old_discord_member.name}* will be migrated and become associated with {new_guild_member.mention}')

            with models.db.atomic():

                old_discord_member.discord_id = new_guild_member.id
                old_discord_member.save()
                old_discord_member.update_name(new_name=new_guild_member.name)

            await ctx.send('Migration complete!')

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
        print(player_id)

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


def setup(bot):
    bot.add_cog(administration(bot))
