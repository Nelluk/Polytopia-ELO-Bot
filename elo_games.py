import discord
import asyncio
# import websockets
from discord.ext import commands
import peewee
from models import db, Team, Game, Player, Lineup, Tribe, Squad, SquadGame
from bot import (helper_roles, mod_roles, bot_channels, logger, args,
                require_teams, command_prefix, game_request_channel,
                game_announce_channel, date_cutoff, game_channel_category)


def in_bot_channel():
    async def predicate(ctx):
        if bot_channels is None:
            return True
        if str(ctx.message.channel.id) in bot_channels:
            return True
        else:
            await ctx.send('This command can only be used in a designated bot channel.')
            return False
    return commands.check(predicate)


class ELOGamesCog:
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

    @commands.command(aliases=['namegame', 'game_name', 'name_game'])
    @commands.has_any_role(*helper_roles)
    async def gamename(self, ctx, game: poly_game, *args):

        if game is None:
            await ctx.send('No matching game was found.')
            return

        with db:
            new_game_name = ' '.join(args)
            game.name = new_game_name
            game.save()

        await ctx.send(f'Game ID {game.id} has been renamed to "{game.name}"')

    @commands.command(aliases=['endgame', 'win', 'winner'])
    @commands.has_any_role(*helper_roles)
    async def wingame(self, ctx, winning_game: poly_game, winning_side_name: str):

        if winning_game is None:
            await ctx.send(f'No matching game was found.')
            return

        with db:
            if winning_game.is_completed == 1:
                await ctx.send(f'Game with ID {winning_game.id} is already marked as completed with winning team {winning_game.winner.name}')
                return

            if winning_game.team_size > 1:
                # Game is a team game, so winner specified by team name
                matching_teams = Team.get_by_name(winning_side_name)
                if len(matching_teams) > 1:
                    await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
                    return
                if len(matching_teams) == 0:
                    await ctx.send(f'Cannot find a team with name "{winning_side_name}". Be sure to use the full name, surrounded by quotes if it is more than one word.')
                    return
                winning_team = matching_teams[0]

                if winning_team.id == winning_game.home_team.id:
                    losing_team = winning_game.away_team
                elif winning_team.id == winning_game.away_team.id:
                    losing_team = winning_game.home_team
                else:
                    await ctx.send('That team did not play in game {0.id}. The teams were {0.home_team.name} and {0.away_team.name}.'.format(winning_game))
                    return
            else:
                # Game is a 1v1 and winner is specified by player name
                matching_players = Player.get_by_string(winning_side_name)
                if len(matching_players) > 1:
                    await ctx.send('More than one matching player found. Be more specific or trying an @Mention.')
                    return
                if len(matching_players) == 0:
                    await ctx.send(f'Cannot find a player with name "{winning_side_name}". Try specifying with an @Mention.')
                    return
                winning_player = matching_players[0]
                winning_team, losing_team = None, None
                for lineup in winning_game.lineup:
                    if lineup.player == winning_player:
                        winning_team = lineup.team
                    else:
                        losing_team = lineup.team
                if winning_team is None:
                    await ctx.send(f'Player {winning_player.discord_name} did not play in game {winning_game.id}. Check `{command_prefix}game {winning_game.id}`.')
                    return

        # Check passed. Declare winner!
        with db:
            winning_game.declare_winner(winning_team, losing_team)

        winner_roster = winning_game.get_roster(winning_team)
        loser_roster = winning_game.get_roster(losing_team)

        player_mentions = [f'<@{p.discord_id}>' for p, _, _ in (winner_roster + loser_roster)]

        embed = game_embed(ctx, winning_game)
        await self.delete_game_channels(ctx, winning_game)

        if game_announce_channel is not None:
            channel = ctx.guild.get_channel(int(game_announce_channel))
            if channel is not None:
                await channel.send(f'Game concluded! Congrats **{winning_game.get_side_name("WIN")}**. Roster: {" ".join(player_mentions)}')
                await channel.send(embed=embed)
                await ctx.send(f'Game concluded! See {channel.mention} for full details.')
                return

        await ctx.send(f'Game concluded! Congrats team {winning_team.name}. Roster: {" ".join(player_mentions)}')
        await ctx.send(embed=embed)

    @commands.command(aliases=['request_game', 'requestgame', 'gamereq'])
    @commands.cooldown(2, 30, commands.BucketType.user)
    async def reqgame(self, ctx, *args):
        # Used so that users can submit game information to staff - bot will relay the text in the command to a specific channel.
        # Staff would then take action and create games
        if game_request_channel is None:
            return
        channel = ctx.guild.get_channel(int(game_request_channel))
        await channel.send(f'{ctx.message.author} submitted: {ctx.message.clean_content}')
        await ctx.send('Request has been logged')

    @commands.command(aliases=['newgame'])
    @commands.has_any_role(*helper_roles)
    async def startgame(self, ctx, game_name: str, *args):
        """startgame "Name of Game" @player1 @player2 VS @player3 @player4"""
        # TODO: Make game_name optional, would require custom parsing all the args and detecting when a game_name is there or not.

        if len(args) not in [3, 5, 7, 9, 11] or args[int(len(args) / 2)].upper() != 'VS':
            await ctx.send('Invalid format. Example usage for a 2v2 game: `{}startgame "Game Name" @player1 @player2 VS @player3 @player4`'.format(command_prefix))
            return

        side_home, side_away = [], []
        for p in args[:int(len(args) / 2)]:         # Args in first half before 'VS', converted to Discord Members
            guild_matches = await get_guild_member(ctx, p)
            if len(guild_matches) == 0:
                await ctx.send(f'Could not match "{p}" to a server member. Try using an @Mention.')
                return
            if len(guild_matches) > 1:
                await ctx.send(f'More than one server matches found for "{p}". Try being more specific or using an @Mention.')
                return
            side_home.append(guild_matches[0])

        for p in args[int(len(args) / 2) + 1:]:     # Args in second half after 'VS'
            guild_matches = await get_guild_member(ctx, p)
            if len(guild_matches) == 0:
                await ctx.send(f'Could not match "{p}" to a server member. Try using an @Mention.')
                return
            if len(guild_matches) > 1:
                await ctx.send(f'More than one server matches found for "{p}". Try being more specific or using an @Mention.')
                return
            side_away.append(guild_matches[0])

        if len(side_home + side_away) > len(set(side_home + side_away)):
            await ctx.send('Duplicate players detected. Are you sure this is what you want? (That means the two sides are uneven.)')

            # await ctx.send('Duplicate players detected. Example usage for a 2v2 game: `{}startgame "Name of Game" @player1 @player2 VS @player3 @player4`'.format(command_prefix))
            # return
            # # Disabling this check would be a decent way to enable uneven teams ie 2v1, with the same person listed twice on one side.

        home_team_flag, list_of_home_teams = get_teams_of_players(side_home)  # List of what server team each player is on, eg Ronin, Jets.
        away_team_flag, list_of_away_teams = get_teams_of_players(side_away)

        if (None in list_of_away_teams) or (None in list_of_home_teams):
            if require_teams is True:
                await ctx.send('One or more players listed cannot be matched to a Team (based on Discord Roles). Make sure player has exactly one matching Team role.')
                return
            else:
                # Set this to a home/away game if at least one player has no matching role, AND require_teams == false
                home_team_flag = away_team_flag = False

        if home_team_flag and away_team_flag:
            # If all players on both sides are playing with only members of their own Team (server team), those Teams are impacted by the game...
            home_side_team = list_of_home_teams[0]
            away_side_team = list_of_away_teams[0]

            if home_side_team == away_side_team:
                with db:
                    # If Team Foo is playing against another squad from Team Foo, reset them to 'Home' and 'Away'
                    home_side_team, _ = Team.get_or_create(name='Home', defaults={'emoji': ':stadium:'})
                    away_side_team, _ = Team.get_or_create(name='Away', defaults={'emoji': ':airplane:'})

        else:
            # Otherwise the players are "intermingling" and the game just influences two hidden teams in the database called 'Home' and 'Away'
            with db:
                home_side_team, _ = Team.get_or_create(name='Home', defaults={'emoji': ':stadium:'})
                away_side_team, _ = Team.get_or_create(name='Away', defaults={'emoji': ':airplane:'})

        logger.debug(f'All input checks passed. Creating new game records with args: {args}')
        with db:
            # Sanity checks all passed. Start a new game!
            newgame = Game.create(team_size=len(side_home), home_team=home_side_team, away_team=away_side_team, name=game_name)
            side_home_players = []
            side_away_players = []
            for player_discord, player_team in zip(side_home, list_of_home_teams):
                side_home_players.append(upsert_player_and_lineup(player_discord=player_discord, player_team=player_team, game_side=home_side_team, new_game=newgame)[0])

            for player_discord, player_team in zip(side_away, list_of_away_teams):
                side_away_players.append(upsert_player_and_lineup(player_discord=player_discord, player_team=player_team, game_side=away_side_team, new_game=newgame)[0])

            if len(side_home_players) > 1:
                Squad.upsert_squad(player_list=side_home_players, game=newgame, team=home_side_team)
                Squad.upsert_squad(player_list=side_away_players, game=newgame, team=away_side_team)

        if newgame.team_size > 1:
            await self.create_game_channels(ctx=ctx, game=newgame, home_players=side_home_players, away_players=side_away_players)

        mentions = [p.mention for p in side_home + side_away]
        embed = game_embed(ctx, newgame)

        if game_announce_channel is not None:
            channel = ctx.guild.get_channel(int(game_announce_channel))
            if channel is not None:
                await channel.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')
                await channel.send(embed=embed)
                await ctx.send(f'New game ID {newgame.id} started! See {channel.mention} for full details.')
                return
        await ctx.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')
        await ctx.send(embed=embed)

    async def delete_game_channels(self, ctx, game):

        if game_channel_category is None:
            return
        chan_category = discord.utils.get(ctx.guild.categories, id=int(game_channel_category))
        if chan_category is None:
            logger.error(f'In delete_game_channels - chans_category_id {game_channel_category} was supplied but cannot be loaded')
            return
        if ctx.guild.me.guild_permissions.manage_channels is not True:
            logger.error('In delete_game_channels - manage_channels permission is false.')
            return

        matching_chans = [c for c in chan_category.channels if c.name.startswith(f'e{game}-')]
        for chan in matching_chans:
            logger.warn(f'Deleting channel {chan.name}')
            await chan.delete(reason='Game concluded')

    async def create_game_channels(self, ctx, game, home_players, away_players):

        if game_channel_category is None:
            return
        chan_category = discord.utils.get(ctx.guild.categories, id=int(game_channel_category))
        if chan_category is None:
            logger.error(f'In create_game_channels - chans_category_id {game_channel_category} was supplied but cannot be loaded')
            return
        if ctx.guild.me.guild_permissions.manage_channels is not True:
            logger.error('In create_game_channels - manage_channels permission is false.')
            return

        home_string = f'{game.name}_{game.home_team.name}'
        away_string = f'{game.name}_{game.away_team.name}'
        home_chan_name = f'e{game.id}-{" ".join(home_string.replace("The", "").replace("the", "").split()).replace(" ", "-")}'
        away_chan_name = f'e{game.id}-{" ".join(away_string.replace("The", "").replace("the", "").split()).replace(" ", "-")}'
        # Turns game named 'The Mountain of Fire' to something like #e41-mountain-of-fire_ronin

        home_members = [ctx.guild.get_member(p.discord_id) for p in home_players]
        away_members = [ctx.guild.get_member(p.discord_id) for p in away_players]
        home_permissions, away_permissions = {}, {}
        perm = discord.PermissionOverwrite(read_messages=True, add_reactions=True, send_messages=True, attach_files=True)

        for m in home_members + [ctx.guild.me]:
            home_permissions[m] = perm
        for m in away_members + [ctx.guild.me]:
            away_permissions[m] = perm

        home_permissions[ctx.guild.default_role] = away_permissions[ctx.guild.default_role] = discord.PermissionOverwrite(read_messages=False)

        home_chan = await ctx.guild.create_text_channel(name=home_chan_name, overwrites=home_permissions, category=chan_category, reason='ELO Game chan')
        away_chan = await ctx.guild.create_text_channel(name=away_chan_name, overwrites=away_permissions, category=chan_category, reason='ELO Game chan')
        logger.debug(f'Created channels {home_chan.name} and {away_chan.name}')

        home_mentions, away_mentions = [p.mention for p in home_members], [p.mention for p in away_members]
        home_names, away_names = [p.discord_name for p in home_players], [p.discord_name for p in away_players]

        await home_chan.send(f'This is the team channel for game **{game.name}**.\n'
            f'This team is composed of {" / ".join(home_mentions)}\n'
            f'Your opponents are: {" / ".join(away_names)}\n\n'
            '*This channel will self-destruct as soon as the game is marked as concluded.*')
        await away_chan.send(f'This is the team channel for game **{game.name}**.\n'
            f'This team is composed of {" / ".join(away_mentions)}\n'
            f'Your opponents are: {" / ".join(home_names)}\n\n'
            '*This channel will self-destruct as soon as the game is marked as concluded.*')

    @commands.command(aliases=['incomplete'])
    async def incompletegames(self, ctx):
        """or incomplete: Lists oldest incomplete games"""
        incomplete_list = []
        for counter, game in enumerate(Game.select().where(Game.is_completed == 0).order_by(Game.date)[:500]):
            incomplete_list.append((
                f'{game.get_headline()}',
                f'{(str(game.date))} - {game.team_size}v{game.team_size}'
            ))

        await paginate(self.bot, ctx, title='**Oldest Incomplete Games**', message_list=incomplete_list, page_start=0, page_end=10, page_size=10)

    @commands.command()
    @commands.has_any_role(*mod_roles)
    async def deletegame(self, ctx, game: poly_game):
        """deletegame 5 (reverts ELO changes. Use with care.)"""

        if game is None:
            await ctx.send(f'No matching game was found.')
            return

        with db:
            gid = game.id
            game.delete_game()
            await ctx.send(f'Game with ID {gid} has been deleted and team/player ELO changes have been reverted, if applicable.')

    @in_bot_channel()
    @commands.command(aliases=['games'])
    async def game(self, ctx, *args):
        # Search games by ID#, name, team participation, or player participation. Show game detail card if 1 result, else a paginated list.

        try:
            game_id = int(''.join(args))
            game = Game.get(id=game_id)     # Argument is an int, so show game by ID
            embed = game_embed(ctx, game)
            await ctx.send(embed=embed)
            return
        except ValueError:
            pass
        except peewee.DoesNotExist:
            await ctx.send('Game with ID {} cannot be found.'.format(game_id))
            return

        team_matches, player_matches, game_entry_list = [], [], []

        game_matches = Game.select().where(Game.name.contains(' '.join(args)))
        for arg in args:
            teams = Team.get_by_name(arg)
            if len(teams) == 1:
                team_matches.append(teams[0])
            players = Player.get_by_string(arg)
            if len(players) == 1:
                player_matches.append(players[0])

        if len(team_matches + player_matches) + len(game_matches) == 0:
            await ctx.send(
                'Could not find any results. Example usage:\n'
                f'`{command_prefix}game 5` - Show details of game 5\n'
                f'`{command_prefix}game Ocean` - List games with "Ocean" in the name\n'
                f'`{command_prefix}game Ronin` - List games where Team Ronin participated\n'
                f'`{command_prefix}game Ronin Jets` - List games of Ronin vs Jets\n'
                f'`{command_prefix}game Nelluk` - List games where Nelluk participated\n'
                f'`{command_prefix}game Nelluk rickdaheals anarchoRex` - List games with all three players in the roster\n')
            return

        if len(game_matches) > 0:
            games = game_matches
        elif len(team_matches) == 1:
            games = Game.select().where((Game.away_team == team_matches[0]) | (Game.home_team == team_matches[0]))
        elif len(team_matches) == 2:
            games = Game.select().where(((Game.away_team == team_matches[0]) | (Game.home_team == team_matches[0])) & ((Game.away_team == team_matches[1]) | (Game.home_team == team_matches[1])))
        elif len(team_matches) > 2:
            await ctx.send('This command can only accept one or two team names.')
            return
        elif len(player_matches) > 0:
            q = Lineup.select(Game).join(Game).where(Lineup.player.in_(player_matches)).group_by(Lineup.game).having(peewee.fn.COUNT(Lineup.player) == len(player_matches))
            games = [lineup.game for lineup in q[:500]]
        else:
            logger.error(f'Unexpected input in games command: {team_matches} {player_matches}')
            await ctx.send('Unexpected error.')
            return

        if len(games) == 1:
            embed = game_embed(ctx, games[0])
            await ctx.send(embed=embed)
            return
        for game in games:
                if game.is_completed == 0:
                    status_str = 'Incomplete'
                else:
                    status_str = f'**WINNER:** {game.get_side_name(side="WIN")}'
                game_entry_list.append(
                    (game.get_headline(),
                    f'{(str(game.date))} - {game.team_size}v{game.team_size} - {status_str}'))
        await paginate(self.bot, ctx, title='**Search Results**', message_list=game_entry_list, page_start=0, page_end=10, page_size=10)

    @in_bot_channel()
    @commands.command(aliases=['teaminfo'])
    async def team(self, ctx, team_string: str):

        matching_teams = Team.get_by_name(team_string)
        if len(matching_teams) > 1:
            await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
            return
        if len(matching_teams) == 0:
            await ctx.send(f'Cannot find a team with name "{team_string}". Be sure to use the full name, surrounded by quotes if it is more than one word.')
            return
        team = matching_teams[0]

        team_role = discord.utils.get(ctx.guild.roles, name=team.name)
        team_members = [x.name for x in team_role.members]
        member_stats = []
        for member in team_role.members:
            # Create a list of members - pull ELO score from database if they are registered, or with 0 ELO if they are not
            try:
                p = Player.get(discord_id=member.id)
                member_stats.append((p.discord_name, p.elo, f'*({p.elo})*'))
            except peewee.DoesNotExist:
                member_stats.append((member.name, 0, '\u200b'))

        member_stats.sort(key=lambda tup: tup[1], reverse=True)     # sort the list descending by ELO
        members_sorted = [f'{x[0]}{x[2]}' for x in member_stats]    # create list of strings like Nelluk(1000)

        recent_games = Game.select().where(((Game.home_team == team) | (Game.away_team == team)) & (Game.team_size > 1)).order_by(-Game.date)[:10]
        wins, losses = team.get_record()

        embed = discord.Embed(title=f'Team card for **{team.name}** {team.emoji}')
        embed.add_field(name='Results', value=f'ELO: {team.elo}   Wins {wins} / Losses {losses}')
        embed.add_field(name=f'Members({len(team_members)})', value=f'{" / ".join(members_sorted)}')

        if team.image_url:
            embed.set_thumbnail(url=team.image_url)

        embed.add_field(value='*Recent games*', name='\u200b', inline=False)
        for game in recent_games:
            opponent = game.away_team if (game.home_team == team) else game.home_team
            if game.is_completed == 1:
                result = '**WIN**' if (game.winner == team) else 'LOSS'
            else:
                result = 'Incomplete'
            name_str = f' - {game.name} - ' if game.name else ''
            embed.add_field(
                name=f'Game {game.id} vs {opponent.name} {opponent.emoji} {name_str} {result}',
                value=f'{str(game.date)} - {game.team_size}v{game.team_size}', inline=False)

        await ctx.send(embed=embed)

    @in_bot_channel()
    @commands.command()
    async def squad(self, ctx, *args):
        # Provides list of squads that contain given members, or details on squad if only one match. Can also take ID as an argument.an

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
                p_matches = Player.get_by_string(p_name)
                if len(p_matches) == 1:
                    squad_players.append(p_matches[0])
                elif len(p_matches) > 1:
                    await ctx.send(f'Found multiple matches for player "{p_name}". Try being more specific or quoting players "Full Name".')
                    return
                else:
                    await ctx.send(f'Found no matches for player "{p_name}".')
                    return

            squad_list = Squad.get_all_matching_squads(squad_players)
            if len(squad_list) == 0:
                await ctx.send(f'Found no squads containing players: {" / ".join(args)}')
                return
            if len(squad_list) > 1:
                # More than one matching name found, so display a short list
                embed = discord.Embed(title=f'Found {len(squad_list)} matches. Try `{command_prefix}squad IDNUM`:')
                for squad in squad_list[:10]:
                    wins, losses = squad.get_record()
                    embed.add_field(
                        name=f'`ID {squad.id:>3} - {" / ".join(squad.get_names()):40}`',
                        value=f'`(ELO: {squad.elo}) W {wins} / L {losses}`',
                        inline=False
                    )
                await ctx.send(embed=embed)
                return

            # Exact matching squad found by player name
            squad = squad_list[0]

        wins, losses = squad.get_record()
        ranking_query = Squad.get_leaderboard().tuples()

        for rank, s in enumerate(ranking_query):
            if s[0] == squad.id:
                break
        if len(ranking_query) == 0:
                rank = -1

        embed = discord.Embed(title=f'Squad card for Squad {squad.id}\n{"  /  ".join(squad.get_names())}', value='\u200b')
        embed.add_field(name='Results', value=f'ELO: {squad.elo},  W {wins} / L {losses}', inline=True)
        embed.add_field(name='Ranking', value=f'{rank + 1} of {len(ranking_query)}', inline=True)
        recent_games = SquadGame.select().join(Game).where(SquadGame.squad == squad).order_by(-SquadGame.game.date)[:5]
        embed.add_field(value='\u200b', name='Most recent games', inline=False)

        for squad_game in recent_games:
            game = squad_game.game
            status = 'Completed' if game.is_completed == 1 else 'Incomplete'
            embed.add_field(name='Game {0.id}   {1.emoji} **{1.name}** *vs* **{2.name}** {2.emoji}'.format(game, game.home_team, game.away_team),
                            value='Status: {} - {}'.format(status, str(game.date)), inline=False)

        await ctx.send(embed=embed)

    @in_bot_channel()
    @commands.command(aliases=['playerinfo'])
    async def player(self, ctx, *args):
        with db:
            if len(args) == 0:
                # Player looking for info on themselves
                player = Player.get_by_string(f'<@{ctx.author.id}>')
                if len(player) != 1:
                    await ctx.send(f'Could not find you in the database. Try setting your code with {command_prefix}setcode')
                    return
                player = player[0]
            else:
                # Otherwise look for a player matching whatever theyentered
                player_mention = ' '.join(args)
                matching_players = Player.get_by_string(player_mention)
                if len(matching_players) == 1:
                    player = matching_players[0]
                elif len(matching_players) == 0:
                    # No matching name in database. Fall back to searching on polytopia_id or polytopia_name. Warn if player is found in guild.
                    matches = await get_guild_member(ctx, player_mention)
                    if len(matches) > 0:
                        await ctx.send(f'"{player_mention}" was found in the server but is not registered with me. '
                            f'Players can be registered with `{command_prefix}setcode` or being in a new game\'s lineup.')

                    try:
                        player = Player.select().where((Player.polytopia_id.contains(player_mention)) | (Player.polytopia_name.contains(player_mention))).get()
                    except peewee.DoesNotExist:
                        await ctx.send(f'Could not find \"{player_mention}\" by Discord name, Polytopia name, or Polytopia ID.')
                        return
                else:
                    await ctx.send('There is more than one player found with that name. Specify user with @Mention.'.format(player_mention))
                    return

        with db:
            wins, losses = player.get_record()
            ranked_players_query = Player.get_leaderboard(date_cutoff=date_cutoff).tuples()

            if len(ranked_players_query) == 0:
                counter = -1
            else:
                for counter, p in enumerate(ranked_players_query):
                    if p[0] == player.id:
                        break
                    # counter should now equal ranking of player in the leaderboard

            recent_games = Game.select().join(Lineup).where(Lineup.player == player).order_by(-Game.date)[:7]

            embed = discord.Embed(title=f'Player card for {player.discord_name}')
            embed.add_field(name='Results', value=f'ELO: {player.elo}, W {wins} / L {losses}')
            embed.add_field(name='Ranking', value=f'{counter + 1} of {len(ranked_players_query)}')
            guild_member = ctx.guild.get_member(player.discord_id)
            embed.set_thumbnail(url=guild_member.avatar_url_as(size=512))
            if player.team:
                team_str = f'{player.team.name} {player.team.emoji}' if player.team.emoji else player.team.name
                embed.add_field(name='Last-known Team', value=team_str)
            if player.polytopia_name:
                embed.add_field(name='Polytopia Game Name', value=player.polytopia_name)
            if player.polytopia_id:
                embed.add_field(name='Polytopia ID', value=player.polytopia_id)
                content_str = player.polytopia_id
                # Used as a single message before player card so users can easily copy/paste Poly ID
            else:
                content_str = ''

            embed.add_field(value='\u200b', name='Most recent games', inline=False)
            for game in recent_games:
                status = 'Completed' if game.is_completed == 1 else 'Incomplete'
                if game.is_completed == 0:
                    status = 'Incomplete'
                else:
                    player_team = Lineup.select().where((Lineup.game == game) & (Lineup.player == player)).get().team
                    status = '**WIN**' if player_team == game.winner else '***Loss***'
                embed.add_field(name=f'{game.get_headline()}',
                                value=f'{status} - {str(game.date)} - {game.team_size}v{game.team_size}')

            await ctx.send(content=content_str, embed=embed)

    @in_bot_channel()
    @commands.command(aliases=['lbteam', 'leaderboardteam'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def leaderboard_team(self, ctx):
        """or lbteam : shows team leaderboard"""

        embed = discord.Embed(title='**Team Leaderboard**')
        with db:
            for counter, team in enumerate(Team.select().order_by(-Team.elo).where((Team.name != 'Home') & (Team.name != 'Away'))):
                team_role = discord.utils.get(ctx.guild.roles, name=team.name)
                team_name_str = f'{team.name}({len(team_role.members)})'  # Show team name with number of members
                wins, losses = team.get_record()
                embed.add_field(name=f'`{(counter + 1):>3}. {team_name_str:30}  (ELO: {team.elo:4})  W {wins} / L {losses}` {team.emoji}', value='\u200b', inline=False)
        await ctx.send(embed=embed)

    @in_bot_channel()
    @commands.command(aliases=['leaderboard', 'lbi', 'lb'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def leaderboard_individual(self, ctx):

        leaderboard = []
        with db:
            leaderboard_query = Player.get_leaderboard(date_cutoff=date_cutoff)
            for counter, player in enumerate(leaderboard_query[:500]):
                wins, losses = player.get_record()
                emoji_str = player.team.emoji if player.team else ''
                leaderboard.append(
                    (f'`{(counter + 1):>3}.` {emoji_str}`{player.discord_name}`', f'`(ELO: {player.elo:4}) W {wins} / L {losses}`')
                )

        await paginate(self.bot, ctx, title='**Individual Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @in_bot_channel()
    @commands.command(aliases=['lbsquad', 'leaderboardsquad'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def leaderboard_squad(self, ctx):

        leaderboard = []
        with db:
            squads = Squad.get_leaderboard()
            for counter, sq in enumerate(squads[:200]):
                wins, losses = sq.get_record()
                squad_members = sq.get_members()
                emoji_list = [p.team.emoji for p in squad_members if p.team is not None]
                emoji_string = ' '.join(emoji_list)
                squad_names = ' / '.join(sq.get_names())
                leaderboard.append(
                    (f'`{(counter + 1):>3}.` {emoji_string}`{squad_names}`', f'`(ELO: {sq.elo:4}) W {wins} / L {losses}`')
                )
        await paginate(self.bot, ctx, title='**Squad Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @commands.command()
    async def setcode(self, ctx, *args):

        if len(args) == 1:      # User setting code for themselves. No special permissions required.
            target_discord_member = ctx.message.author
            new_id = args[0]

        elif len(args) == 2:    # User changing another user's code. Helper permissions required.
            if len(get_matching_roles(ctx.author, helper_roles)) == 0:
                await ctx.send(f'You only have permission to set your own code. To do that use `{command_prefix}setcode YOURCODEHERE`')
                return

            # Try to find matching guild/server member
            guild_matches = await get_guild_member(ctx, args[0])
            if len(guild_matches) == 0:
                await ctx.send(f'Could not find any server member matching "{args[0]}". Try specifying with an @Mention')
                return
            elif len(guild_matches) > 1:
                await ctx.send(f'Found multiple server members matching "{args[0]}". Try specifying with an @Mention')
                return
            target_discord_member = guild_matches[0]
            new_id = args[1]
        else:
            # Unexpected input
            await ctx.send(f'Wrong number of arguments. Use `{command_prefix}setcode my_polytopia_code`')
            return

        if len(new_id) != 16 or new_id.isalnum() is False:
            # Very basic polytopia code sanity checking. Making sure it is 16-character alphanumeric.
            await ctx.send(f'Polytopia code "{new_id}" does not appear to be a valid code.')
            return

        _, team_list = get_teams_of_players([target_discord_member])
        with db:
            player, created = upsert_player_and_lineup(player_discord=target_discord_member, player_team=team_list[0], game_side=None, new_game=None)
            player.polytopia_id = new_id
            player.save()

        if created:
            await ctx.send('Player {0.discord_name} added to system with Polytopia code {0.polytopia_id} and ELO {0.elo}'.format(player))
        else:
            await ctx.send('Player {0.discord_name} updated in system with Polytopia code {0.polytopia_id}.'.format(player))

    @commands.command(aliases=['code'])
    async def getcode(self, ctx, player_string: str):

        with db:
            player_matches = Player.get_by_string(player_string)
            if len(player_matches) == 0:
                await ctx.send('Cannot find player with that name. Correct usage: `{}getcode @Player`'.format(command_prefix))
                return
            if len(player_matches) > 1:
                await ctx.send('More than one matching player found. Use @player to specify. Correct usage: `{}getcode @Player`'.format(command_prefix))
                return
            player_target = player_matches[0]
            if player_target.polytopia_id:
                await ctx.send(player_target.polytopia_id)
            else:
                await ctx.send('User was found but does not have a Polytopia ID on file.')

    @commands.command()
    async def setname(self, ctx, *args):
        if len(args) == 1:
            # User setting code for themselves. No special permissions required.
            target_player = Player.get_by_string(f'<@{ctx.author.id}>')
            if len(target_player) != 1:
                await ctx.send(f'Player with name {ctx.author.name} is not in the system. Try registering with {command_prefix}setcode first.')
                return
            new_name = args[0]
        elif len(args) == 2:
            # User changing another user's code. Admin permissions required.
            if len(get_matching_roles(ctx.author, helper_roles)) == 0:
                await ctx.send('You do not have permission to trigger this command.')
                return

            target_player = Player.get_by_string(args[0])
            if len(target_player) != 1:
                await ctx.send(f'Player with name {ctx.author.name} is not in the system. Try registering with {command_prefix}setcode first.')
                return
            new_name = args[1]
        else:
            # Unexpected input
            await ctx.send(f'Wrong number of arguments. Use `{command_prefix}setname my_polytopia_name`')
            return

        with db:
            target_player[0].polytopia_name = new_name
            target_player[0].save()
            await ctx.send('Player {0.discord_name} updated in system with Polytopia name {0.polytopia_name}.'.format(target_player[0]))

    @commands.command(aliases=['set_tribe', 'tribe'])
    @commands.has_any_role(*helper_roles)
    async def settribe(self, ctx, game: poly_game, player_name, tribe_name):

        if game is None:
            await ctx.send(f'No matching game was found.')
            return

        with db:
            matching_tribes = Tribe.select().where(Tribe.name.contains(tribe_name))
            if len(matching_tribes) != 1:
                await ctx.send('Matching Tribe not found, or too many found. Check spelling or be more specific.')
                return
            tribe = matching_tribes[0]

            players = Player.get_by_string(player_name)

            if len(players) == 0:
                await ctx.send('Could not find matching player.')
                return

            if len(players) > 1:
                await ctx.send('More than one player with that name found. Try using @mention.')
                # Could improve this by only searching for players within a game's lineup, but that would be a decent amount of work
                return

            lineups = Lineup.select().join(Player).where((Player.id == players[0]) & (Lineup.game == game))
            if len(lineups) != 1:
                await ctx.send(f'Could not match player {player_name} to game {game.id}.')
                return

        with db:
            lineups[0].tribe = tribe
            lineups[0].save()
            emoji_str = tribe.emoji if tribe.emoji is not None else ''
            await ctx.send(f'Player {players[0].discord_name} assigned to tribe {tribe.name} in game {game.id} {emoji_str}')

    @commands.command()
    @commands.has_any_role(*mod_roles)
    async def tribe_emoji(self, ctx, tribe_name: str, emoji):

        if len(emoji) != 1 and ('<:' not in emoji):
            await ctx.send('Valid emoji not detected. Example: `{}tribe_emoji Tribename :my_custom_emoji:`'.format(command_prefix))
            return

        with db:
            matching_tribes = Tribe.select().where(Tribe.name.contains(tribe_name))
            if len(matching_tribes) != 1:
                await ctx.send('Matching Tribe not found, or too many found. Check spelling or be more specific.')
                return
            tribe = matching_tribes[0]

            tribe.emoji = emoji
            tribe.save()

            await ctx.send('Tribe {0.name} updated with new emoji: {0.emoji}'.format(tribe))

    @commands.command(aliases=['addteam'])
    @commands.has_any_role(*mod_roles)
    async def team_add(self, ctx, *args):
        # Team name is expected to match the name of a discord Role, so bot can automatically tell what team a player is in
        name = ' '.join(args)
        try:
            db.connect()
            team = Team.create(name=name)
            await ctx.send(f'Team {name} created! Starting ELO: {team.elo}. Players with a Discord Role exactly matching \"{name}\" will be considered team members. '
                f'You can now set the team flair with `{command_prefix}`team_emoji and `{command_prefix}team_image`.')
        except peewee.IntegrityError:
            await ctx.send('That team already exists!')
        db.close()

    @commands.command()
    @commands.has_any_role(*mod_roles)
    async def team_emoji(self, ctx, team_name: str, emoji):

        if len(emoji) != 1 and ('<:' not in emoji):
            await ctx.send('Valid emoji not detected. Example: `{}team_emoji name :my_custom_emoji:`'.format(command_prefix))
            return

        with db:
            matching_teams = Team.get_by_name(team_name)
            if len(matching_teams) != 1:
                await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_emoji name :my_custom_emoji:`'.format(command_prefix))
                return

            team = matching_teams[0]
            team.emoji = emoji
            team.save()

            await ctx.send('Team {0.name} updated with new emoji: {0.emoji}'.format(team))

    @commands.command()
    @commands.has_any_role(*mod_roles)
    async def team_image(self, ctx, team_name: str, image_url):

        if 'http' not in image_url:
            await ctx.send(f'Valid image url not detected. Example usage: `{command_prefix}team_image name http://url_to_image.png`')
            # This is a very dumb check to make sure user is passing a URL and not a random string. Assumes mod can figure it out from there.
            return

        with db:
            matching_teams = Team.get_by_name(team_name)
            if len(matching_teams) != 1:
                await ctx.send(f'Can\'t find matching team or too many matches. Example: `{command_prefix}team_image name http://url_to_image.png`')
                return

            team = matching_teams[0]
            team.image_url = image_url
            team.save()

            await ctx.send(f'Team {team.name} updated with new image_url (image should appear below)')
            await ctx.send(team.image_url)

    @commands.command()
    @commands.has_any_role(*mod_roles)
    async def team_name(self, ctx, old_team_name: str, new_team_name: str):

        with db:
            try:
                team = Team.get(name=old_team_name)
            except peewee.DoesNotExist:
                await ctx.send('That team can not be found. Be sure to use the full team name. Example: `{}team_name \"Current name\" \"New Team Name\"`'.format(command_prefix))
                return

            team.name = new_team_name
            team.save()

            await ctx.send('Team **{}** has been renamed to **{}**.'.format(old_team_name, new_team_name))

    @in_bot_channel()
    @commands.command(aliases=['elohelp'])
    async def help(self, ctx):
        commands = [('lb', 'Show individual leaderboard'),
                    ('lbteam', 'Show team leaderboard'),
                    ('lbsquad', 'Show squad leaderboard'),
                    ('team `name`', 'Display stats for a given team.'),
                    ('player @player', 'Display stats for a given player. Also lets you search by game code/name.'),
                    ('game `SEARCH`', 'Search for games. Examples:\n`game 52` - Show details on game 52\n`game Ocean` - Show all games with "Ocean" in name\nCan also accept a list of team or player names!'),
                    ('squad `LIST OF PLAYERS`', 'Show squads containing given members - or detailed squad info if only one match.'),
                    ('setcode `POLYTOPIACODE`', 'Register your code with the bot for others to find. Also will place you on the leaderboards.'),
                    ('setname `IN-GAME NAME`', 'Register your in-game name with the bot for others to find.'),
                    ('getcode `PLAYER`', 'Simply return the Polytopia code of anyone registered.'),
                    ('incomplete', 'List oldest games with no declared winner'),
                    ('help_staff', 'Display helper commands, if allowed')]

        if game_request_channel is not None:
            commands.append(('reqgame `"Name of Game" player1 player2 VS player3 player4`', 'Submit game details to staff to be added to the bot. Include tribe emoji if known.'))
            commands.append(('reqgame `GAMEID won by [player/team name]`', 'Submit game results to staff to update an existing game.'))

        embed = discord.Embed(title='**ELO Bot Help**')
        for command, desc in commands:
            embed.add_field(name='{}{}'.format(command_prefix, command), value=desc, inline=False)
        await ctx.send(embed=embed)

    @in_bot_channel()
    @commands.command(aliases=['help-staff'])
    @commands.has_any_role(*helper_roles)
    async def help_staff(self, ctx):
        commands = [('newgame "Name of Game" @player1 @player2 VS @player3 @player4', 'Start a new game between listed players.\n`Aliases: startgame`'),
                    ('wingame `GAMEID WINNING-TEAM-OR-PLAYER`', f'Declare winner of open game. Eg `{command_prefix}win 45 Ronin`\n`Aliases: win, winner`'),
                    ('namegame `GAMEID` \"Name of Game\"', 'Store Polytopia in-game name for this match`'),
                    ('setcode `@user POLYTOPIACODE`', 'Change or add the code of another user to the bot.'),
                    ('setname `PLAYER IN-GAME-NAME`', 'Change or add the in-game name of another user to the bot.'),
                    ('settribe `GAMEID PLAYER TRIBENAME`', 'Mark what tribe a player has chosen in a given game.\nExample: `{}settribe 5 Nelluk Bardur`'.format(command_prefix))]

        mod_commands = [('deletegame `GAMEID`', 'Delete game and roll back relevant ELO changes'),
                        ('team_add \"Team Name\"', 'Add team to bot. Be sure to use full name - must have a matching **Discord role** of identical name.'),
                        ('team_emoji `name :emoji-code:`', 'Set an emoji to be associated with a team.'),
                        ('team_image `name http://image-url.png`', 'Set an image to be associated with a team.'),
                        ('tribe_emoji `TRIBENAME :emoji-code:`', 'Set an emoji to be associated with a Polytopia tribe.'),
                        ('team_name \"current team name\" \"New Team Name\"', 'Change a team name.')]

        embed = discord.Embed(title='**ELO Bot Help - Staff Commands**')
        for command, desc in commands:
            embed.add_field(name='{}{}'.format(command_prefix, command), value=desc, inline=False)
        embed.add_field(name='*Mod-only commands below*', value='\u200b', inline=False)
        for command, desc in mod_commands:
            embed.add_field(name='{}{}'.format(command_prefix, command), value=desc, inline=False)
        await ctx.send(embed=embed)


def initialize_data():
    team_list = [('The Ronin', ':spy:', 'https://media.discordapp.net/attachments/471128500338819072/471941775142158346/neworange.png'),
                    ('The Jets', ':airplane:', 'https://media.discordapp.net/attachments/471128500338819072/471941513241427968/newpurple.png'),
                    ('The Lightning', ':cloud_lightning:', 'https://media.discordapp.net/attachments/471128500338819072/471941648499081217/teamyellow.png'),
                    ('The Sparkies', ':dog:', 'https://media.discordapp.net/attachments/471128500338819072/471941823900942347/newbrown.png'),
                    ('The Mallards', ':duck:', 'https://media.discordapp.net/attachments/471128500338819072/471941726139973663/newgreen.png'),
                    ('The Cosmonauts', ':space_invader:', 'https://media.discordapp.net/attachments/471128500338819072/471941440797278218/newmagenta.png'),
                    ('The Wildfire', ':fire:', 'https://media.discordapp.net/attachments/471128500338819072/471941893371199498/newred.png'),
                    ('The Bombers', ':bomb:', 'https://media.discordapp.net/attachments/471128500338819072/471941345842298881/newblue.png'),
                    ('The Plague', ':nauseated_face:', 'https://media.discordapp.net/attachments/471128500338819072/471941955933306900/theplague.png'),
                    ('The Crawfish', ':fried_shrimp:', 'https://media.discordapp.net/attachments/471128500338819072/481290788261855232/red-crawfish.png'),
                    ('Home', ':stadium:', None),
                    ('Away', ':airplane:', None)]

    tribe_list = ['Bardur', 'Imperius', 'Xin-Xi', 'Oumaji', 'Kickoo', 'Hoodrick', 'Luxidoor', 'Vengir', 'Zebasi', 'Ai-Mo', 'Quetzali', 'Aquarion', 'Elyrion']

    db.connect()
    for team, emoji, image_url in team_list:
        try:
            print(f'Adding team{team}')
            logger.debug(f'Adding team{team}')
            team = Team.create(name=team, emoji=emoji, image_url=image_url)
        except peewee.IntegrityError:
            pass
    for tribe in tribe_list:
        try:
            print(f'Adding tribe{tribe}')
            logger.debug(f'Adding tribe{tribe}')
            Tribe.create(name=tribe)
        except peewee.IntegrityError:
            pass
    db.close()


def get_matching_roles(discord_member, list_of_role_names):
        # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.polytopia_id
        member_roles = [x.name for x in discord_member.roles]
        return set(member_roles).intersection(list_of_role_names)


def get_teams_of_players(list_of_players):
    # given [List, Of, discord.Member, Objects] - return a, b
    # a = binary flag if all members are on the same Poly team. b = [list] of the Team objects from table the players are on
    # input: [Nelluk, Frodakcin]
    # output: True, [<Ronin>, <Ronin>]

    with db:
        query = Team.select()
        list_of_teams = [team.name for team in query]               # ['The Ronin', 'The Jets', ...]
        list_of_matching_teams = []
        for player in list_of_players:
            matching_roles = get_matching_roles(player, list_of_teams)
            if len(matching_roles) == 1:
                name = next(iter(matching_roles))
                list_of_matching_teams.append(Team.get(Team.name == name))
            else:
                list_of_matching_teams.append(None)
                # Would be here if no player Roles match any known teams, -or- if they have more than one match

        same_team_flag = True if all(x == list_of_matching_teams[0] for x in list_of_matching_teams) else False
        return same_team_flag, list_of_matching_teams


def upsert_player_and_lineup(player_discord, player_team, game_side=None, new_game=None):

        if player_discord.nick:
            if player_discord.name in player_discord.nick:
                display_name = player_discord.nick
            else:
                display_name = f'{player_discord.name} ({player_discord.nick})'
        else:
            display_name = player_discord.name

        player, created = Player.get_or_create(discord_id=player_discord.id, defaults={'discord_name': display_name, 'team': player_team})
        if not created:
            player.team = player_team    # update team with existing player in db in case they have been traded
            player.discord_name = display_name
            player.save()
            logger.debug('Player {player.discord_name} updated')
        if new_game is not None:
            Lineup.create(game=new_game, player=player, team=game_side)
            logger.debug('Player {player.discord_name} inserted')
        return player, created


async def get_guild_member(ctx, input):

        # Find matching Guild member by @Mention or Name. Fall back to case-insensitive search

        guild_matches, substring_matches = [], []
        try:
            guild_matches.append(await commands.MemberConverter().convert(ctx, input))
        except commands.errors.BadArgument:
            pass
            # No matches in standard MemberConverter. Move on to a case-insensitive search.
            for p in ctx.guild.members:
                name_str = p.nick.upper() + p.name.upper() if p.nick else p.name.upper()
                if p.name.upper() == input.upper():
                    guild_matches.append(p)
                if input.upper() in name_str:
                    substring_matches.append(p)

            if len(guild_matches) > 0:
                return guild_matches
            if len(input) > 3:
                return substring_matches

        return guild_matches


def game_embed(ctx, game):

        home_side_team = game.home_team
        away_side_team = game.away_team
        side_home_roster = game.get_roster(home_side_team)
        side_away_roster = game.get_roster(away_side_team)

        game_headline = game.get_headline()
        game_headline = game_headline.replace('\u00a0', '\n')   # Put game.name onto its own life if its there

        embed = discord.Embed(title=game_headline)

        game_status = 'Incomplete'
        if game.is_completed == 1:
            game_status = 'Completed'
            embed.title += f'\n\nWINNER: {game.get_side_name(side="WIN")}'

            if game.team_size == 1:
                winning_player = Lineup.select().where((Lineup.game == game) & (Lineup.team == game.winner)).get().player
                winning_member = ctx.guild.get_member(winning_player.discord_id)
                embed.set_thumbnail(url=winning_member.avatar_url_as(size=512))

            elif game.winner.image_url:
                embed.set_thumbnail(url=game.winner.image_url)

        # TEAM ELOs and ELO DELTAS
        if home_side_team.name != 'Home' and away_side_team.name != 'Away':
            if game.is_completed == 1:
                if game.winner == home_side_team:
                    home_delta_string = f'+{game.winner_delta}'
                    away_delta_string = f'{game.loser_delta}'
                else:
                    home_delta_string = f'{game.loser_delta}'
                    away_delta_string = f'+{game.winner_delta}'
                home_elo_str = f' ({home_side_team.elo} {home_delta_string})'
                away_elo_str = f' ({away_side_team.elo} {away_delta_string})'
            else:
                home_elo_str = f'({home_side_team.elo})'
                away_elo_str = f'({away_side_team.elo})'
        else:
            # Hide team ELO if its just generic Home/Away
            home_elo_str = away_elo_str = ''

        # SQUAD ELOs and ELO DELTAS
        if len(side_home_roster) > 1:
            home_player_list = [x[0] for x in side_home_roster]
            away_player_list = [x[0] for x in side_away_roster]

            home_squad = Squad.get_matching_squad(home_player_list)[0]
            away_squad = Squad.get_matching_squad(away_player_list)[0]
            home_squad_str = f'Squad ELO: {home_squad.elo}'
            away_squad_str = f'Squad ELO: {away_squad.elo}'
            if game.is_completed == 1:
                home_squad_delta = SquadGame.select().where((SquadGame.game == game) & (SquadGame.squad == home_squad)).get().elo_change
                away_squad_delta = SquadGame.select().where((SquadGame.game == game) & (SquadGame.squad == away_squad)).get().elo_change
                if game.winner == home_side_team:
                    home_squad_str += f' (+{home_squad_delta})'
                    away_squad_str += f' ({away_squad_delta})'
                else:
                    home_squad_str += f' ({home_squad_delta})'
                    away_squad_str += f' (+{away_squad_delta})'
        else:
            home_squad_str = away_squad_str = '\u200b'

        game_data = [(home_side_team, home_elo_str, home_squad_str, side_home_roster), (away_side_team, away_elo_str, away_squad_str, side_away_roster)]

        for team, elo_str, squad_str, roster in game_data:
            if game.team_size > 1:
                embed.add_field(name=f'Lineup for Team **{team.name}**{elo_str}', value=squad_str, inline=False)

            for player, elo_delta, tribe_emoji in roster:
                if elo_delta == 0:
                    p_delta_str = ''
                elif elo_delta > 0:
                    p_delta_str = f' (+{elo_delta})'
                else:
                    p_delta_str = f' ({elo_delta})'

                embed.add_field(name=f'**{player.discord_name}** {tribe_emoji}', value=f'ELO: {player.elo}{p_delta_str}', inline=True)

            embed.add_field(value='\u200b', name=' \u200b', inline=False)

        embed.set_footer(text=f'Status: {game_status}  -  Creation Date {str(game.date)}')
        return embed
        # await ctx.send(embed=embed)


async def paginate(bot, ctx, title, message_list, page_start=0, page_end=10, page_size=10):
    # Allows user to page through a long list of messages with reactions

    page_end = page_end if len(message_list) > page_end else len(message_list)

    first_loop = True
    while True:
        embed = discord.Embed(title=title)
        for entry in range(page_start, page_end):
            embed.add_field(name=message_list[entry][0], value=message_list[entry][1], inline=False)

        if first_loop is True:
            sent_message = await ctx.send(embed=embed)
        else:
            try:
                await sent_message.clear_reactions()
            except (discord.ext.commands.errors.CommandInvokeError, discord.errors.Forbidden):
                logger.warn('Unable to clear message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
            await sent_message.edit(embed=embed)

        if page_start > 0:
            await sent_message.add_reaction('⏪')
        if page_end < len(message_list):
            await sent_message.add_reaction('⏩')

        def check(reaction, user):
            e = str(reaction.emoji)
            if page_start > 0 and page_end < len(message_list):
                compare = e.startswith(('⏪', '⏩'))
            elif page_end >= len(message_list):
                compare = e.startswith('⏪')
            elif page_start <= 0:
                compare = e.startswith('⏩')
            else:
                compare = False
            return ((user == ctx.message.author) and (reaction.message.id == sent_message.id) and compare)

        try:
            reaction, user = await bot.wait_for('reaction_add', timeout=20.0, check=check)
        except asyncio.TimeoutError:
            try:
                await sent_message.clear_reactions()
            except (discord.ext.commands.errors.CommandInvokeError, discord.errors.Forbidden):
                logger.debug('Unable to clear message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
            finally:
                break
        else:
            if '⏪' in str(reaction.emoji):

                page_start = 0 if (page_start - page_size < 0) else (page_start - page_size)
                page_end = page_start + page_size if (page_start + page_size <= len(message_list)) else len(message_list)

            elif '⏩' in str(reaction.emoji):

                page_end = len(message_list) if (page_end + page_size > len(message_list)) else (page_end + page_size)
                page_start = page_end - page_size if (page_end - page_size) >= 0 else 0

            first_loop = False


if args.add_default_data:
    initialize_data()
    exit(0)


def setup(bot):
    bot.add_cog(ELOGamesCog(bot))
