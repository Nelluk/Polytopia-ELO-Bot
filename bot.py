import discord
import asyncio
import websockets
import configparser
from discord.ext import commands
from models import *

config = configparser.ConfigParser()
config.read('config.ini')

discord_key = config['DEFAULT']['discord_key']
helper_roles = (config['DEFAULT']['helper_roles']).split(',')
mod_roles = (config['DEFAULT']['mod_roles']).split(',') + helper_roles
command_prefix = config['DEFAULT']['command_prefix']
require_teams = config.getboolean('DEFAULT', 'require_teams')

date_cutoff = datetime.datetime.today() - datetime.timedelta(days=90)  # Players who haven't played since cutoff are not included in leaderboards

bot = commands.Bot(command_prefix=command_prefix)
bot.remove_command('help')


def create_tables():
    with db:
        db.create_tables([Team, Game, Player, Lineup, Tribe, Squad, SquadGame, SquadMember])


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
            team = Team.create(teamname=team, emoji=emoji, image_url=image_url)
        except IntegrityError:
            pass
    for tribe in tribe_list:
        try:
            Tribe.create(name=tribe)
        except IntegrityError:
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
        t1 = Team.select().where(Team.teamname.contains(team1)).get()
        t2 = Team.select().where(Team.teamname.contains(team2)).get()
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


create_tables()
initialize_data()
# example_game_data()


def get_member_from_mention(mention_str):
        # Assumes string of format <@123457890>, returns discord.Member object or None
        # If string is of format <@!12345>, the ! indicates that member has a temporary nickname set on this server.

        try:
            p_id = int(mention_str.strip('<>!@'))
        except ValueError:
            return None
        return bot.guilds[0].get_member(p_id)  # This assumes the bot is only being used on one server!


def get_team_from_name(team_name):
    teams = Team.select().where(Team.teamname.contains(team_name))
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
        list_of_teams = [team.teamname for team in query]               # ['The Ronin', 'The Jets', ...]
        list_of_matching_teams = []
        for player in list_of_players:
            matching_roles = get_matching_roles(player, list_of_teams)
            if len(matching_roles) == 1:
                teamname = next(iter(matching_roles))
                list_of_matching_teams.append(Team.get(Team.teamname == teamname))
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
        except DoesNotExist:
            return []

    # Otherwise return any matches from the name string
    return Player.select().where(Player.discord_name.contains(player_string))


def upsert_player_and_lineup(player_discord, player_team, game_side=None, new_game=None):
        player, created = Player.get_or_create(discord_id=player_discord.id, defaults={'discord_name': player_discord.name, 'team': player_team})
        if not created:
            player.team = player_team    # update team with existing player in db in case they have been traded
            player.discord_name = player_discord.name
            player.save()
        if new_game is not None:
            Lineup.create(game=new_game, player=player, team=game_side)
        return player, created


# DISCORD COMMANDS BELOW


@bot.event
async def on_ready():
    print('We have logged in as {0.user}'.format(bot))


@bot.check
async def globally_block_dms(ctx):
    # Should prevent bot from being able to be controlled via DM
    return ctx.guild is not None


@bot.command(aliases=['endgame', 'win', 'winner'])
@commands.has_any_role(*helper_roles)
async def wingame(ctx, game_id: int, winning_team_name: str):
    """wingame 5 \"The Ronin\""""

    with db:
        try:
            winning_game = Game.get(id=game_id)
        except DoesNotExist:
            await ctx.send('Game with ID {} cannot be found.'.format(game_id))
            return

        if winning_game.is_completed == 1:
            await ctx.send('Game with ID {} is already marked as completed with winning team {}'.format(game_id, winning_game.winner.teamname))
            return

        matching_teams = get_team_from_name(winning_team_name)
        if len(matching_teams) > 1:
            await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
            return
        if len(matching_teams) == 0:
            await ctx.send('Cannot find a team with name "{}". Be sure to use the full name, surrounded by quotes if it is more than one word.'.format(winning_team_name))
            return
        winning_team = matching_teams[0]

        if winning_team.id == winning_game.home_team.id:
            losing_team = winning_game.away_team
        elif winning_team.id == winning_game.away_team.id:
            losing_team = winning_game.home_team
        else:
            await ctx.send('That team did not play in game {0.id}. The teams were {0.home_team.teamname} and {0.away_team.teamname}.'.format(winning_game))
            return

        winning_game.declare_winner(winning_team, losing_team)

    with db:
        embed = discord.Embed(title='Game {gid} has concluded and {winner} is victorious. Congratulations!'.format(gid=game_id, winner=winning_team.teamname))
        if winning_team.image_url:
            embed.set_thumbnail(url=winning_team.image_url)

        embed.add_field(name='**VICTORS**: {0.teamname} ELO: {0.elo} (+{1})'.format(winning_team, winning_game.winner_delta), value='\u200b', inline=False)
        # TODO: Hide ELO and delta if teams are home/away
        winning_players = winning_game.get_roster(winning_team)  # returns [(player, elo_delta), ...]
        losing_players = winning_game.get_roster(losing_team)

        mention_str = 'Game Roster: '
        for winning_player, elo_delta, tribe_emoji in winning_players:
            embed.add_field(name='{0.discord_name} {1}  (ELO: {0.elo})'.format(winning_player, tribe_emoji), value='+{}'.format(elo_delta), inline=True)
            mention_str += '<@{}> '.format(winning_player.discord_id)

        embed.add_field(name='**LOSERS**: {0.teamname} ELO: {0.elo} ({1})'.format(losing_team, winning_game.loser_delta), value='\u200b', inline=False)

        for losing_player, elo_delta, tribe_emoji in losing_players:
            embed.add_field(name='{0.discord_name} {1} (ELO: {0.elo})'.format(losing_player, tribe_emoji), value='{}'.format(elo_delta), inline=True)
            mention_str += '<@{}> '.format(losing_player.discord_id)

    await ctx.send(content=mention_str, embed=embed)


@bot.command(aliases=['newgame'])
@commands.has_any_role(*helper_roles)
async def startgame(ctx, *args):
    """startgame @player1 @player2 VS @player3 @player4"""

    if len(args) not in [3, 5, 7, 9, 11] or args[int(len(args) / 2)].upper() != 'VS':
        await ctx.send('Invalid format. Example usage for a 2v2 game: `{}startgame @player1 @player2 VS @player3 @player4`'.format(command_prefix))
        return

    side_home = args[:int(len(args) / 2)]
    side_away = args[int(len(args) / 2) + 1:]

    if len(side_home) > len(set(side_home)) or len(side_away) > len(set(side_away)):
        await ctx.send('Duplicate players detected. Example usage for a 2v2 game: `{}startgame @player1 @player2 VS @player3 @player4`'.format(command_prefix))
        # Disabling this check would be a decent way to enable uneven teams ie 2v1, with the same person listed twice on one side.
        return

    side_home = [get_member_from_mention(x) for x in side_home]
    side_away = [get_member_from_mention(x) for x in side_away]

    if None in side_home or None in side_away:
        await ctx.send('Command included invalid player. Example usage for a 2v2 game: `{}startgame @player1 @player2 VS @player3 @player4`'.format(command_prefix))
        return

    home_team_flag, list_of_home_teams = get_teams_of_players(side_home)  # List of what server team each player is on, eg Ronin, Jets.
    away_team_flag, list_of_away_teams = get_teams_of_players(side_away)

    if (None in list_of_away_teams) or (None in list_of_home_teams):
        if require_teams is True:
            await(ctx.send('One or more players listed cannot be matched to a Team (based on Discord Roles). Make sure player has exactly one matching Team role.'))
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
                try:
                    home_side_team = Team.get(Team.teamname == 'Home')
                    away_side_team = Team.get(Team.teamname == 'Away')
                except DoesNotExist:
                    await(ctx.send('**ERROR**: Could not find special teams named **Home** and **Away**. These teams must be added with those exact names for bot to work.'))
                    return

    else:
        # Otherwise the players are "intermingling" and the game just influences two hidden teams in the database called 'Home' and 'Away'
        with db:
            try:
                home_side_team = Team.get(Team.teamname == 'Home')
                away_side_team = Team.get(Team.teamname == 'Away')
            except DoesNotExist:
                await(ctx.send('**ERROR**: Could not find special teams named **Home** and **Away**. These teams must be added with those exact names for bot to work.'))
                return

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
            home_elo_str = 'Squad ELO: {}'.format(home_squad.elo)
            away_elo_str = 'Squad ELO: {}'.format(away_squad.elo)
        else:
            home_elo_str = 'Player ELO: {}'.format(side_home_players[0].elo)
            away_elo_str = 'Player ELO: {}'.format(side_away_players[0].elo)

    embed = discord.Embed(title='Game {0}: {1.emoji}  **{1.teamname}**   *VS*   **{2.teamname}**  {2.emoji}'.format(newgame.id, home_side_team, away_side_team))
    embed.add_field(name='Lineup for Team *{}*'.format(home_side_team.teamname), value=home_elo_str, inline=False)
    mention_str = 'Game Roster: '

    for player in side_home_players:
        embed.add_field(name='**{0.discord_name}**'.format(player), value='\u200b')
        mention_str += '<@{}> '.format(player.discord_id)

    embed.add_field(value='\u200b', name=' \u200b', inline=False)

    embed.add_field(name='Lineup for Team *{}*'.format(away_side_team.teamname), value=away_elo_str, inline=False)

    for player in side_away_players:
        embed.add_field(name='**{0.discord_name}**'.format(player), value='\u200b')
        mention_str += '<@{}> '.format(player.discord_id)

    await ctx.send(content=mention_str, embed=embed)


@bot.command(aliases=['gameinfo'])
async def game(ctx, game_id: int):
    with db:
        try:
            game = Game.get(id=game_id)
        except DoesNotExist:
            await ctx.send('Game with ID {} cannot be found.'.format(game_id))
            return

        home_side_team = game.home_team
        away_side_team = game.away_team
        side_home = game.get_roster(home_side_team)
        side_away = game.get_roster(away_side_team)

    if game.is_completed == 1:
        game_status = 'Completed'
        embed = discord.Embed(title='Game {0}: {1.emoji}  **{1.teamname}**   *VS*   **{2.teamname}**  {2.emoji}\nWINNER: {3}'.format(game.id, home_side_team, away_side_team, game.winner.teamname))
        if game.winner.image_url:
            embed.set_thumbnail(url=game.winner.image_url)

    else:
        game_status = 'Incomplete'
        embed = discord.Embed(title='Game {0}: {1.emoji}  **{1.teamname}**   *VS*   **{2.teamname}**  {2.emoji}'.format(game.id, home_side_team, away_side_team))

    embed.add_field(name='Lineup for Team **{0.teamname}**({0.elo})'.format(home_side_team), value='\u200b', inline=False)

    for player, elo_delta, tribe_emoji in side_home:
        embed.add_field(name='**{0.discord_name}** {1}'.format(player, tribe_emoji), value='ELO: {0.elo}'.format(player), inline=True)

    embed.add_field(value='\u200b', name=' \u200b', inline=False)
    embed.add_field(name='Lineup for Team **{0.teamname}**({0.elo})'.format(away_side_team), value='\u200b', inline=False)

    for player, elo_delta, tribe_emoji in side_away:
        embed.add_field(name='**{0.discord_name}** {1}'.format(player, tribe_emoji), value='ELO: {0.elo}'.format(player), inline=True)

    embed.set_footer(text='Status: {}  -  Creation Date {}'.format(game_status, str(game.timestamp).split(' ')[0]))
    await ctx.send(embed=embed)


@bot.command(aliases=['incomplete'])
async def incompletegames(ctx):
    """or incomplete: Lists oldest incomplete games"""
    embed = discord.Embed(title='Oldest incomplete games')

    for counter, game in enumerate(Game.select().where(Game.is_completed == 0).order_by(Game.timestamp)[:20]):
        embed.add_field(name='Game ID #{0.id} - {0.home_team.teamname} vs {0.away_team.teamname}'.format(game), value=(str(game.timestamp).split(' ')[0]), inline=False)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_any_role(*mod_roles)
async def deletegame(ctx, game_id: int):
    """deletegame 5 (reverts ELO changes. Use with care.)"""
    with db:
        try:
            game = Game.get(id=game_id)
        except DoesNotExist:
            await ctx.send('Game with ID {} cannot be found.'.format(game_id))
            return

        await ctx.send('Game with ID {} has been deleted and team/player ELO changes have been reverted, if applicable.'.format(game_id))
        game.delete_game()


@bot.command(aliases=['teaminfo'])
async def team(ctx, team_string: str):

    matching_teams = get_team_from_name(team_string)
    if len(matching_teams) > 1:
        await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
        return
    if len(matching_teams) == 0:
        await ctx.send('Cannot find a team with name "{}". Be sure to use the full name, surrounded by quotes if it is more than one word.'.format(team_string))
        return
    team = matching_teams[0]

    recent_games = Game.select().where((Game.home_team == team) | (Game.away_team == team)).order_by(-Game.timestamp)[:10]

    # TODO: Add ranking within individual leaderboard, start from https://stackoverflow.com/a/907458

    # TODO: Add 'most frequent players'
    wins, losses = team.get_record()

    embed = discord.Embed(title='Team card for **{0.teamname}** {0.emoji}'.format(team))
    embed.add_field(value='\u200b', name='ELO: {}   Wins {} / Losses {}'.format(team.elo, wins, losses))

    if team.image_url:
        embed.set_thumbnail(url=team.image_url)

    for game in recent_games:
        opponent = game.away_team if (game.home_team == team) else game.home_team
        if game.is_completed == 1:
            result = '**WIN**' if (game.winner == team) else 'LOSS'
        else:
            result = 'Incomplete'
        embed.add_field(name='Game {0.id} vs {1.teamname} {1.emoji} {2}'.format(game, opponent, result), value='{}'.format(str(game.timestamp).split(' ')[0]), inline=False)

    await ctx.send(embed=embed)


@bot.command(aliases=['playerinfo'])
async def player(ctx, player_mention: str):

    with db:
        matching_players = get_player_from_mention_or_string(player_mention)
        if len(matching_players) == 1:
            player = matching_players[0]
        else:
            # Either no results or more than one. Fall back to searching on polytopia_id or polytopia_name
            try:
                player = Player.select().where((Player.polytopia_id == player_mention) | (Player.polytopia_name == player_mention)).get()
            except DoesNotExist:
                await ctx.send('Could not find \"{}\" by Discord name, Polytopia name, or Polytopia ID.'.format(player_mention))
                return

        # TODO: Add ranking within individual leaderboard, start from https://stackoverflow.com/a/907458

        wins, losses = player.get_record()

        ranked_players_query = Player.select(Player.id).join(Lineup).join(Game).where(Game.timestamp > date_cutoff).distinct().order_by(-Player.elo).tuples()
        for counter, p in enumerate(ranked_players_query):
            if p[0] == player.id:
                break
            # counter should now equal ranking of player in the leaderboard

        recent_games = Game.select().join(Lineup).where(Lineup.player == player).order_by(-Game.timestamp)[:5]

        embed = discord.Embed(title='Player card for {}'.format(player.discord_name))
        embed.add_field(name='Results', value='ELO: {}, W {} / L {}'.format(player.elo, wins, losses))
        embed.add_field(name='Ranking', value='{} of {}'.format(counter + 1, len(ranked_players_query)))
        if player.team:
            embed.add_field(name='Last-known Team', value='{}'.format(player.team.teamname))
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
            embed.add_field(name='Game {0.id}   {1.emoji} **{1.teamname}** *vs* **{2.teamname}** {2.emoji}'.format(game, game.home_team, game.away_team),
                            value='Status: {} - {}'.format(status, str(game.timestamp).split(' ')[0]), inline=False)

        await ctx.send(content=content_str, embed=embed)


@bot.command(aliases=['lbteam', 'leaderboardteam'])
async def leaderboard_team(ctx):
    """or lbteam : shows team leaderboard"""

    embed = discord.Embed(title='**Team Leaderboard**')
    with db:
        for counter, team in enumerate(Team.select().order_by(-Team.elo).where((Team.teamname != 'Home') & (Team.teamname != 'Away'))):
            wins, losses = team.get_record()
            embed.add_field(name='`{1:>3}. {0.teamname:30}  (ELO: {0.elo:4})  W {2} / L {3}` {0.emoji}'.format(team, counter + 1, wins, losses), value='\u200b', inline=False)
    await ctx.send(embed=embed)


@bot.command(aliases=['leaderboard', 'lbi', 'lb'])
async def leaderboard_individual(ctx):

    embed = discord.Embed(title='**Individual Leaderboard**')
    with db:
        players_with_recent_games = Player.select().join(Lineup).join(Game).where(Game.timestamp > date_cutoff).distinct().order_by(-Player.elo)
        # all_players = Player.select().order_by(-Player.elo)
        for counter, player in enumerate(players_with_recent_games[:20]):
            wins, losses = player.get_record()
            embed.add_field(name='`{1:>3}. {0.discord_name:30}  (ELO: {0.elo:4})  W {2} / L {3}`'.format(player, counter + 1, wins, losses), value='\u200b', inline=False)
    await ctx.send(embed=embed)


@bot.command(aliases=['lbsquad', 'leaderboardsquad'])
async def leaderboard_squad(ctx):
    embed = discord.Embed(title='**Squad Leaderboard**')

    with db:
        squads = Squad.select().join(SquadGame).group_by(Squad.id).having(fn.COUNT(SquadGame.id) > 2).order_by(-Squad.elo)
        # TODO: Could limit inclusion to date_cutoff although ths might make the board too sparse
        for counter, sq in enumerate(squads)[:20]:
            wins, losses = sq.get_record()
            squad_members = sq.get_names()
            squad_names = ' / '.join(squad_members)
            embed.add_field(name='`{0:>3}. {1:40}  (ELO: {2:4})  W {3} / L {4}`'.format(counter + 1, squad_names, sq.elo, wins, losses), value='\u200b', inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def setcode(ctx, *args):
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


@bot.command(aliases=['code'])
async def getcode(ctx, player_string: str):

    with db:
        player_matches = get_player_from_mention_or_string(player_string)
        if len(player_matches) != 1:
            await ctx.send('Cannot find player with that name. Correct usage: `{}getcode @Player`'.format(command_prefix))
            return
        player_target = player_matches[0]
        if player_target.polytopia_id:
            await ctx.send(player_target.polytopia_id)
        else:
            await ctx.send('User was found but does not have a Polytopia ID on file.')


@bot.command()
async def setname(ctx, *args):
    if len(args) == 1:
        # User setting code for themselves. No special permissions required.
        target_player = get_player_from_mention_or_string(ctx.author.name)
        if len(target_player) != 1:
            await ctx.send('Player with name {} is not in the system. Try registering with {}setcode first.'.format(ctx.author.name, command_prefix))
            return
        new_name = args[0]
    elif len(args) == 2:
        # User changing another user's code. Admin permissions required.
        if len(get_matching_roles(ctx.author, helper_roles)) == 0:
            await ctx.send('You do not have permission to trigger this command.')
            return

        target_player = get_player_from_mention_or_string(args[0])
        if len(target_player) != 1:
            await ctx.send('Player with name {} is not in the system. Try registering with {}setcode first.'.format(ctx.author.name, command_prefix))
            return
        new_name = args[1]
    else:
        # Unexpected input
        await ctx.send('Wrong number of arguments. Use `{}setname my_polytopia_name`'.format(command_prefix))
        return

    with db:
        target_player[0].polytopia_name = new_name
        target_player[0].save()
        await ctx.send('Player {0.discord_name} updated in system with Polytopia name {0.polytopia_name}.'.format(target_player[0]))


@bot.command(aliases=['set_tribe', 'tribe'])
async def settribe(ctx, game_id, player_name, tribe_name):
    with db:
        try:
            game = Game.get(id=game_id)
        except DoesNotExist:
            await ctx.send('Game with ID {} cannot be found.'.format(game_id))
            return
        matching_tribes = Tribe.select().where(Tribe.name.contains(tribe_name))
        if len(matching_tribes) != 1:
            await ctx.send('Matching Tribe not found, or too many found. Check spelling or be more specific.')
            return
        tribe = matching_tribes[0]

        players = get_player_from_mention_or_string(player_name)

        if len(players) != 1:
            await ctx.send('Could not find matching player.'.format(player_name, game_id))
            return

        lineups = Lineup.select().join(Player).where((Player.id == players[0]) & (Lineup.game == game))
        if len(lineups) != 1:
            await ctx.send('Could not match player {} to game {}.'.format(player_name, game_id))
            return

        lineups[0].tribe = tribe
        lineups[0].save()
        await ctx.send('Player {} assigned to tribe {} in game {} {}'.format(player_name, tribe.name, game_id, tribe.emoji))


@bot.command()
@commands.has_any_role(*mod_roles)
async def tribe_emoji(ctx, tribe_name: str, emoji):

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


@bot.command(aliases=['addteam'])
@commands.has_any_role(*mod_roles)
async def team_add(ctx, name: str):
    # Team name is expected to match the name of a discord Role, so bot can automatically tell what team a player is in
    try:
        db.connect()
        team = Team.create(teamname=name)
        await ctx.send('Team {name} created! Starting ELO: {elo}. Players with a Discord Role exactly matching \"{name}\" will be considered team members.'.format(name=name, elo=team.elo))
    except IntegrityError:
        await ctx.send('That team already exists!')
    db.close()


@bot.command()
@commands.has_any_role(*mod_roles)
async def team_emoji(ctx, team_name: str, emoji):

    if len(emoji) != 1 and ('<:' not in emoji):
        await ctx.send('Valid emoji not detected. Example: `{}team_emoji Teamname :my_custom_emoji:`'.format(command_prefix))
        return

    with db:
        matching_teams = get_team_from_name(team_name)
        if len(matching_teams) != 1:
            await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_emoji Teamname :my_custom_emoji:`'.format(command_prefix))
            return

        team = matching_teams[0]
        team.emoji = emoji
        team.save()

        await ctx.send('Team {0.teamname} updated with new emoji: {0.emoji}'.format(team))


@bot.command()
@commands.has_any_role(*mod_roles)
async def team_image(ctx, team_name: str, image_url):

    if 'http'not in image_url:
        await ctx.send('Valid image url not detected. Example usage: `{}team_image Teamname http://url_to_image.png`'.format(command_prefix))
        # This is a very dumb check to make sure user is passing a URL and not a random string. Assumes mod can figure it out from there.
        return

    with db:
        matching_teams = get_team_from_name(team_name)
        if len(matching_teams) != 1:
            await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_image Teamname http://url_to_image.png`'.format(command_prefix))
            return

        team = matching_teams[0]
        team.image_url = image_url
        team.save()

        await ctx.send('Team {0.teamname} updated with new image_url (image should appear below)'.format(team))
        await ctx.send(team.image_url)


@bot.command()
@commands.has_any_role(*mod_roles)
async def team_name(ctx, old_team_name: str, new_team_name: str):

    with db:
        try:
            team = Team.get(teamname=old_team_name)
        except DoesNotExist:
            await ctx.send('That team can not be found. Be sure to use the full team name. Example: `{}team_name \"Current Teamname\" \"New Team Name\"`'.format(command_prefix))
            return

        team.teamname = new_team_name
        team.save()

        await ctx.send('Team **{}** has been renamed to **{}**.'.format(old_team_name, new_team_name))


@bot.command(aliases=['elohelp'])
async def help(ctx):
    commands = [('leaderboard', 'Show individual leaderboard\n`Aliases: lb, leaderboard_individual`'),
                ('leaderboard_team', 'Show team leaderboard\n`Aliases: lbteam, leaderboardteam`'),
                ('team `TEAMNAME`', 'Display stats for a given team.\n`Aliases: teaminfo`'),
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


@bot.command(aliases=['help-staff'])
async def help_staff(ctx):
    commands = [('newgame @player1 @player2 VS @player3 @player4', 'Start a new game between listed players.\n`Aliases: startgame`'),
                ('wingame `GAMEID` \"winning team\"', 'Declare winner of open game.\n`Aliases: win, winner`'),
                ('setcode `@user POLYTOPIACODE`', 'Change or add the code of another user to the bot.'),
                ('setname `PLAYER IN-GAME-NAME`', 'Change or add the in-game name of another user to the bot.')]

    mod_commands = [('deletegame `GAMEID`', 'Delete game and roll back relevant ELO changes'),
                    ('team_add \"Team Name\"', 'Add team to bot. Be sure to use full name - must have a matching **Discord role** of identical name.'),
                    ('team_emoji `TEAMNAME :emoji-code:`', 'Set an emoji to be associated with a team.'),
                    ('team_image `TEAMNAME http://image-url.png`', 'Set an image to be associated with a team.'),
                    ('tribe_emoji `TRIBENAME :emoji-code:`', 'Set an emoji to be associated with a Polytopia tribe.'),
                    ('team_name \"current team name\" \"New Team Name\"', 'Change a team name.')]

    embed = discord.Embed(title='**ELO Bot Help - Staff Commands**')
    for command, desc in commands:
        embed.add_field(name='{}{}'.format(command_prefix, command), value=desc, inline=False)
    embed.add_field(name='*Mod-only commands below*', value='\u200b', inline=False)
    for command, desc in mod_commands:
        embed.add_field(name='{}{}'.format(command_prefix, command), value=desc, inline=False)
    await ctx.send(embed=embed)


bot.run(discord_key)
