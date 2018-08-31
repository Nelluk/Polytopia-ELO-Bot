import discord
import asyncio
import websockets
from discord.ext import commands
import peewee
from models import db, Team, Game, Player, Lineup, Tribe, Squad, SquadGame, SquadMember
from bot import helper_roles, mod_roles, date_cutoff, bot_channels, logger, args, require_teams, command_prefix


def in_bot_channel():
    async def predicate(ctx):
        if bot_channels is None:
            return True
        return str(ctx.message.channel.id) in bot_channels
    return commands.check(predicate)


class ELOGamesCog:
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['namegame'])
    @commands.has_any_role(*helper_roles)
    async def gamename(self, ctx, game_id: int, *args):

        with db:
            try:
                game = Game.get(id=game_id)
            except peewee.DoesNotExist:
                await ctx.send(f'Game with ID {game_id} cannot be found.')
                return

            new_game_name = ' '.join(args)
            game.name = new_game_name
            game.save()

        await ctx.send(f'Game ID {game_id} has been renamed to {game.name}')

    @commands.command(aliases=['endgame', 'win', 'winner'])
    @commands.has_any_role(*helper_roles)
    async def wingame(self, ctx, game_id: int, winning_team_name: str):
        """wingame 5 \"The Ronin\""""

        with db:
            try:
                winning_game = Game.get(id=game_id)
            except peewee.DoesNotExist:
                await ctx.send(f'Game with ID {game_id} cannot be found.')
                return

            if winning_game.is_completed == 1:
                await ctx.send(f'Game with ID {game_id} is already marked as completed with winning team {winning_game.winner.name}')
                return

            matching_teams = get_team_from_name(winning_team_name)
            if len(matching_teams) > 1:
                await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
                return
            if len(matching_teams) == 0:
                await ctx.send(f'Cannot find a team with name "{winning_team_name}". Be sure to use the full name, surrounded by quotes if it is more than one word.')
                return
            winning_team = matching_teams[0]

            if winning_team.id == winning_game.home_team.id:
                losing_team = winning_game.away_team
            elif winning_team.id == winning_game.away_team.id:
                losing_team = winning_game.home_team
            else:
                await ctx.send('That team did not play in game {0.id}. The teams were {0.home_team.name} and {0.away_team.name}.'.format(winning_game))
                return

            winning_game.declare_winner(winning_team, losing_team)

            winner_roster = winning_game.get_roster(winning_team)
            loser_roster = winning_game.get_roster(losing_team)

            player_mentions = [f'<@{p.discord_id}>' for p, _, _ in (winner_roster + loser_roster)]
            await ctx.send(f'Game concluded! Congrats team {winning_team.name}. Roster: {" ".join(player_mentions)}')
            await game_embed(ctx, winning_game)

    @commands.command(aliases=['newgame'])
    @commands.has_any_role(*helper_roles)
    async def startgame(self, ctx, *args):
        """startgame @player1 @player2 VS @player3 @player4"""

        if len(args) not in [3, 5, 7, 9, 11] or args[int(len(args) / 2)].upper() != 'VS':
            await ctx.send('Invalid format. Example usage for a 2v2 game: `{}startgame @player1 @player2 VS @player3 @player4`'.format(command_prefix))
            return

        side_home = args[:int(len(args) / 2)]
        side_away = args[int(len(args) / 2) + 1:]

        if len(side_home + side_away) > len(set(side_home + side_away)):
            await ctx.send('Duplicate players detected. Example usage for a 2v2 game: `{}startgame @player1 @player2 VS @player3 @player4`'.format(command_prefix))
            # Disabling this check would be a decent way to enable uneven teams ie 2v1, with the same person listed twice on one side.
            return

        side_home = [get_member_from_mention(self.bot, x) for x in side_home]
        side_away = [get_member_from_mention(self.bot, x) for x in side_away]

        if None in side_home or None in side_away:
            await ctx.send('Command included invalid player. Example usage for a 2v2 game: `{}startgame @player1 @player2 VS @player3 @player4`'.format(command_prefix))
            return

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
            newgame = Game.create(team_size=len(side_home), home_team=home_side_team, away_team=away_side_team)
            side_home_players = []
            side_away_players = []
            for player_discord, player_team in zip(side_home, list_of_home_teams):
                side_home_players.append(upsert_player_and_lineup(player_discord=player_discord, player_team=player_team, game_side=home_side_team, new_game=newgame)[0])

            for player_discord, player_team in zip(side_away, list_of_away_teams):
                side_away_players.append(upsert_player_and_lineup(player_discord=player_discord, player_team=player_team, game_side=away_side_team, new_game=newgame)[0])

            if len(side_home_players) > 1:
                home_squad = Squad.upsert_squad(player_list=side_home_players, game=newgame, team=home_side_team)
                away_squad = Squad.upsert_squad(player_list=side_away_players, game=newgame, team=away_side_team)

        mentions = [p.mention for p in ctx.message.mentions]
        await ctx.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')
        await game_embed(ctx, newgame)

    @commands.command(aliases=['gameinfo'])
    async def game(self, ctx, *args):
        # TODO: Handle searching by game-name. Present list of games (similar to 'incomplete' command) if there are multiple

        try:
            game_id = int(''.join(args))
            game = Game.get(id=game_id)
            await game_embed(ctx, game)
            return
        except ValueError:
            game_name = ' '.join(args)
            # Args is not an int, which means search by game name
        except peewee.DoesNotExist:
            await ctx.send('Game with ID {} cannot be found.'.format(game_id))
            return

        game_list = Game.select().where(Game.name.contains(game_name))
        if len(game_list) == 0:
            await ctx.send(f'Cannot locate any games matching name "{game_name}"')
            return
        if len(game_list) == 1:
            await game_embed(ctx, game_list[0])
            return

        embed = discord.Embed(title=f'Found {len(game_list)} matches:')
        for counter, game in enumerate(game_list[:5]):
            embed.add_field(name=f'Game ID {game.id} - {game.home_team.name} vs {game.away_team.name} - {game.name}', value=(str(game.date)), inline=False)
        await ctx.send(embed=embed)

    @commands.command(aliases=['incomplete'])
    async def incompletegames(self, ctx):
        """or incomplete: Lists oldest incomplete games"""
        embed = discord.Embed(title='Oldest incomplete games')

        for counter, game in enumerate(Game.select().where(Game.is_completed == 0).order_by(Game.date)[:20]):
            embed.add_field(name='Game ID #{0.id} - {0.home_team.name} vs {0.away_team.name}'.format(game), value=(str(game.date)), inline=False)

        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_any_role(*mod_roles)
    async def deletegame(self, ctx, game_id: int):
        """deletegame 5 (reverts ELO changes. Use with care.)"""
        with db:
            try:
                game = Game.get(id=game_id)
            except peewee.DoesNotExist:
                await ctx.send('Game with ID {} cannot be found.'.format(game_id))
                return

            await ctx.send('Game with ID {} has been deleted and team/player ELO changes have been reverted, if applicable.'.format(game_id))
            game.delete_game()

    @commands.command(aliases=['teaminfo'])
    async def team(self, ctx, team_string: str):

        matching_teams = get_team_from_name(team_string)
        if len(matching_teams) > 1:
            await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
            return
        if len(matching_teams) == 0:
            await ctx.send('Cannot find a team with name "{}". Be sure to use the full name, surrounded by quotes if it is more than one word.'.format(team_string))
            return
        team = matching_teams[0]

        recent_games = Game.select().where((Game.home_team == team) | (Game.away_team == team)).order_by(-Game.date)[:10]

        # TODO: Add 'most frequent players'
        wins, losses = team.get_record()

        embed = discord.Embed(title='Team card for **{0.name}** {0.emoji}'.format(team))
        embed.add_field(value='\u200b', name='ELO: {}   Wins {} / Losses {}'.format(team.elo, wins, losses))

        if team.image_url:
            embed.set_thumbnail(url=team.image_url)

        for game in recent_games:
            opponent = game.away_team if (game.home_team == team) else game.home_team
            if game.is_completed == 1:
                result = '**WIN**' if (game.winner == team) else 'LOSS'
            else:
                result = 'Incomplete'
            embed.add_field(name='Game {0.id} vs {1.name} {1.emoji} {2}'.format(game, opponent, result), value='{}'.format(str(game.date)), inline=False)

        await ctx.send(embed=embed)

    @commands.command(aliases=['playerinfo'])
    async def player(self, ctx, *args):
        with db:
            if len(args) == 0:
                # Player looking for info on themselves
                player = get_player_from_mention_or_string(f'<@{ctx.author.id}>')
                if len(player) != 1:
                    await ctx.send(f'Could not find you in the database. Try setting your code with {command_prefix}setcode')
                    return
                player = player[0]
            else:
                # Otherwise look for a player matching whatever theyentered
                player_mention = ' '.join(args)
                matching_players = get_player_from_mention_or_string(player_mention)
                if len(matching_players) == 1:
                    player = matching_players[0]
                elif len(matching_players) == 0:
                    # Either no results or more than one. Fall back to searching on polytopia_id or polytopia_name
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

            ranked_players_query = Player.select(Player.id).join(Lineup).join(Game).where(Game.date > date_cutoff).distinct().order_by(-Player.elo).tuples()
            for counter, p in enumerate(ranked_players_query):
                if p[0] == player.id:
                    break
                # counter should now equal ranking of player in the leaderboard

            recent_games = Game.select().join(Lineup).where(Lineup.player == player).order_by(-Game.date)[:5]

            embed = discord.Embed(title=f'Player card for {player.discord_name}')
            embed.add_field(name='Results', value=f'ELO: {player.elo}, W {wins} / L {losses}')
            embed.add_field(name='Ranking', value=f'{counter + 1} of {len(ranked_players_query)}')
            if player.team:
                embed.add_field(name='Last-known Team', value='{}'.format(player.team.name))
                if player.team.image_url:
                    embed.set_thumbnail(url=player.team.image_url)
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
                embed.add_field(name='Game {0.id}   {1.emoji} **{1.name}** *vs* **{2.name}** {2.emoji}'.format(game, game.home_team, game.away_team),
                                value='Status: {} - {}'.format(status, str(game.date)), inline=False)

            await ctx.send(content=content_str, embed=embed)

    @in_bot_channel()
    @commands.command(aliases=['lbteam', 'leaderboardteam'])
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def leaderboard_team(self, ctx):
        """or lbteam : shows team leaderboard"""

        embed = discord.Embed(title='**Team Leaderboard**')
        with db:
            for counter, team in enumerate(Team.select().order_by(-Team.elo).where((Team.name != 'Home') & (Team.name != 'Away'))):
                wins, losses = team.get_record()
                embed.add_field(name='`{1:>3}. {0.name:30}  (ELO: {0.elo:4})  W {2} / L {3}` {0.emoji}'.format(team, counter + 1, wins, losses), value='\u200b', inline=False)
        await ctx.send(embed=embed)

    @in_bot_channel()
    @commands.command(aliases=['leaderboard', 'lbi', 'lb'])
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def leaderboard_individual(self, ctx):

        leaderboard = []
        with db:
            players_with_recent_games = Player.select().join(Lineup).join(Game).where(Game.date > date_cutoff).distinct().order_by(-Player.elo)
            for counter, player in enumerate(players_with_recent_games[:500]):
                wins, losses = player.get_record()
                leaderboard.append('`{1:>3}. {0.discord_name:30}  (ELO: {0.elo:4})  W {2} / L {3}`'.format(player, counter + 1, wins, losses))

        await paginate(self.bot, ctx, title='**Individual Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @in_bot_channel()
    @commands.command(aliases=['lbsquad', 'leaderboardsquad'])
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def leaderboard_squad(self, ctx):
        embed = discord.Embed(title='**Squad Leaderboard**')

        leaderboard = []
        with db:
            squads = Squad.select().join(SquadGame).group_by(Squad.id).having(peewee.fn.COUNT(SquadGame.id) > 1).order_by(-Squad.elo)
            # TODO: Could limit inclusion to date_cutoff although ths might make the board too sparse
            for counter, sq in enumerate(squads[:200]):
                wins, losses = sq.get_record()
                squad_members = sq.get_names()
                squad_names = ' / '.join(squad_members)
                leaderboard.append('`{0:>3}. {1:40}  (ELO: {2:4})  W {3} / L {4}`'.format(counter + 1, squad_names, sq.elo, wins, losses))
        await paginate(self.bot, ctx, title='**Squad Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @commands.command()
    async def setcode(self, ctx, *args):
        if len(args) == 1:
            # User setting code for themselves. No special permissions required.
            target_discord_member = ctx.message.author
            new_id = args[0]
        elif len(args) == 2:
            # User changing another user's code. Admin permissions required.
            # This requires a @Mention target because this is also the command to register a user with the bot, which requires info from discord.Member object
            if len(get_matching_roles(ctx.author, helper_roles)) == 0:
                await ctx.send('You do not have permission to trigger this command.')
                return
            if len(ctx.message.mentions) != 1:
                await ctx.send('Incorrect format. Use `{}setcode @Player newcode`'.format(command_prefix))
                return
            target_discord_member = ctx.message.mentions[0]
            new_id = args[1]
        else:
            # Unexpected input
            await ctx.send('Wrong number of arguments. Use `{}setcode my_polytopia_code`'.format(command_prefix))
            return

        flag, team_list = get_teams_of_players([target_discord_member])
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
            player_matches = get_player_from_mention_or_string(player_string)
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
            target_player = get_player_from_mention_or_string(f'<@{ctx.author.id}>')
            if len(target_player) != 1:
                await ctx.send(f'Player with name {ctx.author.name} is not in the system. Try registering with {command_prefix}setcode first.')
                return
            new_name = args[0]
        elif len(args) == 2:
            # User changing another user's code. Admin permissions required.
            if len(get_matching_roles(self, ctx.author, helper_roles)) == 0:
                await ctx.send('You do not have permission to trigger this command.')
                return

            target_player = get_player_from_mention_or_string(args[0])
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
    async def settribe(self, ctx, game_id, player_name, tribe_name):
        with db:
            try:
                game = Game.get(id=game_id)
            except peewee.DoesNotExist:
                await ctx.send(f'Game with ID {game_id} cannot be found.')
                return
            matching_tribes = Tribe.select().where(Tribe.name.contains(tribe_name))
            if len(matching_tribes) != 1:
                await ctx.send('Matching Tribe not found, or too many found. Check spelling or be more specific.')
                return
            tribe = matching_tribes[0]

            players = get_player_from_mention_or_string(player_name)

            if len(players) == 0:
                await ctx.send('Could not find matching player.')
                return

            if len(players) > 1:
                await ctx.send('More than one player with that name found. Try using @mention.')
                # Could improve this by only searching for players within a game's lineup, but that would be a decent amount of work
                return

            lineups = Lineup.select().join(Player).where((Player.id == players[0]) & (Lineup.game == game))
            if len(lineups) != 1:
                await ctx.send(f'Could not match player {player_name} to game {game_id}.')
                return

            lineups[0].tribe = tribe
            lineups[0].save()
            emoji_str = tribe.emoji if tribe.emoji is not None else ''
            await ctx.send(f'Player {player_name} assigned to tribe {tribe} in game {game_id} {emoji_str}')

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
            await ctx.send(f'Team {name} created! Starting ELO: {team.elo}. Players with a Discord Role exactly matching \"{name}\" will be considered team members.')
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
            matching_teams = get_team_from_name(team_name)
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
            matching_teams = get_team_from_name(team_name)
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

    @commands.command(aliases=['elohelp'])
    async def help(self, ctx):
        commands = [('lb', 'Show individual leaderboard\n`Aliases: leaderboard`'),
                    ('lbteam', 'Show team leaderboard'),
                    ('lbsquad', 'Show squad leaderboard'),
                    ('team `name`', 'Display stats for a given team.\n`Aliases: teaminfo`'),
                    ('player @player', 'Display stats for a given player. Also lets you search by game code/name.\n`Aliases: playerinfo`'),
                    ('game `GAMEID`', 'Display stats for a given game\n`Aliases: gameinfo`'),
                    ('setcode `POLYTOPIACODE`', 'Register your code with the bot for others to find. Also will place you on the leaderboards.'),
                    ('setcode `IN-GAME NAME`', 'Register your in-game name with the bot for others to find.'),
                    ('getcode `PLAYER`', 'Simply return the Polytopia code of anyone registered.'),
                    ('incomplete', 'List oldest games with no declared winner'),
                    ('help_staff', 'Display helper commands, if allowed')]

        embed = discord.Embed(title='**ELO Bot Help**')
        for command, desc in commands:
            embed.add_field(name='{}{}'.format(command_prefix, command), value=desc, inline=False)
        await ctx.send(embed=embed)

    @commands.command(aliases=['help-staff'])
    async def help_staff(self, ctx):
        commands = [('newgame @player1 @player2 VS @player3 @player4', 'Start a new game between listed players.\n`Aliases: startgame`'),
                    ('wingame `GAMEID` \"winning team\"', 'Declare winner of open game.\n`Aliases: win, winner`'),
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


def example_game_data():

    games = []
    games.append(({'Lightning': ['PRA', 'StarLord', 'Cheesy boi'], 'Ronin': ['frodakcin', 'Bucky', 'chadyboy24']}))  # S1-1
    games.append(({'Sparkies': ['portalshooter', 'foreverblue173', 'squelchyman'], 'Cosmonauts': ['Escenilx', 'Unwise', 'Dii']}))  # S1-2
    games.append(({'Jets': ['Yuryavic', 'flc735', 'rickdaheals'], 'Mallards': ['ottermelon', 'ReapersTorment', 'MasterJeremy']}))  # S1-4
    games.append(({'Jets': ['Xaxantes', 'Yuryavic', 'flc735'], 'Home': ['Jerry', 'gelatokid', 'Aaron Denton']}))  # S1-5
    games.append(({'Sparkies': ['Xaxantes', 'Yuryavic', 'flc735'], 'Wildfire': ['MATYTY5', 'Zebastian1', 'Skullcracker']}))  # S1-6
    games.append(({'Cosmonauts': ['OscarTWhale', 'Wedgehead'], 'Ronin': ['frodakcin', 'chadyboy24']}))  # S1-7
    games.append(({'Jets': ['frodakcin', 'skarm323', 'chadyboy24'], 'Ronin': ['rickdaheals', 'Chuest', 'Ganshed']}))  # S1-8
    games.append(({'Mallards': ['Unwise', 'Chuest', 'flc735'], 'Jets': ['MasterJeremy', 'Maap', 'ReapersTorment']}))  # S1-9
    games.append(({'Ronin': ['frodakcin', 'skarm323', 'Nelluk'], 'Jets': ['rickdaheals', 'Xaxantes', 'Ganshed']}))  # S1-10
    games.append(({'Ronin': ['ZetaBravo', 'Nelluk'], 'Jets': ['Unwise', 'Yuryavic']}))  # S1-11
    games.append(({'Lightning': ['Vorce'], 'Cosmonauts': ['anarchoRex']}))  # S1-13
    games.append(({'Mallards': ['ReapersTorment', 'Maap'], 'Sparkies': ['foreverblue173', 'Aengus531']}))  # S1-15
    games.append(({'Cosmonauts': ['anarchoRex', 'Wedgehead'], 'Mallards': ['ILostABet', 'ReapersTorment']}))  # S1-16
    games.append(({'Mallards': ['ReapersTorment', 'Maap'], 'Sparkies': ['Vorce', 'Riym9']}))  # S1-17
    games.append(({'Wildfire': ['Skullcracker'], 'Ronin': ['FreezeHorizon']}))  # S1-18
    games.append(({'Sparkies': ['Bomber'], 'Cosmonauts': ['OscarTWhale']}))  # S1-19

    # # FAKE GAMES BELOW FOR SQUAD TESTING
    # games.append(({'Mallards': ['ReapersTorment', 'Maap'], 'Sparkies': ['Unwise', 'Yuryavic']}))
    # games.append(({'Cosmonauts': ['ZetaBravo', 'Nelluk'], 'Mallards': ['ILostABet', 'ReapersTorment']}))
    # games.append(({'Mallards': ['ReapersTorment', 'Maap'], 'Sparkies': ['Vorce', 'Riym9']}))
    # games.append(({'Jets': ['Yuryavic', 'flc735', 'rickdaheals'], 'Mallards': ['ReapersTorment', 'ottermelon', 'MasterJeremy']}))
    # games.append(({'Jets': ['Xaxantes', 'Yuryavic', 'flc735'], 'Home': ['Jerry', 'gelatokid', 'Aaron Denton']}))

    # Each tuple contains a dict. Each dict has two keys representing names of each side team. Each key value is a [Team,of,players]
    for counter1, g in enumerate(games):
        team1, team2 = list(g.keys())[0], list(g.keys())[1]
        t1 = Team.select().where(Team.name.contains(team1)).get()
        t2 = Team.select().where(Team.name.contains(team2)).get()
        game = Game.create(team_size=len(g[team1]), home_team=t1, away_team=t2)

        team1_players, team2_players = [], []
        for counter, p in enumerate(g[team1]):
            fake_discord_id = hash(p) % 10000
            player, created = Player.get_or_create(discord_name=p, defaults={'discord_id': fake_discord_id, 'team': t1})
            Lineup.create(game=game, player=player, team=t1)
            team1_players.append(player)
        for counter, p in enumerate(g[team2]):
            fake_discord_id = hash(p) % 10000
            player, created = Player.get_or_create(discord_name=p, defaults={'discord_id': fake_discord_id, 'team': t2})
            Lineup.create(game=game, player=player, team=t2)
            team2_players.append(player)

        if len(team1_players) > 1:
            Squad.upsert_squad(player_list=team1_players, game=game, team=t1)
            Squad.upsert_squad(player_list=team2_players, game=game, team=t2)

        game.declare_winner(winning_team=t1, losing_team=t2)


def get_member_from_mention(bot, mention_str):
        # Assumes string of format <@123457890>, returns discord.Member object or None
        # If string is of format <@!12345>, the ! indicates that member has a temporary nickname set on this server.

        try:
            p_id = int(mention_str.strip('<>!@'))
        except ValueError:
            return None
        return bot.guilds[0].get_member(p_id)  # This assumes the bot is only being used on one server!


def get_team_from_name(team_name):
    teams = Team.select().where(Team.name.contains(team_name))
    return teams


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


def get_player_from_mention_or_string(player_string):
    if '<@' in player_string:
        # Extract discord ID and look up based on that
        try:
            p_id = int(player_string.strip('<>!@'))
        except ValueError:
            return []
        try:
            player = Player.select().where(Player.discord_id == p_id)
            return player
        except peewee.DoesNotExist:
            return []

    # Otherwise return any matches from the name string
    return Player.select().where(Player.discord_name.contains(player_string))


def upsert_player_and_lineup(player_discord, player_team, game_side=None, new_game=None):
        player, created = Player.get_or_create(discord_id=player_discord.id, defaults={'discord_name': player_discord.name, 'team': player_team})
        if not created:
            player.team = player_team    # update team with existing player in db in case they have been traded
            player.discord_name = player_discord.name
            player.save()
            logger.debug('Player {player.discord_name} updated')
        if new_game is not None:
            Lineup.create(game=new_game, player=player, team=game_side)
            logger.debug('Player {player.discord_name} inserted')
        return player, created


async def game_embed(ctx, game):

    # TODO: Should team emoji handle being None?

        home_side_team = game.home_team
        away_side_team = game.away_team
        side_home_roster = game.get_roster(home_side_team)
        side_away_roster = game.get_roster(away_side_team)

        embed = discord.Embed(title=f'Game {game.id}: {home_side_team.emoji}  **{home_side_team.name}**   *VS*   **{away_side_team.name}**  {away_side_team.emoji}')
        if game.name:
            embed.title += f'\n*{game.name}*'

        game_status = 'Incomplete'
        if game.is_completed == 1:
            game_status = 'Completed'
            embed.title += f'\nWINNER: {game.winner.name}'

            if game.winner.image_url:
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
        await ctx.send(embed=embed)


async def paginate(bot, ctx, title, message_list, page_start=0, page_end=10, page_size=10):
    # Allows user to page through a long list of messages with reactions

    page_end = page_end if len(message_list) > page_end else len(message_list)

    first_loop = True
    while True:
        embed = discord.Embed(title=title)
        for entry in range(page_start, page_end):
            embed.add_field(name=message_list[entry], value='\u200b', inline=False)

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

        # def check(reaction, user):
        #     e = str(reaction.emoji)
        #     return user == ctx.message.author and e.startswith(('⏪', '⏩'))

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

if args.add_example_games:
    example_game_data()
    exit(0)


def setup(bot):
    bot.add_cog(ELOGamesCog(bot))
