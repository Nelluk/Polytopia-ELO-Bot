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


class administration:
    def __init__(self, bot):
        self.bot = bot
        self.bg_task = bot.loop.create_task(self.task_confirm_auto())

    async def __local_check(self, ctx):

        if settings.is_staff(ctx):
            return True
        else:
            if ctx.invoked_with == 'help' and ctx.command.name != 'help':
                return False
            else:
                await ctx.send('You do not have permission to use this command.')
                return False

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

            with models.db:
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
        return await ctx.send(f'Game {game.id} is now marked as unranked.')

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

        await game.delete_game_channels(ctx.guild)

        game.is_pending = True
        tomorrow = (datetime.datetime.now() + datetime.timedelta(hours=24))
        game.expiration = tomorrow if game.expiration < tomorrow else game.expiration
        game.save()
        return await ctx.send(f'Game {game.id} is now an open game and no longer in progress.')

    @commands.command(usage='game_id')
    async def unwin(self, ctx, game: PolyGame = None):
        """ *Staff*: Reset a completed game to incomplete
        Reverts ELO changes from the completed game and any subsequent completed game.
        Resets the game as if it were still incomplete with no declared winner.
         **Examples**
        `[p]unwin 50`
        """

        if game is None:
            return await ctx.send(f'No matching game was found.')

        game.confirmations_reset()

        if game.is_completed and game.is_confirmed:
            elo_logger.debug(f'unwin game {game.id}')
            async with ctx.typing():
                with models.db.atomic():
                    timestamp = game.completed_ts
                    game.reverse_elo_changes()
                    game.completed_ts = None
                    game.is_confirmed = False
                    game.is_completed = False
                    game.winner = None
                    game.save()

                    models.Game.recalculate_elo_since(timestamp=timestamp)
            elo_logger.debug(f'unwin game {game.id} completed')
            return await ctx.send(f'Game {game.id} has been marked as *Incomplete*. ELO changes have been reverted and ELO from all subsequent games recalculated.')

        elif game.is_completed:
            # Unconfirmed win
            game.completed_ts = None
            game.is_completed = False
            game.winner = None
            game.save()
            return await ctx.send(f'Unconfirmed Game {game.id} has been marked as *Incomplete*.')

        else:
            return await ctx.send(f'Game {game.id} does not have a confirmed winner.')

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
        **Example:**
        `[p]tribe_emoji Bardur :new_bardur_emoji:`
        """

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}tribe_emoji Tribename :my_custom_emoji:`'.format(ctx.prefix))

        try:
            tribeflair = models.TribeFlair.upsert(name=tribe_name, guild_id=ctx.guild.id, emoji=emoji)
        except exceptions.CheckFailedError as e:
            return await ctx.send(e)

        await ctx.send('Tribe {0.tribe.name} updated with new emoji: {0.emoji}'.format(tribeflair))

    @commands.command(aliases=['addteam'], usage='new_team_name')
    @settings.is_mod_check()
    @settings.teams_allowed()
    async def team_add(self, ctx, *args):
        """*Mod*: Create new server Team
        The team should have a Role with an identical name.
        **Example:**
        `[p]team_add The Amazeballs`
        """

        name = ' '.join(args)
        try:
            team = models.Team.create(name=name, guild_id=ctx.guild.id)
        except peewee.IntegrityError:
            return await ctx.send('That team already exists!')

        await ctx.send(f'Team {name} created! Starting ELO: {team.elo}. Players with a Discord Role exactly matching \"{name}\" will be considered team members. '
                f'You can now set the team flair with `{ctx.prefix}`team_emoji and `{ctx.prefix}team_image`.')

    @commands.command(usage='team_name new_emoji')
    @settings.is_mod_check()
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
            matching_teams = models.Team.get_or_except(team_name, ctx.guild.id)
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_emoji name :my_custom_emoji:`')

        team = matching_teams[0]
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
            matching_teams = models.Team.get_or_except(old_team_name, ctx.guild.id)
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_name \"Current name\" \"New Team Name\"`')

        team = matching_teams[0]
        team.name = new_team_name
        team.save()

        await ctx.send('Team **{}** has been renamed to **{}**.'.format(old_team_name, new_team_name))

    @commands.command()
    @settings.is_mod_check()
    @settings.on_polychampions()
    async def purge_novas(self, ctx, *, arg=None):
        """*Mods*: Purge inactive Novas
        Purges the 'Novas' role from any player who either:
        A) Joined more than a week ago and has no registered poly code, or
        B) Join more than 6 weeks ago and has no games registered with the bot in the last 6 weeks.
        """

        role = discord.utils.get(ctx.guild.roles, name='The Novas')
        count = 0

        for member in role.members:
            try:
                dm = models.DiscordMember.get(discord_id=member.id)
            except peewee.DoesNotExist:
                logger.info(f'Player {member.name} not registered.')
                last_week = (datetime.datetime.now() + datetime.timedelta(days=-7))
                if member.joined_at < last_week:
                    logger.info(f'Joined more than a week ago. Purging role...')
                    await member.remove_roles(role)
                    count += 1
                continue
            else:
                logger.info(f'Player {member.name} is registered.')
                six_weeks = (datetime.datetime.now() + datetime.timedelta(days=-42))
                if member.joined_at < six_weeks:
                    if not dm.games_played(in_days=42):
                        count += 1
                        logger.info(f'Purging {member.name} from Novas - joined more than 6 weeks ago and has played 0 games in that period.')
        await ctx.send(f'Purging  **The Novas** role from {count} members who have not registered a poly code in the last week OR played a game in the last six weeks.')

    @commands.command()
    @commands.is_owner()
    async def purge_incomplete(self, ctx):
        """*Owner*: Purge old incomplete games
        Purges up to 10 games at a time. Only incomplete 2-player games that started more than 60 days ago, or 3-player games that started more than 90 days ago.
        """

        old_60d = (datetime.date.today() + datetime.timedelta(days=-60))
        old_90d = (datetime.date.today() + datetime.timedelta(days=-90))

        def async_game_search():
            query = models.Game.search(status_filter=2, guild_id=ctx.guild.id)
            query = list(query)  # reversing 'Incomplete' queries so oldest is at top
            query.reverse()
            return query

        game_list = await self.bot.loop.run_in_executor(None, async_game_search)

        delete_result = []
        for game in game_list[:500]:
            rank_str = ' - *Unranked*' if not game.is_ranked else ''
            if len(game.lineup) == 2 and game.date < old_60d and not game.is_completed:
                delete_result.append(f'Deleting incomplete 1v1 game older than 60 days. - {game.get_headline()} - {game.date}{rank_str}')
                await self.bot.loop.run_in_executor(None, game.delete_game)

            if len(game.lineup) == 3 and game.date < old_90d and not game.is_completed:
                delete_result.append(f'Deleting incomplete 3-player game older than 90 days. - {game.get_headline()} - {game.date}{rank_str}')
                await game.delete_game_channels(ctx.guild)
                await self.bot.loop.run_in_executor(None, game.delete_game)

            if len(delete_result) >= 10:
                break  # more than ten games and the output will be truncated

        delete_str = '\n'.join(delete_result)[:1900]  # max send length is 2000 chars.
        await ctx.send(f'{delete_str}\nFinished - purged {len(delete_result)} games')

    @commands.command()
    @commands.is_owner()
    async def recalc_elo(self, ctx):
        """*Owner*: Recalculate ELO for all games
        Intended to be used when a change to the ELO math is made to apply to all games retroactively
        """

        async with ctx.typing():
            await ctx.send('Recalculating ELO for all games in database.')
            await self.bot.loop.run_in_executor(None, models.Game.recalculate_all_elo)
            # Allows bot to remain responsive while this large operation is running.
        await ctx.send('Recalculation complete!')

    @commands.command(aliases=['dbb'])
    @commands.is_owner()
    async def backup_db(self, ctx):
        """*Owner*: Backup PSQL database to a file
        Intended to be used when a change to the ELO math is made to apply to all games retroactively
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
