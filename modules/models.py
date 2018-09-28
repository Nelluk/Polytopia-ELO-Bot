import datetime
from peewee import *
import modules.utilities as utilities

db = PostgresqlDatabase('polytopia', user='cbsteven')


class BaseModel(Model):
    class Meta:
        database = db


class Team(BaseModel):
    name = TextField(unique=False, null=False)       # can't store in case insensitive way, need to use ILIKE operator
    elo = SmallIntegerField(default=1000)
    emoji = TextField(null=False, default='')       # Changed default from nullable/None
    image_url = TextField(null=True)
    guild_id = BitField(unique=False, null=False)   # Included for possible future expanson

    class Meta:
        indexes = ((('name', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes


class DiscordMember(BaseModel):
    discord_id = BitField(unique=True, null=False)
    name = TextField(unique=False)
    elo = SmallIntegerField(default=1000)
    polytopia_id = TextField(null=True)
    polytopia_name = TextField(null=True)


class Player(BaseModel):
    discord_member = ForeignKeyField(DiscordMember, unique=False, null=False, backref='guildmember', on_delete='CASCADE')
    guild_id = BitField(unique=False, null=False)
    nick = TextField(unique=False, null=True)
    team = ForeignKeyField(Team, null=True, backref='player')
    elo = SmallIntegerField(default=1000)
    # Add discord name here too so searches can hit just one table?
    # Add array of achievements?

    def upsert(discord_member_obj, guild_id, team=None):

        discord_member, _ = DiscordMember.get_or_create(discord_id=discord_member_obj.id, defaults={'name': discord_member_obj.name})

        # http://docs.peewee-orm.com/en/latest/peewee/querying.html#upsert
        player = Player.insert(discord_member=discord_member, guild_id=guild_id, nick=discord_member_obj.nick, team=team).on_conflict(
            conflict_target=[Player.discord_member, Player.guild_id],  # update if exists
            preserve=[Player.team, Player.nick]  # refresh team/nick with new value
        ).execute()

        return player

    class Meta:
        indexes = ((('discord_member', 'guild_id'), True),)   # Trailing comma is required


class Tribe(BaseModel):
    name = TextField(unique=False, null=False)
    guild_id = BitField(unique=False, null=False)
    emoji = TextField(null=False, default='')

    class Meta:
        indexes = ((('name', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes


class Game(BaseModel):
    name = TextField(null=True)
    winner_delta = IntegerField(default=0)
    loser_delta = IntegerField(default=0)
    is_completed = BooleanField(default=False)
    is_confirmed = BooleanField(default=False)  # Use to confirm losses and filter searches?
    announcement_message = BitField(default=None, null=True)
    announcement_channel = BitField(default=None, null=True)
    date = DateField(default=datetime.datetime.today)
    completed_ts = DateTimeField(null=True, default=None)
    name = TextField(null=True)

    def create_game(teams, guild_id, name=None, require_teams=False):

        # Determine what Team guild members are associated with
        home_team_flag, list_of_home_teams = utilities.get_teams_of_players(guild_id=guild_id, list_of_players=teams[0])  # get list of what server team each player is on, eg Ronin, Jets.
        away_team_flag, list_of_away_teams = utilities.get_teams_of_players(guild_id=guild_id, list_of_players=teams[1])

        if (None in list_of_away_teams) or (None in list_of_home_teams):
            if require_teams is True:
                raise utilities.CheckFailedError('One or more players listed cannot be matched to a Team (based on Discord Roles). Make sure player has exactly one matching Team role.')
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
                    home_side_team, _ = Team.get_or_create(name='Home', guild_id=guild_id, defaults={'emoji': ':stadium:'})
                    away_side_team, _ = Team.get_or_create(name='Away', guild_id=guild_id, defaults={'emoji': ':airplane:'})

        else:
            # Otherwise the players are "intermingling" and the game just influences two hidden teams in the database called 'Home' and 'Away'
            with db:
                home_side_team, _ = Team.get_or_create(name='Home', guild_id=guild_id, defaults={'emoji': ':stadium:'})
                away_side_team, _ = Team.get_or_create(name='Away', guild_id=guild_id, defaults={'emoji': ':airplane:'})

        with db:
            newgame = Game.create(name=name)

            side_home_players = []
            side_away_players = []
            # Create/update Player records
            for player_discord, player_team in zip(teams[0], list_of_home_teams):
                side_home_players.append(Player.upsert(player_discord, guild_id=guild_id, team=player_team))

            for player_discord, player_team in zip(teams[1], list_of_away_teams):
                side_away_players.append(Player.upsert(player_discord, guild_id=guild_id, team=player_team))

            # Create/update Squad records
            home_squad = Squad.upsert(player_list=side_home_players)
            away_squad = Squad.upsert(player_list=side_away_players)

            home_squadgame = SquadGame.create(game=newgame, squad=home_squad, team=home_side_team)

            for squadmember in home_squad.squadmembers:
                SquadMemberGame.create(member=squadmember, squadgame=home_squadgame)

            away_squadgame = SquadGame.create(game=newgame, squad=away_squad, team=away_side_team)

            for squadmember in away_squad.squadmembers:
                SquadMemberGame.create(member=squadmember, squadgame=away_squadgame)

        return newgame, home_squadgame, away_squadgame


class Squad(BaseModel):
    elo = SmallIntegerField(default=1000)

    def get_matching_squad(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns squad with exactly the same participating players. See https://stackoverflow.com/q/52010522/1281743
        query = Squad.select().join(SquadMember).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list)) & (fn.SUM(SquadMember.player.not_in(player_list).cast('integer')) == 0)
        )

        return query

    def upsert(player_list):
        # TODO: could re-write to be a legit upsert as in Player.upsert
        squads = Squad.get_matching_squad(player_list)

        if len(squads) == 0:
            # Insert new squad based on this combination of players
            sq = Squad.create()
            for p in player_list:
                SquadMember.create(player=p, squad=sq)
            return sq

        return squads[0]

    # def upsert_squad(player_list, game, team):
    #     # TODO: could re-write to be a legit upsert as in Player.upsert
    #     squads = Squad.get_matching_squad(player_list)

    #     if len(squads) == 0:
    #         # Insert new squad based on this combination of players
    #         sq = Squad.create()
    #         for p in player_list:
    #             SquadMember.create(player=p, squad=sq)
    #         squadgame = SquadGame.create(game=game, squad=sq, team=team)
    #         # return sq
    #     else:
    #         # Update existing squad with new game
    #         squadgame = SquadGame.create(game=game, squad=squads[0], team=team)
    #         sq = squads[0]

    #     for squadmember in sq.squadmembers:
    #         SquadMemberGame.create(member=squadmember, squadgame=squadgame)


class SquadMember(BaseModel):
    player = ForeignKeyField(Player, null=False, on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadmembers', on_delete='CASCADE')


class SquadGame(BaseModel):
    game = ForeignKeyField(Game, null=False, backref='squad', on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadgame', on_delete='CASCADE')
    team = ForeignKeyField(Team, null=True)
    elo_change = SmallIntegerField(default=0)
    is_winner = BooleanField(default=False)
    team_chan_category = BitField(default=None, null=True)
    team_chan = BitField(default=None, null=True)   # Store category/ID of team channel for more consistent renaming-deletion


class SquadMemberGame(BaseModel):
    member = ForeignKeyField(SquadMember, null=False, backref='membergame', on_delete='CASCADE')
    squadgame = ForeignKeyField(SquadGame, null=False, on_delete='CASCADE')
    tribe = ForeignKeyField(Tribe, null=True)
    elo_change = SmallIntegerField(default=0)


with db:
    db.create_tables([Team, DiscordMember, Game, Player, Tribe, Squad, SquadGame, SquadMember, SquadMemberGame])
    # Only creates missing tables so should be safe to run each time
