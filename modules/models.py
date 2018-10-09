import datetime
import discord
from peewee import *
from playhouse.postgres_ext import *
import modules.exceptions as exceptions
from modules import utilities
import logging

logger = logging.getLogger('polybot.' + __name__)

db = PostgresqlDatabase('polytopia2', user='cbsteven')


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

    def get_by_name(team_name: str, guild_id: int):
        teams = Team.select().where((Team.name.contains(team_name)) & (Team.guild_id == guild_id))
        return teams

    def completed_game_count(self):

        num_games = SquadGame.select().join(Game).join_from(SquadGame, Team).where(
            (SquadGame.team == self) & (SquadGame.game.is_completed == 'TRUE')
        ).count()
        print(f'team: {self.id} completed-game-count: {num_games}')

        return num_games

    def change_elo_after_game(self, opponent_elo, is_winner):

        if self.completed_game_count() < 11:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - self.elo) / 400.0))), 3)

        if is_winner is True:
            new_elo = round(self.elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(self.elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - self.elo)
        print('Team chance of winning: {} opponent elo {} current ELO {}, new elo {}, elo_delta {}'.format(chance_of_winning, opponent_elo, self.elo, new_elo, elo_delta))

        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta

    def team_games_subq():
        # Subquery of all games with more than 2 players
        return SquadMemberGame.select(SquadMemberGame.squadgame.game).join(SquadGame).group_by(
            SquadMemberGame.squadgame.game
        ).having(fn.COUNT('*') > 2)

    def get_record(self):

        wins = Game.select(Game, SquadGame).join(SquadGame).where(
            (Game.id.in_(Team.team_games_subq())) & (Game.is_completed == 1) & (SquadGame.team == self) & (SquadGame.is_winner == 1)
        ).count()

        losses = Game.select(Game, SquadGame).join(SquadGame).where(
            (Game.id.in_(Team.team_games_subq())) & (Game.is_completed == 1) & (SquadGame.team == self) & (SquadGame.is_winner == 0)
        ).count()

        return (wins, losses)


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
    name = TextField(unique=False, null=True)
    team = ForeignKeyField(Team, null=True, backref='player')
    elo = SmallIntegerField(default=1000)
    trophies = ArrayField(CharField, null=True)
    # Add discord name here too so searches can hit just one table?

    def generate_display_name(self=None, player_name=None, player_nick=None):
        if player_nick:
            if player_name in player_nick:
                display_name = player_nick
            else:
                display_name = f'{player_name} ({player_nick})'
        else:
            display_name = player_name

        if self:
            self.name = display_name
            self.save()
        return display_name

    def upsert(discord_member_obj, guild_id, team=None):
        # Stopped using postgres upsert on_conflict() because it only returns row ID so its annoying to use
        display_name = Player.generate_display_name(player_name=discord_member_obj.name, player_nick=discord_member_obj.nick)

        try:
            with db.atomic():
                discord_member = DiscordMember.create(discord_id=discord_member_obj.id, name=discord_member_obj.name)
        except IntegrityError:
            discord_member = DiscordMember.get(discord_id=discord_member_obj.id)
            discord_member.name = discord_member_obj.name
            discord_member.save()

        try:
            with db.atomic():
                player = Player.create(discord_member=discord_member, guild_id=guild_id, nick=discord_member_obj.nick, name=display_name, team=team)
            created = True
        except IntegrityError:
            created = False
            player = Player.get(discord_member=discord_member, guild_id=guild_id)
            player.nick = discord_member_obj.nick
            player.name = display_name
            player.team = team
            player.save()

        return player, created

    def get_teams_of_players(guild_id, list_of_players):
        # TODO: make function async? Tried but got invalid syntax complaint in linter in the calling function

        # given [List, Of, discord.Member, Objects] - return a, b
        # a = binary flag if all members are on the same Poly team. b = [list] of the Team objects from table the players are on
        # input: [Nelluk, Frodakcin]
        # output: True, [<Ronin>, <Ronin>]

        with db:
            query = Team.select(Team.name).where(Team.guild_id == guild_id)
            list_of_teams = [team.name for team in query]               # ['The Ronin', 'The Jets', ...]
            list_of_matching_teams = []
            for player in list_of_players:
                matching_roles = utilities.get_matching_roles(player, list_of_teams)
                if len(matching_roles) == 1:
                    # TODO: This would be more efficient to do as one query and then looping over the list of teams one time for each player
                    name = next(iter(matching_roles))
                    list_of_matching_teams.append(
                        Team.select().where(
                            (Team.name == name) & (Team.guild_id == guild_id)
                        ).get()
                    )
                else:
                    list_of_matching_teams.append(None)
                    # Would be here if no player Roles match any known teams, -or- if they have more than one match

            same_team_flag = True if all(x == list_of_matching_teams[0] for x in list_of_matching_teams) else False
            return same_team_flag, list_of_matching_teams

    def get_by_string(player_string: str, guild_id: int):
        # Returns QuerySet containing players in current guild matching string. Searches against discord mention ID first, then exact discord name match,
        # then falls back to substring match on name/nick, then a lastly a substring match of polytopia ID or polytopia in-game name

        try:
            p_id = int(player_string.strip('<>!@'))
            # lookup either on <@####> mention string or raw ID #
            return Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.discord_id == p_id) & (Player.guild_id == guild_id)
            )
        except ValueError:
            if len(player_string.split('#', 1)[0]) > 2:
                discord_str = player_string.split('#', 1)[0]
                # If query is something like 'Nelluk#7034', use just the 'Nelluk' to match against discord_name.
                # This happens if user does an @Mention then removes the @ character
            else:
                discord_str = player_str

            name_exact_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.name == discord_str) & (Player.guild_id == guild_id)
            )
            if len(name_exact_match) == 1:
                # String matches DiscordUser.name exactly
                return name_exact_match

            # If no exact match, return any substring matches
            name_substring_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                ((Player.nick.contains(player_string)) | (DiscordMember.name.contains(discord_str))) & (Player.guild_id == guild_id)
            )

            if len(name_substring_match) > 0:
                return name_substring_match

            # If no substring name matches, return anything with matching polytopia name or code
            poly_fields_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                ((DiscordMember.polytopia_id.contains(player_string)) | (DiscordMember.polytopia_name.contains(player_string))) & (Player.guild_id == guild_id)
            )
            return poly_fields_match

    def completed_game_count(self):

        num_games = Lineup.select().join(Game).where(
            (Lineup.game.is_completed == 'TRUE') & (Lineup.player == self)
        ).count()

        return num_games

    def wins(self):
        # TODO: Could combine wins/losses into one function that takes an argument and modifies query

        q = Lineup.select().join(Game).where(
            (Lineup.game.is_completed == 'TRUE') & (Lineup.player == self) & (Lineup.lineup_num == Lineup.game.winning_lineup)
        )

        return q

    def losses(self):
        q = Lineup.select().join(Game).where(
            (Lineup.game.is_completed == 'TRUE') & (Lineup.player == self) & (Lineup.lineup_num != Lineup.game.winning_lineup)
        )

        return q

    def get_record(self):

        return (self.wins().count(), self.losses().count())

    # def leaderboard_rank(self, date_cutoff):
    #     # TODO: This could be replaced with Postgresql Window functions to have the DB calculate the rank.
    #     # Advantages: Probably moderately more efficient, and will resolve ties in a sensible way
    #     # But no idea how to write the query :/

    #     query = Player.leaderboard(date_cutoff=date_cutoff, guild_id=self.guild_id)

    #     player_found = False
    #     for counter, p in enumerate(query.tuples()):
    #         print(p)
    #         if p[0] == self.id:
    #             player_found = True
    #             break

    #     rank = counter + 1 if player_found else None
    #     return (rank, query.count())

    # def leaderboard(date_cutoff, guild_id: int):
    #     query = Player.select().join(SquadMember).join(SquadMemberGame).join_from(SquadMemberGame, SquadGame).join(Game).where(
    #         (Player.guild_id == guild_id) & (Game.is_completed == 1) & (Game.date > date_cutoff)
    #     ).distinct().order_by(-Player.elo)

    #     if query.count() < 10:
    #         # Include all registered players on leaderboard if not many games played
    #         query = Player.select().where(Player.guild_id == guild_id).order_by(-Player.elo)

    #     return query

    class Meta:
        indexes = ((('discord_member', 'guild_id'), True),)   # Trailing comma is required


class Tribe(BaseModel):
    name = TextField(unique=True, null=False)


class TribeFlair(BaseModel):
    tribe = ForeignKeyField(Tribe, unique=False, null=False)
    emoji = TextField(null=False, default='')
    guild_id = BitField(unique=False, null=False)

    class Meta:
        indexes = ((('tribe', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes

    def get_by_name(name: str, guild_id: int):
        tribe_match = TribeFlair.select(TribeFlair, Tribe).join(Tribe).where(
            (Tribe.name.contains(name)) & (TribeFlair.guild_id == guild_id)
        )

        if len(tribe_match) == 0:
            return None
        else:
            return tribe_match[0]

    def upsert(name: str, guild_id: int, emoji: str):
        try:
            tribe = Tribe.get(Tribe.name.contains(name))
        except DoesNotExist:
            raise exceptions.CheckFailedError(f'Could not find any tribe name containing "{name}"')

        tribeflair, created = TribeFlair.get_or_create(tribe=tribe, guild_id=guild_id, defaults={'emoji': emoji})
        if not created:
            tribeflair.emoji = emoji
            tribeflair.save()

        return tribeflair


class Game(BaseModel):
    name = TextField(null=True)
    is_completed = BooleanField(default=False)
    is_confirmed = BooleanField(default=False)  # Use to confirm losses and filter searches?
    is_pending = BooleanField(default=True)     # For matchmaking
    announcement_message = BitField(default=None, null=True)
    announcement_channel = BitField(default=None, null=True)
    date = DateField(default=datetime.datetime.today)
    completed_ts = DateTimeField(null=True, default=None)
    name = TextField(null=True)
    lineup_channels = BinaryJSONField(null=True)
    winning_lineup = SmallIntegerField(null=True)
    team_size = SmallIntegerField(null=False, default=1)
    elo_changes_team = BinaryJSONField(null=True)
    elo_changes_squad = BinaryJSONField(null=True)

    def load_full_game(game_id: int):
        # Returns a single Game object with all related tables pre-fetched. or None

        game = Game.select().where(Game.id == game_id)

        subq = Lineup.select(Lineup, TribeFlair, Tribe, Team, Player, DiscordMember, Squad).join_from(
            Lineup, Player).join(DiscordMember).join_from(
            Lineup, Squad, JOIN.LEFT_OUTER).join_from(
            Lineup, Team).join_from(
            Lineup, TribeFlair, JOIN.LEFT_OUTER).join(Tribe, JOIN.LEFT_OUTER)

        res = prefetch(game, subq)

        if len(res) == 0:
            raise DoesNotExist()
        return res[0]

    def create_game(teams, guild_id, name=None, require_teams=False):

        # Determine what Team guild members are associated with
        home_team_flag, list_of_home_teams = Player.get_teams_of_players(guild_id=guild_id, list_of_players=teams[0])  # get list of what server team each player is on, eg Ronin, Jets.
        away_team_flag, list_of_away_teams = Player.get_teams_of_players(guild_id=guild_id, list_of_players=teams[1])

        if (None in list_of_away_teams) or (None in list_of_home_teams):
            if require_teams is True:
                raise exceptions.CheckFailedError('One or more players listed cannot be matched to a Team (based on Discord Roles). Make sure player has exactly one matching Team role.')
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
            newgame = Game.create(name=name,
                                  team_size=max(len(teams[0]), len(teams[1])),
                                  is_pending=False)

            side_home_players = []
            side_away_players = []
            # Create/update Player records
            for player_discord, player_team in zip(teams[0], list_of_home_teams):
                side_home_players.append(Player.upsert(player_discord, guild_id=guild_id, team=player_team)[0])

            for player_discord, player_team in zip(teams[1], list_of_away_teams):
                side_away_players.append(Player.upsert(player_discord, guild_id=guild_id, team=player_team)[0])

            # Create/update Squad records
            home_squad, away_squad = None, None
            if len(side_home_players) > 1:
                home_squad = Squad.upsert(player_list=side_home_players)
            if len(side_away_players) > 1:
                away_squad = Squad.upsert(player_list=side_away_players)

            # lineup_num = Lineup.next_lineup_num()
            for p in side_home_players:
                Lineup.create(lineup_num=Lineup.next_lineup_num(), game=newgame, squad=home_squad, team=home_side_team, player=p)

            # lineup_num = Lineup.next_lineup_num()
            for p in side_away_players:
                Lineup.create(lineup_num=Lineup.next_lineup_num(), game=newgame, squad=away_squad, team=away_side_team, player=p)

        return newgame

    def declare_winner(self, winning_lineup, confirm: bool):

        # TODO: does not support games != 2 sides

        winning_side, losing_side = [], []

        for lineup in self.lineup:
            if lineup.lineup_num == winning_lineup:
                winning_side.append(lineup)
            else:
                losing_side.append(lineup)

        print(winning_side, losing_side)

        # STEP 1: INDIVIDUAL/PLAYER ELO

        def average_elo_from_list(list_of_lineups):
            elo_list = [p.player.elo for p in list_of_lineups]
            return round(sum(elo_list) / len(elo_list))

        winning_side_ave_elo = average_elo_from_list(winning_side)
        losing_side_ave_elo = average_elo_from_list(losing_side)

        for winning_member in winning_side:
            winning_member.change_elo_after_game(my_side_elo=winning_side_ave_elo, opponent_elo=losing_side_ave_elo, is_winner=True)

        for losing_member in losing_side:
            losing_member.change_elo_after_game(my_side_elo=losing_side_ave_elo, opponent_elo=winning_side_ave_elo, is_winner=False)

    def return_participant(self, ctx, player=None, team=None):
        # Given a string representing a player or a team (team name, player name/nick/ID)
        # Return a tuple of the participant and their squadgame, ie Player, SquadGame or Team, Squadgame

        if player:
            print('here')
            player_obj = Player.get_by_string(player_string=player, guild_id=ctx.guild.id)
            if not player_obj:
                raise exceptions.CheckFailedError(f'Cannot find a player with name "{player}". Try specifying with an @Mention.')
            if len(player_obj) > 1:
                raise exceptions.CheckFailedError(f'More than one player match found for "{player}". Be more specific.')
            player_obj = player_obj[0]
            print(len(self.lineup))
            for p in self.lineup:
                print(p, player_obj)
                if p.player == player_obj:
                    return player_obj, p.lineup_num

            raise exceptions.CheckFailedError(f'{player_obj.name} did not play in game {self.id}.')

        elif team:
            team_obj = Team.get_by_name(team_name=team, guild_id=ctx.guild.id)
            if not team_obj:
                raise exceptions.CheckFailedError(f'Cannot find a team with name "{team}".')
            if len(team_obj) > 1:
                raise exceptions.CheckFailedError(f'More than one team match found for "{team}". Be more specific.')
            team_obj = team_obj[0]

            for t in self.lineup:
                if t.team == team_obj:
                    return team_obj, t.lineup_num

            raise exceptions.CheckFailedError(f'{team_obj.name} did not play in game {self.id}.')
        else:
            raise exceptions.CheckFailedError('Player name or team name must be supplied for this function')

    def get_winner(self):
        # Returns player name of winner if its a 1v1, or team-name of winning side if its a group game

        for lineup in self.lineup:
            if lineup.lineup_num == self.winning_lineup:
                if self.team_size > 1:
                    return lineup.team
                return lineup.player

        return None


class Squad(BaseModel):
    elo = SmallIntegerField(default=1000)

    def upsert(player_list):

        squads = Squad.get_matching_squad(player_list)

        if len(squads) == 0:
            # Insert new squad based on this combination of players
            sq = Squad.create()
            for p in player_list:
                SquadMember.create(player=p, squad=sq)
            return sq

        return squads[0]

    def get_matching_squad(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns squad with exactly the same participating players. See https://stackoverflow.com/q/52010522/1281743
        query = Squad.select().join(SquadMember).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list)) & (fn.SUM(SquadMember.player.not_in(player_list).cast('integer')) == 0)
        )

        return query


class SquadMember(BaseModel):
    player = ForeignKeyField(Player, null=False, on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadmembers', on_delete='CASCADE')


class Lineup(BaseModel):
    lineup_num = SmallIntegerField(null=False, unique=False)
    tribe = ForeignKeyField(TribeFlair, null=True)
    game = ForeignKeyField(Game, null=False, backref='lineup', on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=True, backref='lineup')
    team = ForeignKeyField(Team, null=False, backref='lineup')
    player = ForeignKeyField(Player, null=False, backref='lineup')
    elo_change_player = SmallIntegerField(default=0)

    def next_lineup_num():
        # Return an integer lineup_num that is one higher than current max(lineup_num)
        # Access line: p = Lineup.next_lineup_num()
        q = Lineup.select(fn.COALESCE(fn.MAX(Lineup.lineup_num), 0).alias('next_lineup_num'))
        return q[0].next_lineup_num + 1

    def change_elo_after_game(self, my_side_elo, opponent_elo, is_winner):
        # Average(Away Side Elo) is compared to Average(Home_Side_Elo) for calculation - ie all members on a side will have the same elo_delta
        # Team A: p1 900 elo, p2 1000 elo = 950 average
        # Team B: p1 1000 elo, p2 1200 elo = 1100 average
        # ELO is compared 950 vs 1100 and all players treated equally

        num_games = self.player.completed_game_count()

        if num_games < 6:
            max_elo_delta = 75
        elif num_games < 11:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - my_side_elo) / 400.0))), 3)

        if is_winner is True:
            new_elo = round(my_side_elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(my_side_elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - my_side_elo)
        print(f'Player chance of winning: {chance_of_winning} opponent elo:{opponent_elo} my_side_elo: {my_side_elo},'
                f'elo_delta {elo_delta}, current_player_elo {self.player.elo}, new_player_elo {int(self.player.elo + elo_delta)}')

        self.player.elo = int(self.player.elo + elo_delta)
        self.elo_change_player = elo_delta
        self.player.save()
        self.save()


with db:
    db.create_tables([Team, DiscordMember, Game, Player, Tribe, Squad, SquadMember, Lineup, TribeFlair])
    # Only creates missing tables so should be safe to run each time
