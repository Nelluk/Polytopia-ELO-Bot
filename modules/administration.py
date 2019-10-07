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
        if settings.run_tasks:
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

        await game.delete_game_channels(self.bot.guilds, ctx.guild.id)

        game.is_pending = True
        tomorrow = (datetime.datetime.now() + datetime.timedelta(hours=24))
        game.expiration = tomorrow if game.expiration < tomorrow else game.expiration
        game.save()
        return await ctx.send(f'Game {game.id} is now an open game and no longer in progress.')

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
    # async def team_add(self, ctx, *args):
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

        query = models.Player.select(models.DiscordMember.discord_id).join(models.Lineup).join(models.Game).join_from(models.Player, models.DiscordMember).where(
            (models.Lineup.player == models.Player.id) & (models.Game.date > activity_time) & (models.Game.guild_id == ctx.guild.id)
        ).group_by(models.DiscordMember.discord_id).having(
            peewee.fn.COUNT(models.Lineup.id) > 0
        )

        list_of_active_player_ids = [p[0] for p in query.tuples()]

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
        if len(members_str) > 1850:
            members_str = '(*Output truncated*)' + members_str[:1850]
        await ctx.send(f'Found {len(defunct_members)} inactive members - *{inactive_role.name}* has been applied to each: {members_str}')

    @commands.command()
    # @settings.is_mod_check()
    @settings.on_polychampions()
    async def grad_novas(self, ctx, *, arg=None):
        """*Staff*: Check Novas for graduation requirements
        Apply the 'Free Agent' role to any Novas who meets requirements:
        - Three ranked team games, and ranked games with members of at least three League teams
        """

        grad_count = 0
        role = discord.utils.get(ctx.guild.roles, name='The Novas')
        grad_role = discord.utils.get(ctx.guild.roles, name='Free Agent')
        recruiter_role = discord.utils.get(ctx.guild.roles, name='Team Recruiter')
        drafter_role = discord.utils.get(ctx.guild.roles, name='Drafter')
        inactive_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        grad_chan = ctx.guild.get_channel(540332800927072267)  # Novas draft talk
        if ctx.guild.id == settings.server_ids['test']:
            role = discord.utils.get(ctx.guild.roles, name='testers')
            grad_role = discord.utils.get(ctx.guild.roles, name='Team Leader')
            recruiter_role = discord.utils.get(ctx.guild.roles, name='role1')
            drafter_role = recruiter_role
            grad_chan = ctx.guild.get_channel(479292913080336397)  # bot spam

        await ctx.send(f'Auto-graduating Novas')
        async with ctx.typing():
            for member in role.members:
                if inactive_role and inactive_role in member.roles:
                    continue
                try:
                    dm = models.DiscordMember.get(discord_id=member.id)
                    player = models.Player.get(discord_member=dm, guild_id=ctx.guild.id)
                except peewee.DoesNotExist:
                    logger.debug(f'Player {member.name} not registered.')
                    continue
                if grad_role in member.roles:
                    logger.debug(f'Player {player.name} already has the graduate role.')
                    continue
                if player.completed_game_count() < 3:
                    logger.debug(f'Player {player.name} has not completed enough ranked games ({player.completed_game_count()} completed).')
                    continue
                if player.games_played(in_days=7).count() == 0:
                    logger.debug(f'Player {player.name} has not played in any recent games.')
                    continue

                team_game_count = 0
                league_teams_represented, qualifying_games = [], []

                for lineup in player.games_played():
                    game = lineup.game
                    if not game.is_ranked or game.largest_team() == 1:
                        continue
                    team_game_count += 1
                    for lineup in game.lineup:
                        if lineup.player.team not in league_teams_represented and lineup.player.team != player.team and lineup.gameside.team != player.team:
                            league_teams_represented.append(lineup.player.team)
                            if str(game.id) not in qualifying_games:
                                qualifying_games.append(str(game.id))
                if team_game_count < 3:
                    logger.debug(f'Player {player.name} has not completed enough team games.')
                    continue
                if len(league_teams_represented) < 3:
                    logger.debug(f'Player {player.name} has not played with enough league members.')
                    continue

                wins, losses = dm.get_record()
                logger.debug(f'Player {player.name} meets qualifications: {qualifying_games}')
                grad_count += 1
                await member.add_roles(grad_role)
                await grad_chan.send(f'Player {member.mention} (*Global ELO: {dm.elo} \u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}*) qualifies for graduation on the basis of games: `{" ".join(qualifying_games)}`')
            if grad_count:
                await grad_chan.send(f'{recruiter_role.mention} the above player(s) meet the qualifications for graduation. DM {drafter_role.mention} to express interest.')

            await ctx.send(f'Completed auto-grad: {grad_count} new graduates.')

    @commands.command()
    @settings.is_mod_check()
    @settings.on_polychampions()
    async def kick_inactive(self, ctx, *, arg=None):
        """*Mods*: Kick players from server who don't meet activity requirements

        Kicks members from server who either:
        - Joined the server more than a week ago but have not registered a Poly code, or
        - Joined more than a month ago but have played zero ELO games in the last month.

        If a member has any role assigned they will not be kicked, beyond this list of 'kickable' roles:
        Inactive, The Novas, ELO Rookie, ELO Player

        For example, Someone with role The Novas that has played zero games in the last month will be kicked.
        """

        count = 0
        last_week = (datetime.datetime.now() + datetime.timedelta(days=-7))
        last_month = (datetime.datetime.now() + datetime.timedelta(days=-30))
        inactive_role_name = settings.guild_setting(ctx.guild.id, 'inactive_role')
        kickable_roles = [discord.utils.get(ctx.guild.roles, name=inactive_role_name), discord.utils.get(ctx.guild.roles, name='The Novas'),
                          discord.utils.get(ctx.guild.roles, name='ELO Rookie'), discord.utils.get(ctx.guild.roles, name='ELO Player'),
                          discord.utils.get(ctx.guild.roles, name='@everyone')]

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

    @commands.command()
    @settings.is_mod_check()
    async def purge_incomplete(self, ctx):
        """*Mod*: Purge old incomplete games
        Purges up to 10 games at a time. Only incomplete 2-player games that started more than 60 days ago, or 3-player games that started more than 90 days ago.
        """

        old_60d = (datetime.date.today() + datetime.timedelta(days=-60))
        old_90d = (datetime.date.today() + datetime.timedelta(days=-90))
        old_120d = (datetime.date.today() + datetime.timedelta(days=-120))

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
                await game.delete_game_channels(self.bot.guilds, ctx.guild.id)
                await self.bot.loop.run_in_executor(None, game.delete_game)

            if len(game.lineup) == 4:
                if game.date < old_90d and not game.is_completed and not game.is_ranked:
                    delete_result.append(f'Deleting incomplete 4-player game older than 90 days. - {game.get_headline()} - {game.date}{rank_str}')
                    await game.delete_game_channels(self.bot.guilds, ctx.guild.id)
                    await self.bot.loop.run_in_executor(None, game.delete_game)
                if game.date < old_120d and not game.is_completed and game.is_ranked:
                    delete_result.append(f'Deleting incomplete ranked 4-player game older than 120 days. - {game.get_headline()} - {game.date}{rank_str}')
                    await game.delete_game_channels(self.bot.guilds, ctx.guild.id)
                    await self.bot.loop.run_in_executor(None, game.delete_game)

            if len(delete_result) >= 10:
                break  # more than ten games and the output will be truncated

        delete_str = '\n'.join(delete_result)[:1900]  # max send length is 2000 chars.
        await ctx.send(f'{delete_str}\nFinished - purged {len(delete_result)} games')

    @commands.command(aliases=['migrate'])
    @commands.is_owner()
    async def migrate_player(self, ctx, from_string: str, to_string: str):
        """*Owner*: Migrate games from player's old account to new account
        Target player cannot have any games associated with their profile. Use a @Mention or raw user ID as an argument.

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

        try:
            new_discord_member = models.DiscordMember.select().where(models.DiscordMember.discord_id == new_guild_member.id).get()
        except peewee.DoesNotExist:
            pass
            # This is desired outcome - no DiscordMember found matching to_id, so its safe
        else:
            return await ctx.send(f'Found a DiscordMember *{new_discord_member.name}* in the database matching discord id `{new_guild_member.id}`. Cannot migrate to an existing player! Use `{ctx.prefix}delete_player` first.')

        logger.warn(f'Migrating player profile of ID {from_id} {old_discord_member.name} to new guild member {new_guild_member.id}{new_guild_member.name}')

        await ctx.send(f'The games from DiscordMember `{from_id}` *{old_discord_member.name}* will be migrated and become associated with {new_guild_member.mention}')

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
        try:
            discord_member = models.DiscordMember.select().where(models.DiscordMember.discord_id == player_id).get()
        except peewee.DoesNotExist:
            return await ctx.send(f'Could not find a DiscordMember in the database matching discord id `{player_id}`')

        player_games = models.Lineup.select().join(models.Player).where(
            (models.Lineup.player.discord_member == discord_member)
        ).count()

        if player_games > 0:
            return await ctx.send(f'DiscordMember {discord_member.name} was found but has {player_games} associated ELO games. Can only delete players with zero games.')

        name = discord_member.name
        discord_member.delete_instance()
        await ctx.send(f'Deleting DiscordMember {name} with discord ID `{player_id}` from ELO database. They have zero games associated with their profile.')

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
