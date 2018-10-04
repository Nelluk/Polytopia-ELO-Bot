from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import peewee
from modules.models import Game, db, Player, Team, SquadGame, SquadMemberGame  # Team, Game, Player, DiscordMember
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
            gid = int(game_id)
        except ValueError:
            logger.error(f'Invalid game ID "{game_id}".')
            return None
        game = Game.load_full_game(game_id=gid)
        return game

    @commands.command(aliases=['newgame'], brief='Helpers: Sets up a new game to be tracked', usage='"Name of Game" player1 player2 vs player3 player4')
    # @commands.has_any_role(*helper_roles)
    # TODO: command should require 'Member' role on main server
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

        game = Game.load_full_game(game_id=1)

        await ctx.send(embed=game.embed(ctx))

        # smg = SquadMemberGame.get(id=1)
        # print(smg.tribe.emoji)
        # foo = Player.get_by_string(player_string=name, guild_id=ctx.guild.id)
        # p = foo[0]
        # print(p.completed_game_count())
        # print(name)
        # p = Player.get_by_string(name)

        # p[0].test()
        # Player.test()
        # Player.test(foo='blah')


def setup(bot):
    bot.add_cog(games(bot))
