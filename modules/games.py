from discord.ext import commands
import modules.utilities as utilities
import peewee
from modules.models import Game, db  # Team, Game, Player, DiscordMember
from bot import logger


class games():

    def __init__(self, bot):
        self.bot = bot

    def poly_game(game_id):
        # Give game ID integer return matching game or None. Can be used as a converter function for discord command input:
        # https://discordpy.readthedocs.io/en/rewrite/ext/commands/commands.html#basic-converters
        with db:
            try:
                game = Game.get(id=game_id)
                logger.debug(f'Game with ID {game_id} found.')
                return game
            except peewee.DoesNotExist:
                logger.warn(f'Game with ID {game_id} cannot be found.')
                return None
            except ValueError:
                logger.error(f'Invalid game ID "{game_id}".')
                return None

    @commands.command(aliases=['newgame'], brief='Helpers: Sets up a new game to be tracked', usage='"Name of Game" player1 player2 vs player3 player4')
    # @commands.has_any_role(*helper_roles)
    # command should require 'Member' role on main server
    async def startgame(self, ctx, game_name: str, *args):
        side_home, side_away = [], []
        example_usage = (f'Example usage:\n`{ctx.prefix}startgame "Name of Game" player2`- Starts a 1v1 game between yourself and player2'
            f'\n`{ctx.prefix}startgame "Name of Game" player1 player2 VS player3 player4` - Start a 2v2 game')

        if len(args) == 1:
            guild_matches = await utilities.get_guild_member(ctx, args[0])
            if len(guild_matches) == 0:
                return await ctx.send(f'Could not match "{args[0]}" to a server member. Try using an @Mention.')
            if len(guild_matches) > 1:
                return await ctx.send(f'More than one server matches found for "{args[0]}". Try being more specific or using an @Mention.')
            if guild_matches[0] == ctx.author:
                return await ctx.send(f'Stop playing with yourself!')
            side_away.append(guild_matches[0])
            side_home.append(ctx.author)

            return await ctx.send(f'Game is between {side_home[0].name} and {side_away[0].name}')
        elif len(args) > 1:
            if utilities.guild_setting(ctx, 'allow_teams') is False:
                return await ctx.send(f'Only 1v1 games are enabled on this server. For team ELO games with squad leaderboards check out PolyChampions.')
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
        else:
            return await ctx.send(f'Invalid format. {example_usage}')

        if len(side_home + side_away) > len(set(side_home + side_away)):
            await ctx.send('Duplicate players detected. Are you sure this is what you want? (That means the two sides are uneven.)')

        if ctx.author not in (side_home + side_away):  # TODO: allow staff to create games with other people
            return await ctx.send('You can\'t create a game that you are not a participant in.')

        logger.debug(f'All input checks passed. Creating new game records with args: {args}')

        newgame, home_squadgame, away_squadgame = Game.create_game([side_home, side_away],
            name=game_name, guild_id=ctx.guild.id,
            require_teams=utilities.guild_setting(ctx, 'require_teams'))

        # TODO: Send game embeds and create team channels

        mentions = [p.mention for p in side_home + side_away]
        await ctx.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')

    @commands.command(aliases=['endgame', 'win'], usage='game_id winner_name')
    # @commands.has_any_role(*helper_roles)
    async def wingame(self, ctx, winning_game: poly_game, winning_side_name: str):
        if winning_game is None:
            return await ctx.send(f'No matching game was found.')

        game_data = winning_game.load_all_related()
        for squadgame in game_data:
            for t in squadgame.membergame:
                print(f'{t.id} - {t.tribe.name}')


def setup(bot):
    bot.add_cog(games(bot))
