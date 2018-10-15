from discord.ext import commands
from pbwrap import Pastebin
from modules.models import db, Team, Game, Player, DiscordMember, Lineup, Tribe, TribeFlair, Squad, SquadGame
import csv
import json
import peewee
import datetime
import settings
import logging

logger = logging.getLogger('polybot.' + __name__)

try:
    pastebin_api = settings.get_setting('pastebin_key')
except KeyError:
    logger.warn('pastebin_key not found in config.ini - Pastebin functionality will be limited')
    pastebin_api = None


class import_export:
    def __init__(self, bot):
        self.bot = bot

    @commands.is_owner()
    @commands.command(aliases=['dbr'])
    async def db_restore(self, ctx):
        """Owner: Restore database from backed up JSON file.
        File should be named `db_import.json` in same directory as `bot.py`.
        Will fail if there are any games in the existing database as a failsafe.
        """
        if Game.select().count() > 0:
            return await ctx.send('Existing database already has game content. Remove this check if you want to restore on top of existing data.')
        await ctx.send(f'Attempting to restore games from file db_import.json')
        with open('db_import.json') as json_file:
            data = json.load(json_file)
            for team in data['teams']:
                try:
                    guild_id = team['guild_id']
                except KeyError:
                    guild_id = 478571892832206869
                try:
                    with db.atomic():
                        Team.create(name=team['name'], emoji=team['emoji'], image_url=team['image'], guild_id=guild_id)
                except peewee.IntegrityError:
                    logger.warn(f'Cannot add Team {team["name"]} - Already exists')

            for tribe in data['tribes']:
                try:
                    guild_id = tribe['guild_id']
                except KeyError:
                    guild_id = 478571892832206869
                TribeFlair.upsert(name=tribe['name'], guild_id=guild_id, emoji=tribe['emoji'])

            for player in data['players']:
                try:
                    guild_id = player['guild_id']
                except KeyError:
                    guild_id = 478571892832206869

                if player['team'] is not None:
                    try:
                        team = Team.get(name=player['team'], guild_id=guild_id)
                    except peewee.DoesNotExist:
                        logger.warn(f'Cannot add player {player["name"]} to team {player["team"]}')
                        team = None
                else:
                    team = None

                discord_member, _ = DiscordMember.get_or_create(discord_id=player['discord_id'],
                    defaults={'polytopia_id': player['poly_id'],
                              'polytopia_name': player['poly_name'],
                              'name': player['name']})
                player, _ = Player.get_or_create(discord_member=discord_member, guild_id=guild_id,
                    defaults={'name': player['name'],
                              'team': team})

            # for match in data['matches']:
            #     host = Player.get(discord_id=match['host'])
            #     newmatch = Match.create(host=host, notes=match['notes'], team_size=match['team_size'], expiration=match['expiration'])
            #     for mp in match['players']:
            #         match_player = Player.get(discord_id=mp)
            #         MatchPlayer.create(player=match_player, match=newmatch)

            for game in data['games']:
                try:
                    guild_id = game['guild_id']
                except KeyError:
                    guild_id = 478571892832206869

                team1, _ = Team.get_or_create(name=game['team1'][0]['team'], guild_id=guild_id)
                team2, _ = Team.get_or_create(name=game['team2'][0]['team'], guild_id=guild_id)

                newgame = Game.create(id=game['id'],
                                      team_size=len(game['team1']),
                                      guild_id=guild_id,
                                      name=game['name'],
                                      date=game['date'],
                                      announcement_channel=game['announce_chan'],
                                      announcement_message=game['announce_msg'])
                team1_players, team2_players = [], []
                team1_tribes, team2_tribes = [], []

                for p in game['team1']:
                    newplayer, _ = Player.upsert(discord_id=p['player_id'], guild_id=guild_id, discord_name=p['player_name'])

                    tribe_choice = p['tribe']
                    if tribe_choice is not None:
                        tribe = TribeFlair.get_by_name(name=tribe_choice, guild_id=guild_id)
                    else:
                        tribe = None

                    team1_players.append(newplayer)
                    team1_tribes.append(tribe)

                for p in game['team2']:
                    newplayer, _ = Player.upsert(discord_id=p['player_id'], guild_id=guild_id, discord_name=p['player_name'])

                    tribe_choice = p['tribe']
                    if tribe_choice is not None:
                        tribe = TribeFlair.get_by_name(name=tribe_choice, guild_id=guild_id)
                    else:
                        tribe = None

                    team2_players.append(newplayer)
                    team2_tribes.append(tribe)

                # Create/update Squad records
                team1_squad, team2_squad = None, None
                if len(team1_players) > 1:
                    team1_squad = Squad.upsert(player_list=team1_players, guild_id=guild_id)
                if len(team2_players) > 1:
                    team2_squad = Squad.upsert(player_list=team2_players, guild_id=guild_id)

                team1_squadgame = SquadGame.create(game=newgame, squad=team1_squad, team=team1)

                for p, t in zip(team1_players, team1_tribes):
                    Lineup.create(game=newgame, squadgame=team1_squadgame, player=p, tribe=t)

                team2_squadgame = SquadGame.create(game=newgame, squad=team2_squad, team=team2)

                for p, t in zip(team2_players, team2_tribes):
                    Lineup.create(game=newgame, squadgame=team2_squadgame, player=p, tribe=t)

                if game['winner']:
                    full_game = Game.load_full_game(game_id=newgame.id)
                    if team1.name == game['winner']:
                        full_game.declare_winner(winning_side=team1_squadgame, confirm=True)
                    elif team2.name == game['winner']:
                        full_game.declare_winner(winning_side=team2_squadgame, confirm=True)

                    full_game.completed_ts = game['completed_ts']
                    full_game.save()

                print(f'Creating game ID # {newgame.id} - {team1.name} vs {team2.name}')
                logger.debug(f'Creating game ID # {newgame.id} - {team1.name} vs {team2.name}')

        db.execute_sql("SELECT pg_catalog.setval('game_id_seq', (SELECT max(id) FROM game), true );")
        # sets postgres game ID to current max value, otherwise after importing games it would try to create new games at id=1

    @commands.command(aliases=['dbb'])
    # @commands.has_any_role(*mod_roles)
    async def db_backup(self, ctx):
        """Mod: Backs up database of to a new file
        The file will be a JSON file on the bot's hosting server that can be used to restore to a fresh database.
        Comes in handy if ELO math changes and you want to re-run all the games with the new math.
        """

        # Main flaws of backup -
        # Games that involve a deleted player will be skipped (not sure when this would happen)

        await ctx.send(f'Database backup starting. This will take a few seconds.')
        teams_list = []
        for team in Team.select():
            team_obj = {"name": team.name, "emoji": team.emoji, "image": team.image_url}
            teams_list.append(team_obj)

        tribes_list = []
        for tribe in Tribe.select():
            tribe_obj = {"name": tribe.name, "emoji": tribe.emoji}
            tribes_list.append(tribe_obj)

        players_list = []
        for player in Player.select():
            player_obj = {"name": player.discord_name, "discord_id": player.discord_id, "poly_id": player.polytopia_id, "poly_name": player.polytopia_name}
            if player.team:
                player_obj['team'] = player.team.name
            else:
                player_obj['team'] = None
            players_list.append(player_obj)

        match_list = []
        for match in Match.select():
            match_players = [mp.player.discord_id for mp in match.matchplayer]
            match_obj = {"host": match.host.discord_id, "notes": match.notes,
                        "team_size": match.team_size, "expiration": str(match.expiration),
                        "players": match_players}
            match_list.append(match_obj)

        games_list = []
        for game in Game.select().order_by(Game.completed_ts):
            team1 = game.home_team
            team2 = game.away_team
            team1_players, team2_players = [], []
            for lineup in Lineup.select().join(Player).where((Lineup.game == game) & (Lineup.team == team1)):
                lineup_obj = {"player_id": lineup.player.discord_id,
                              "player_name": lineup.player.discord_name,
                              "team": lineup.team.name,
                              "tribe": lineup.tribe.name if lineup.tribe else None}
                # Could add name of tribe choice here
                team1_players.append(lineup_obj)
            for lineup in Lineup.select().join(Player).where((Lineup.game == game) & (Lineup.team == team2)):
                lineup_obj = {"player_id": lineup.player.discord_id,
                              "player_name": lineup.player.discord_name,
                              "team": lineup.team.name,
                              "tribe": lineup.tribe.name if lineup.tribe else None}
                team2_players.append(lineup_obj)
            if len(team1_players) != len(team2_players) or len(team1_players) == 0:
                # TODO: This is to just skip exporting games that have a deleted player on one side. At the moment no graceful way to handle this.
                break

            winner = game.winner.name if game.winner else None
            completed_ts_str = str(game.completed_ts) if game.completed_ts else None
            games_obj = {"id": game.id, "date": str(game.date),
                        "name": game.name, "winner": winner,
                        "team1": team1_players, "team2": team2_players,
                        "announce_chan": game.announcement_channel, "announce_msg": game.announcement_message,
                        "completed_ts": completed_ts_str}
            games_list.append(games_obj)

        data = {"teams": teams_list, "tribes": tribes_list, "players": players_list, "games": games_list, "matches": match_list}
        with open(f'db_export-{datetime.datetime.today().strftime("%Y-%m-%d")}.json', 'w') as outfile:
            json.dump(data, outfile)

        await ctx.send(f'Database has been backed up to file {outfile.name} on my hosting server.')

    @commands.command(aliases=['gex', 'gameexport'])
    # @commands.has_any_role(*helper_roles)
    @commands.cooldown(1, 300, commands.BucketType.guild)
    async def game_export(self, ctx):
        """Staff: Export list of completed games to pastebin
        Will be a CSV file that can be opened as a spreadsheet. Might be useful to somebody who wants to do their own tracking.
        """

        with open('games_export.csv', mode='w') as export_file:
            game_writer = csv.writer(export_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

            header = ['ID', 'Winner', 'Home', 'Away', 'Date', 'Home1', 'Home2', 'Home3', 'Home4', 'Home5', 'Away1', 'Away2', 'Away3', 'Away4', 'Away5']
            game_writer.writerow(header)

            query = Game.select().where(Game.is_completed == 1)
            for q in query:
                row = [q.id, q.winner.name, q.home_team.name, q.away_team.name, str(q.date)]

                pquery = Lineup.select().where(Lineup.game == q.id)
                home_players = []
                away_players = []
                for lineup in pquery:
                    if lineup.team == q.home_team:
                        home_players.append(lineup.player.discord_name)
                    else:
                        away_players.append(lineup.player.discord_name)

                home_players.extend([''] * (5 - len(home_players)))  # Pad list of players with extra blank entries so total length is 5
                away_players.extend([''] * (5 - len(away_players)))
                row += home_players + away_players
                game_writer.writerow(row)

        pb = Pastebin(pastebin_api)
        pb_url = pb.create_paste_from_file(filepath='games_export.csv', api_paste_private=0, api_paste_expire_date='1D', api_paste_name='Polytopia Game Data')
        await ctx.send(f'Game data has been exported to the following URL: {pb_url}')

    @game_export.error
    async def game_export_handler(self, ctx, error):
        """A local Error Handler
        The global on_command_error will still be invoked after."""

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f'This command is on cooldown. Try again in {int(error.retry_after)} seconds.')
            return
        if isinstance(error, commands.CommandInvokeError) or isinstance(error, PermissionError):
            await ctx.send(f'Error creating export file.')
            # If bot is run as a system service the export file will be created by root, which can't be over-written if bot is later run as a user
            # One fix would be to reconfigure system service to run as the user, but that is a bit complicated
            return
        await ctx.send(f'Unknown error')
        logger.warn(f'Unknown error suppressed in game_export command: {error}')
        print(error)
        # This error handler is overly simple and can't raise exceptions that it doesn't specifically handle. No way around it other than
        # writing a full error handler class.


def setup(bot):
    bot.add_cog(import_export(bot))
