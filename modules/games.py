import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import peewee
from modules.models import Game, db, Player, Team, SquadGame, SquadMemberGame, DiscordMember, Squad, SquadMember  # Team, Game, Player, DiscordMember
# from bot import logger
import logging

logger = logging.getLogger('polybot.' + __name__)


class games():

    def __init__(self, bot):
        self.bot = bot

    def poly_game(game_id):
        # Give game ID integer return matching game or None. Can be used as a converter function for discord command input:
        # https://discordpy.readthedocs.io/en/rewrite/ext/commands/commands.html#basic-converters
        # all-related records are prefetched
        try:
            game = Game.load_full_game(game_id=int(game_id))
            logger.debug(f'Game with ID {game_id} found.')
            return game
        except ValueError:
            logger.warn(f'Invalid game ID "{game_id}".')
            return None
        except peewee.DoesNotExist:
            logger.warn(f'Game with ID {game_id} cannot be found.')
            return None

    async def on_member_update(self, before, after):
        # Updates display name in DB if user changes their discord name or guild nick
        if before.nick == after.nick and before.name == after.name:
            return

        try:
            player = Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.discord_id == after.id) & (Player.guild_id == after.guild.id)
            ).get()
        except peewee.DoesNotExist:
            return

        player.discord_member.name = after.name
        player.discord_member.save()
        player.generate_display_name(player_name=after.name, player_nick=after.nick)

    @commands.command(aliases=['namegame'], usage='game_id "New Name"')
    # @commands.has_any_role(*helper_roles)
    async def gamename(self, ctx, game: poly_game, *args):
        """*Staff:* Renames an existing game
        **Example:**
        `[p]gamename 25 Mountains of Fire`
        """

        if game is None:
            await ctx.send('No matching game was found.')
            return

        new_game_name = ' '.join(args)
        with db:
            # if game.name is not None:
                # await self.update_game_channel_name(ctx, game=game, old_game_name=game.name, new_game_name=new_game_name)
            # TODO: update for game channels
            game.name = new_game_name.title()
            game.save()
        # await update_announcement(ctx, game)
        # TODO: make above line work

        await ctx.send(f'Game ID {game.id} has been renamed to "{game.name}"')

    # @in_bot_channel()
    @commands.command(aliases=['teamlb'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbteam(self, ctx):
        """display team leaderboard"""
        # TODO: Only show number of members who have an ELO ranking?
        embed = discord.Embed(title='**Team Leaderboard**')
        with db:
            query = Team.select().order_by(-Team.elo).where(
                ((Team.name != 'Home') & (Team.name != 'Away') & (Team.guild_id == ctx.guild.id))
            )
            for counter, team in enumerate(query):
                team_role = discord.utils.get(ctx.guild.roles, name=team.name)
                team_name_str = f'{team.name}({len(team_role.members)})'  # Show team name with number of members
                # wins, losses = team.get_record()
                wins, losses = 1, 2
                embed.add_field(name=f'`{(counter + 1):>3}. {team_name_str:30}  (ELO: {team.elo:4})  W {wins} / L {losses}` {team.emoji}', value='\u200b', inline=False)
        await ctx.send(embed=embed)

    # @in_bot_channel()
    @commands.command(brief='See details on a player', usage='player_name', aliases=['elo'])
    async def player(self, ctx, *args):
        """See your own player card or the card of another player
        This also will find results based on a game-code or in-game name, if set.
        **Examples**
        `[p]player` - See your own player card
        `[p]player Nelluk` - See Nelluk's card
        """

        with db:
            if len(args) == 0:
                # Player looking for info on themselves
                player = Player.get_by_string(player_string=f'<@{ctx.author.id}>', guild_id=ctx.guild.id)
                if len(player) != 1:
                    return await ctx.send(f'Could not find you in the database. Try setting your code with {ctx.prefix}setcode')
                player = player[0]
            else:
                # Otherwise look for a player matching whatever they entered
                player_mention = ' '.join(args)
                matching_players = Player.get_by_string(player_string=player_mention, guild_id=ctx.guild.id)
                if len(matching_players) == 1:
                    player = matching_players[0]
                elif len(matching_players) == 0:
                    # No matching name in database. Fall back to searching on polytopia_id or polytopia_name. Warn if player is found in guild.
                    matches = await utilities.get_guild_member(ctx, player_mention)
                    if len(matches) > 0:
                        await ctx.send(f'"{player_mention}" was found in the server but is not registered with me. '
                            f'Players can be registered with `{ctx.prefix}setcode` or being in a new game\'s lineup.')

                    return await ctx.send(f'Could not find \"{player_mention}\" by Discord name, Polytopia name, or Polytopia ID.')

                else:
                    await ctx.send('There is more than one player found with that name. Specify user with @Mention.'.format(player_mention))
                    return
        with db:
            wins, losses = player.get_record()
            rank, lb_length = player.leaderboard_rank(settings.date_cutoff)

            if rank is None:
                rank_str = 'Unranked'
            else:
                rank_str = f'{rank} of {lb_length}'

            embed = discord.Embed(title=f'Player card for {player.name}')
            embed.add_field(name='Results', value=f'ELO: {player.elo}, W {wins} / L {losses}')
            embed.add_field(name='Ranking', value=rank_str)

            guild_member = ctx.guild.get_member(player.discord_member.discord_id)
            if guild_member is not None:
                embed.set_thumbnail(url=guild_member.avatar_url_as(size=512))

            if player.team:
                team_str = f'{player.team.name} {player.team.emoji}' if player.team.emoji else player.team.name
                embed.add_field(name='Last-known Team', value=team_str)
            if player.discord_member.polytopia_name:
                embed.add_field(name='Polytopia Game Name', value=player.discord_member.polytopia_name)
            if player.discord_member.polytopia_id:
                embed.add_field(name='Polytopia ID', value=player.discord_member.polytopia_id)
                content_str = player.discord_member.polytopia_id
                # Used as a single message before player card so users can easily copy/paste Poly ID
            else:
                content_str = ''

            embed.add_field(value='\u200b', name='Most recent games', inline=False)
            recent_games = Game.select(Game.id).join(SquadGame).join(SquadMemberGame).join(SquadMember).where(
                (SquadMemberGame.member.player == player)
            ).order_by(-Game.date)[:7]

            for game in recent_games:
                game = Game.load_full_game(game_id=game.id)  # preloads game data to reduce DB queries.
                if game.is_completed == 0:
                    status = 'Incomplete'
                else:
                    player_team = SquadGame.select(SquadGame.is_winner).join(SquadMemberGame).join(SquadMember).where(
                        (SquadGame.game == game) & (SquadMemberGame.member.player == player)
                    ).get()
                    print(player_team)
                    status = '**WIN**' if player_team.is_winner == 1 else '***Loss***'

                    embed.add_field(name=f'{game.get_headline()}',
                                value=f'{status} - {str(game.date)} - {game.team_size()}v{game.team_size()}')

            await ctx.send(content=content_str, embed=embed)

    @commands.command(aliases=['newgame'], brief='Helpers: Sets up a new game to be tracked', usage='"Name of Game" player1 player2 vs player3 player4')
    # @commands.has_any_role(*helper_roles)
    # TODO: command should require 'Rider' role on main server. 2v2 should require above that
    async def startgame(self, ctx, game_name: str, *args):
        side_home, side_away = [], []
        example_usage = (f'Example usage:\n`{ctx.prefix}startgame "Name of Game" player2`- Starts a 1v1 game between yourself and player2'
            f'\n`{ctx.prefix}startgame "Name of Game" player1 player2 VS player3 player4` - Start a 2v2 game')

        if len(args) == 1:
            # Shortcut version for 1v1s:
            # $startgame "Name of Game" opponent_name
            guild_matches = await utilities.get_guild_member(ctx, args[0])
            if len(guild_matches) == 0:
                return await ctx.send(f'Could not match "{args[0]}" to a server member. Try using an @Mention.')
            if len(guild_matches) > 1:
                return await ctx.send(f'More than one server matches found for "{args[0]}". Try being more specific or using an @Mention.')
            if guild_matches[0] == ctx.author:
                return await ctx.send(f'Stop playing with yourself!')
            side_away.append(guild_matches[0])
            side_home.append(ctx.author)

        elif len(args) > 1:
            # $startgame "Name of Game" p1 p2 vs p3 p4
            if settings.guild_setting(ctx.guild.id, 'allow_teams') is False:
                return await ctx.send('Only 1v1 games are enabled on this server. For team ELO games with squad leaderboards check out PolyChampions.')
            if len(args) not in [3, 5, 7, 9, 11] or args[int(len(args) / 2)].upper() != 'VS':
                return await ctx.send(f'Invalid format. {example_usage}')

            for p in args[:int(len(args) / 2)]:         # Args in first half before 'VS', converted to Discord Members
                guild_matches = await utilities.get_guild_member(ctx, p)
                if len(guild_matches) == 0:
                    return await ctx.send(f'Could not match "{p}" to a server member. Try using an @Mention.')
                if len(guild_matches) > 1:
                    return await ctx.send(f'More than one server matches found for "{p}". Try being more specific or using an @Mention.')
                side_home.append(guild_matches[0])

            for p in args[int(len(args) / 2) + 1:]:     # Args in second half after 'VS'
                guild_matches = await utilities.get_guild_member(ctx, p)
                if len(guild_matches) == 0:
                    return await ctx.send(f'Could not match "{p}" to a server member. Try using an @Mention.')
                if len(guild_matches) > 1:
                    return await ctx.send(f'More than one server matches found for "{p}". Try being more specific or using an @Mention.')
                side_away.append(guild_matches[0])

            if len(side_home) > settings.guild_setting(ctx.guild.id, 'max_team_size') or len(side_home) > settings.guild_setting(ctx.guild.id, 'max_team_size'):
                return await ctx.send('Maximium {0}v{0} games are enabled on this server. For full functionality with support for up to 5v5 games and league play check out PolyChampions.'.format(settings.guild_setting(ctx.guild.id, 'max_team_size')))

        else:
            return await ctx.send(f'Invalid format. {example_usage}')

        if len(side_home + side_away) > len(set(side_home + side_away)):
            # TODO: put behind allow_uneven_teams setting
            await ctx.send('Duplicate players detected. Are you sure this is what you want? (That means the two sides are uneven.)')

        if ctx.author not in (side_home + side_away) and settings.is_staff(ctx, ctx.author) is False:
            return await ctx.send('You can\'t create a game that you are not a participant in.')

        logger.debug(f'All input checks passed. Creating new game records with args: {args}')

        newgame, home_squadgame, away_squadgame = Game.create_game([side_home, side_away],
            name=game_name, guild_id=ctx.guild.id,
            require_teams=settings.guild_setting(ctx.guild.id, 'require_teams'))

        # TODO: Send game embeds and create team channels

        mentions = [p.mention for p in side_home + side_away]
        await ctx.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')

    @commands.command(aliases=['endgame', 'win'], usage='game_id winner_name')
    # @commands.has_any_role(*helper_roles)
    async def wingame(self, ctx, winning_game: poly_game, winning_side_name: str):
        if winning_game is None:
            return await ctx.send(f'No matching game was found.')

        if winning_game.is_completed is True:
            logger.debug('here is_completed')
            if winning_game.is_confirmed is True:
                logger.debug('here is_confirmed')
                return await ctx.send(f'Game with ID {winning_game.id} is already marked as completed with winner **{winning_game.get_winner().name}**')
            else:
                await ctx.send(f'Warning: Unconfirmed game with ID {winning_game.id} had previously been marked with winner **{winning_game.get_winner().name}**')

        if settings.is_staff(ctx, ctx.author):
            is_staff = True
        else:
            is_staff = False

            try:
                player, _ = winning_game.return_participant(ctx, player=ctx.author.id)
            except exceptions.CheckFailedError:
                return await ctx.send(f'You were not a participant in game {winning_game.id}, and do not have staff privileges.')

        try:
            if winning_game.team_size() == 1:
                winning_obj, winning_side = winning_game.return_participant(ctx, player=winning_side_name)

            elif winning_game.team_size() > 1:
                winning_obj, winning_side = winning_game.return_participant(ctx, team=winning_side_name)
            else:
                return logger.error('Invalid team_size. Aborting wingame command.')
        except exceptions.CheckFailedError as ex:
            return await ctx.send(f'{ex}')

        winning_game.declare_winner(winning_side=winning_side, confirm=is_staff)

    @commands.command()
    # @commands.has_any_role(*helper_roles)
    async def ts(self, ctx, name: str):

        player = Player.get(id=1)
        print(player.leaderboard_rank(date_cutoff=settings.date_cutoff))
        return

        print(q)
        # print(f'len: {len(q)}')
        # # print(dir(q))
        for r in q:
            print(r)
        #     print(r.id, r)

    # @in_bot_channel()
    @commands.command(aliases=['games'], brief='Find games or see a game\'s details', usage='game_id')
    async def game(self, ctx, *args):

        """Filter/search for specific games, or see a game's details.
        **Examples**:
        `[p]game 51` - See details on game # 51.
        `[p]games Jets`
        `[p]games Jets Ronin`
        `[p]games Nelluk`
        `[p]games Nelluk rickdaheals [or more players]`
        `[p]games Jets loss` - Jets losses
        `[p]games Ronin win` - Ronin victories
        `[p]games Jets Ronin incomplete`
        `[p]games Nelluk win`
        `[p]games Nelluk rickdaheals incomplete`
        """

        # TODO: remove 'and/&' to remove confusion over game names like Ocean & Prophesy

        arg_list = list(args)

        try:
            game_id = int(''.join(arg_list))
            game = Game.load_full_game(game_id=game_id)     # Argument is an int, so show game by ID
            embed = game.embed(ctx)
            return await ctx.send(embed=embed)
        except ValueError:
            return
        except peewee.DoesNotExist:
            return await ctx.send('Game with ID {} cannot be found.'.format(game_id))


def setup(bot):
    bot.add_cog(games(bot))
