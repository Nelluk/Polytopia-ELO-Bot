import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import peewee
from modules.models import Game, db, Player, Team, DiscordMember, Squad, TribeFlair, Lineup, SquadGame, SquadMember  # Team, Game, Player, DiscordMember
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
    @commands.command()
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lb(self, ctx):
        """ Display individual leaderboard"""

        leaderboard = []
        with db:
            leaderboard_query = Player.leaderboard(date_cutoff=settings.date_cutoff, guild_id=ctx.guild.id)
            for counter, player in enumerate(leaderboard_query[:500]):
                wins, losses = player.get_record()
                emoji_str = player.team.emoji if player.team else ''
                leaderboard.append(
                    (f'`{(counter + 1):>3}.` {emoji_str}`{player.name}`', f'`(ELO: {player.elo:4}) W {wins} / L {losses}`')
                )

        await utilities.paginate(self.bot, ctx, title='**Individual Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

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
    @commands.command(aliases=['squadlb'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbsquad(self, ctx):
        """Display squad leaderboard"""

        leaderboard = []
        with db:
            squads = Squad.leaderboard(date_cutoff=settings.date_cutoff, guild_id=ctx.guild.id)
            for counter, sq in enumerate(squads[:200]):
                wins, losses = sq.get_record()
                squad_members = sq.get_members()
                emoji_list = [p.team.emoji for p in squad_members if p.team is not None]
                emoji_string = ' '.join(emoji_list)
                squad_names = ' / '.join(sq.get_names())
                leaderboard.append(
                    (f'`{(counter + 1):>3}.` {emoji_string}`{squad_names}`', f'`(ELO: {sq.elo:4}) W {wins} / L {losses}`')
                )
        await utilities.paginate(self.bot, ctx, title='**Squad Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    # @in_bot_channel()
    @commands.command(brief='Find squads or see details on a squad', usage='player1 [player2] [player3]', aliases=['squads'])
    async def squad(self, ctx, *args):
        """Find squads with specific players, or see details on a squad
        **Examples:**
        `[p]squad 5` - details on squad 5
        `[p]squad Nelluk` - squads containing Nelluk
        `[p]squad Nelluk frodakcin` - squad containing both players
        """
        with db:
            try:
                # Argument is an int, so show squad by ID
                squad_id = int(''.join(args))
                squad = Squad.get(id=squad_id)
            except ValueError:
                squad_id = None
                # Args is not an int, which means search by game name
            except peewee.DoesNotExist:
                await ctx.send('Squad with ID {} cannot be found.'.format(squad_id))
                return

            if squad_id is None:
                # Search by player names
                squad_players = []
                for p_name in args:
                    p_matches = Player.get_by_string(p_name, guild_id=ctx.guild.id)
                    if len(p_matches) == 1:
                        squad_players.append(p_matches[0])
                    elif len(p_matches) > 1:
                        return await ctx.send(f'Found multiple matches for player "{p_name}". Try being more specific or quoting players "Full Name".')
                    else:
                        return await ctx.send(f'Found no matches for player "{p_name}".')

                squad_list = Squad.get_all_matching_squads(squad_players)
                if len(squad_list) == 0:
                    return await ctx.send(f'Found no squads containing players: {" / ".join(args)}')
                if len(squad_list) > 1:
                    # More than one matching name found, so display a short list
                    embed = discord.Embed(title=f'Found {len(squad_list)} matches. Try `{ctx.prefix}squad IDNUM`:')
                    for squad in squad_list[:10]:
                        wins, losses = squad.get_record()
                        embed.add_field(
                            name=f'`ID {squad.id:>3} - {" / ".join(squad.get_names()):40}`',
                            value=f'`(ELO: {squad.elo}) W {wins} / L {losses}`',
                            inline=False
                        )
                    return await ctx.send(embed=embed)

                # Exact matching squad found by player name
                squad = squad_list[0]

        with db:
            wins, losses = squad.get_record()
            rank, lb_length = squad.leaderboard_rank(settings.date_cutoff)

            if rank is None:
                rank_str = 'Unranked'
            else:
                rank_str = f'{rank} of {lb_length}'

            names_with_emoji = [f'{p.team.emoji} {p.name}' if p.team is not None else f'{p.name}' for p in squad.get_members()]

            embed = discord.Embed(title=f'Squad card for Squad {squad.id}\n{"  /  ".join(names_with_emoji)}', value='\u200b')
            embed.add_field(name='Results', value=f'ELO: {squad.elo},  W {wins} / L {losses}', inline=True)
            embed.add_field(name='Ranking', value=rank_str, inline=True)
            recent_games = SquadGame.select().join(Game).where(
                (SquadGame.squad == squad) & (Game.is_pending == 0)
            ).order_by(-Game.date)[:5]
            embed.add_field(value='\u200b', name='Most recent games', inline=False)

            for squadgame in recent_games:
                game = Game.load_full_game(game_id=squadgame.game)  # preloads game data to reduce DB queries.
                if game.is_completed == 0:
                    status = 'Incomplete'
                else:
                    status = '**WIN**' if squadgame.id == Game.winner else '***Loss***'

                embed.add_field(name=f'{game.get_headline()}',
                            value=f'{status} - {str(game.date)} - {game.team_size()}v{game.team_size()}')

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
                    return await ctx.send('There is more than one player found with that name. Specify user with @Mention.'.format(player_mention))

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

            recent_games = SquadGame.select(SquadGame, Game).join(Game).join_from(SquadGame, Lineup).where(
                (Lineup.player == player) & (Game.is_pending == 0)
            ).order_by(-Game.date)[:7]

            for squadgame in recent_games:
                game = Game.load_full_game(game_id=squadgame.game)  # preloads game data to reduce DB queries.
                if game.is_completed == 0:
                    status = 'Incomplete'
                else:
                    status = '**WIN**' if squadgame.id == Game.winner else '***Loss***'

                embed.add_field(name=f'{game.get_headline()}',
                            value=f'{status} - {str(game.date)} - {game.team_size()}v{game.team_size()}')

            await ctx.send(content=content_str, embed=embed)

    # @in_bot_channel()
    @commands.command(usage='team_name')
    async def team(self, ctx, team_string: str):
        """See details on a team
        **Example:**
        [p]team Ronin
        """

        matching_teams = Team.get_by_name(team_string, ctx.guild.id)
        if len(matching_teams) > 1:
            return await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
        if len(matching_teams) == 0:
            return await ctx.send(f'Cannot find a team with name "{team_string}". Be sure to use the full name, surrounded by quotes if it is more than one word.')
        team = matching_teams[0]

        embed = discord.Embed(title=f'Team card for **{team.name}** {team.emoji}')
        team_role = discord.utils.get(ctx.guild.roles, name=team.name)
        member_stats = []

        wins, losses = team.get_record()
        embed.add_field(name='Results', value=f'ELO: {team.elo}   Wins {wins} / Losses {losses}')

        if team_role:
            for member in team_role.members:
                # Create a list of members - pull ELO score from database if they are registered, or with 0 ELO if they are not
                p = Player.get_by_string(player_string=str(member.id), guild_id=ctx.guild.id)
                if len(p) == 0:
                    member_stats.append((member.name, 0, '\u200b'))
                else:
                    member_stats.append((f'**{p[0].name}**', p[0].elo, f'({p[0].elo})'))

            member_stats.sort(key=lambda tup: tup[1], reverse=True)     # sort the list descending by ELO
            members_sorted = [f'{x[0]}{x[2]}' for x in member_stats]    # create list of strings like Nelluk(1000)
            embed.add_field(name=f'Members({len(member_stats)})', value=f'{" / ".join(members_sorted)}')
        else:
            await ctx.send(f'Warning: No matching discord role "{team.name}" could be found. Player membership cannot be detected.')

        if team.image_url:
            embed.set_thumbnail(url=team.image_url)

        embed.add_field(value='*Recent games*', name='\u200b', inline=False)

        recent_games = SquadGame.select(SquadGame, Game).join(Game).where(
            (Game.id.in_(Team.team_games_subq())) & (SquadGame.team == team)
        ).order_by(-Game.date)[:7]

        for squadgame in recent_games:
            game = Game.load_full_game(game_id=squadgame.game)
            if game.is_completed == 1:
                result = '**WIN**' if squadgame.id == Game.winner else 'LOSS'
            else:
                result = 'Incomplete'

            # headline = game.get_headline()
            embed.add_field(name=f'{game.get_headline()}',
                value=f'{result} - {str(game.date)} - {game.team_size()}v{game.team_size()}')

        await ctx.send(embed=embed)

    @commands.command(brief='Sets a Polytopia game code and registers user with the bot', usage='[user] polytopia_code')
    async def setcode(self, ctx, *args):
        """
        Sets your own Polytopia code, or allows a staff member to set a player's code. This also will register the player with the bot if not already.
        **Examples:**
        `[p]setcode somelongpolycode`
        `[p]setcode Nelluk somelongpolycode`
        """

        if len(args) == 1:      # User setting code for themselves. No special permissions required.
            target_discord_member = ctx.message.author
            new_id = args[0]

        elif len(args) == 2:    # User changing another user's code. Helper permissions required.

            if settings.is_staff(ctx, ctx.author) is False:
                return await ctx.send(f'You only have permission to set your own code. To do that use `{ctx.prefix}setcode YOURCODEHERE`')

            # Try to find matching guild/server member
            guild_matches = await utilities.get_guild_member(ctx, args[0])
            if len(guild_matches) == 0:
                return await ctx.send(f'Could not find any server member matching "{args[0]}". Try specifying with an @Mention')
            elif len(guild_matches) > 1:
                return await ctx.send(f'Found multiple server members matching "{args[0]}". Try specifying with an @Mention')
            target_discord_member = guild_matches[0]
            new_id = args[1]
        else:
            # Unexpected input
            await ctx.send(f'Wrong number of arguments. Use `{ctx.prefix}setcode my_polytopia_code`')
            return

        if len(new_id) != 16 or new_id.isalnum() is False:
            # Very basic polytopia code sanity checking. Making sure it is 16-character alphanumeric.
            return await ctx.send(f'Polytopia code "{new_id}" does not appear to be a valid code.')

        _, team_list = Player.get_teams_of_players(guild_id=ctx.guild.id, list_of_players=[target_discord_member])
        print(team_list)

        with db:
            player, created = Player.upsert2(target_discord_member, guild_id=ctx.guild.id, team=team_list[0])
            player.discord_member.polytopia_id = new_id
            player.discord_member.save()

        if created:
            await ctx.send('Player {0.name} added to system with Polytopia code {0.discord_member.polytopia_id} and ELO {0.elo}'.format(player))
        else:
            await ctx.send('Player {0.name} updated in system with Polytopia code {0.discord_member.polytopia_id}.'.format(player))

    @commands.command(aliases=['code'], usage='player_name')
    async def getcode(self, ctx, player_string: str):
        """Get game code of a player
        Just returns the code and nothing else so it can easily be copied."""

        # TODO: If no player argument display own code?
        with db:
            player_matches = Player.get_by_string(player_string, ctx.guild.id)
            if len(player_matches) == 0:
                return await ctx.send('Cannot find player with that name. Correct usage: `{}getcode @Player`'.format(ctx.prefix))
            if len(player_matches) > 1:
                return await ctx.send('More than one matching player found. Use @player to specify. Correct usage: `{}getcode @Player`'.format(ctx.prefix))
            player_target = player_matches[0]
            if player_target.discord_member.polytopia_id:
                await ctx.send(player_target.discord_member.polytopia_id)
            else:
                await ctx.send('User was found but does not have a Polytopia ID on file.')

    @commands.command(brief='Set in-game name', usage='new_name')
    async def setname(self, ctx, *args):
        """Sets your own in-game name, or lets staff set a player's in-game name
        When this is set, people can find you by the in-game name with the `[p]player` command.
        **Examples:**
        `[p]setname PolyChamp` - Set your own in-game name to *PolyChamp*
        `[p]setname Nelluk PolyChamp` - Lets staff set in-game name of Nelluk to *PolyChamp*
        """

        if len(args) == 1:
            # User setting code for themselves. No special permissions required.
            target_string = f'<@{ctx.author.id}>'
            new_name = args[0]
        elif len(args) == 2:
            # User changing another user's code. Admin permissions required.
            if settings.is_staff(ctx, ctx.author) is False:
                return await ctx.send('You do not have permission to trigger this command.')
            target_string = args[0]
            new_name = args[1]
        else:
            # Unexpected input
            return await ctx.send(f'Wrong number of arguments. Use `{ctx.prefix}setname my_polytopia_name`. Use "quotation marks" if the name is more than one word.')

        target_player = Player.get_by_string(target_string, ctx.guild.id)
        if len(target_player) == 0:
            return await ctx.send(f'Could not match any players to query "{target_string}". Try registering with {ctx.prefix}setcode first.')
        elif len(target_player) > 1:
            return await ctx.send(f'Multiple players found with query "{target_string}". Be more specfic or use an @Mention.')

        with db:
            target_player[0].discord_member.polytopia_name = new_name
            target_player[0].discord_member.save()
            await ctx.send(f'Player {target_player[0].name} updated in system with Polytopia name {new_name}.')

    @commands.command()
    async def incomplete(self, ctx, all: str = None):
        """List your or all incomplete games
        **Example:**
        `[p]incomplete` - Lists incomplete games you are playing in
        `[p]incomplete all` - Lists all incomplete games
        """
        incomplete_list = []

        if all and all.upper() == 'ALL':
            query = Game.select().where(
                (Game.is_completed == 0) & (Game.is_pending == 0)
            ).order_by(Game.date)
        else:
            query = Game.select().join(Lineup).join(Player).join(DiscordMember).where(
                (Game.is_completed == 0) & (Game.is_pending == 0) & (DiscordMember.discord_id == ctx.author.id)
            ).order_by(Game.date)

        for counter, game in enumerate(query[:500]):
            incomplete_list.append((
                f'{game.get_headline()}',
                f'{(str(game.date))} - {game.team_size()}v{game.team_size()}'
            ))

        await utilities.paginate(self.bot, ctx, title='**Oldest Incomplete Games**', message_list=incomplete_list, page_start=0, page_end=10, page_size=10)

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

        newgame = Game.create_game([side_home, side_away],
            name=game_name, guild_id=ctx.guild.id,
            require_teams=settings.guild_setting(ctx.guild.id, 'require_teams'))

        # TODO: Send game embeds and create team channels

        mentions = [p.mention for p in side_home + side_away]
        await ctx.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')

        await newgame.create_team_channels(ctx)

    @commands.command(aliases=['endgame', 'win'], usage='game_id winner_name')
    # @commands.has_any_role(*helper_roles)
    # TODO: output/announcements
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
                _, _ = winning_game.return_participant(ctx, player=ctx.author.id)
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

    @commands.command(usage='tribe_name new_emoji')
    # @commands.has_any_role(*mod_roles)
    async def tribe_emoji(self, ctx, tribe_name: str, emoji):
        """Mod: Assign an emoji to a tribe
        **Example:**
        `[p]tribe_emoji Bardur :new_bardur_emoji:`
        """

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}tribe_emoji Tribename :my_custom_emoji:`'.format(ctx.prefix))

        try:
            tribeflair = TribeFlair.upsert(name=tribe_name, guild_id=ctx.guild.id, emoji=emoji)
        except exceptions.CheckFailedError as e:
            return await ctx.send(e)

        await ctx.send('Tribe {0.tribe.name} updated with new emoji: {0.emoji}'.format(tribeflair))

    @commands.command(aliases=['addteam'], usage='new_team_name')
    # @commands.has_any_role(*mod_roles)
    async def team_add(self, ctx, *args):
        """Mod: Create new server Team
        The team should have a Role with an identical name.
        **Example:**
        `[p]team_add The Amazeballs`
        """

        name = ' '.join(args)
        try:
            with db.atomic():
                team = Team.create(name=name, guild_id=ctx.guild.id)
        except peewee.IntegrityError:
            return await ctx.send('That team already exists!')

        await ctx.send(f'Team {name} created! Starting ELO: {team.elo}. Players with a Discord Role exactly matching \"{name}\" will be considered team members. '
                f'You can now set the team flair with `{ctx.prefix}`team_emoji and `{ctx.prefix}team_image`.')

    @commands.command(usage='team_name new_emoji')
    # @commands.has_any_role(*mod_roles)
    async def team_emoji(self, ctx, team_name: str, emoji):
        """Mod: Assign an emoji to a team
        **Example:**
        `[p]team_emoji Amazeballs :my_fancy_emoji:`
        """

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

        with db:
            matching_teams = Team.get_by_name(team_name, ctx.guild.id)
            if len(matching_teams) != 1:
                return await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

            team = matching_teams[0]
            team.emoji = emoji
            team.save()

            await ctx.send('Team {0.name} updated with new emoji: {0.emoji}'.format(team))

    @commands.command(usage='team_name image_url')
    # @commands.has_any_role(*mod_roles)
    async def team_image(self, ctx, team_name: str, image_url):
        """Mod: Set a team's logo image

        **Example:**
        `[p]team_image Amazeballs http://www.path.to/image.png`
        """

        if 'http' not in image_url:
            return await ctx.send(f'Valid image url not detected. Example usage: `{ctx.prefix}team_image name http://url_to_image.png`')
            # This is a very dumb check to make sure user is passing a URL and not a random string. Assumes mod can figure it out from there.

        with db:
            matching_teams = Team.get_by_name(team_name, ctx.guild.id)
            if len(matching_teams) != 1:
                return await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

            team = matching_teams[0]
            team.image_url = image_url
            team.save()

            await ctx.send(f'Team {team.name} updated with new image_url (image should appear below)')
            await ctx.send(team.image_url)

    @commands.command(usage='old_name new_name')
    # @commands.has_any_role(*mod_roles)
    async def team_name(self, ctx, old_team_name: str, new_team_name: str):
        """Mod: Change a team's name
        The team should have a Role with an identical name.
        Old name doesn't need to be precise, but new name does. Include quotes if it's more than one word.
        **Example:**
        `[p]team_name Amazeballs "The Wowbaggers"`
        """

        with db:
            matching_teams = Team.get_by_name(old_team_name, ctx.guild.id)
            if len(matching_teams) != 1:
                return await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_name \"Current name\" \"New Team Name\"`'.format(ctx.prefix))

            team = matching_teams[0]
            team.name = new_team_name
            team.save()

            await ctx.send('Team **{}** has been renamed to **{}**.'.format(old_team_name, new_team_name))

    @commands.command()
    # @commands.has_any_role(*helper_roles)
    async def ts(self, ctx, name: str):

        # p = Game.load_full_game(game_id=1)
        # import datetime
        squad = SquadGame.get(id=1)
        await squad.create_channel(ctx)
        return

        print(len(q))
        for s in q.dicts():
            print(s)

    # @in_bot_channel()
    # TODO: searching. this is just bare bones 'show embed of game ID' currently
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
