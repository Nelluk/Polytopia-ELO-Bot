import datetime
import discord
import re
# import psycopg2
from psycopg2.errors import DuplicateObject
from peewee import *
from playhouse.postgres_ext import *
import modules.exceptions as exceptions
# from modules import utilities
from modules import channels
import statistics
import settings
import logging

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')

db = PostgresqlDatabase(settings.psql_db, user=settings.psql_user)


def tomorrow():
    return (datetime.datetime.now() + datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")


class BaseModel(Model):
    class Meta:
        database = db


class Team(BaseModel):
    name = TextField(unique=False, null=False)
    elo = SmallIntegerField(default=1000)
    elo_alltime = SmallIntegerField(default=1000)
    emoji = TextField(null=False, default='')
    image_url = TextField(null=True)
    guild_id = BitField(unique=False, null=False)
    is_hidden = BooleanField(default=False)             # True = generic team ie Home/Away, False = server team like Ronin
    pro_league = BooleanField(default=True)
    external_server = BitField(unique=False, null=True)

    class Meta:
        indexes = ((('name', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes

    def get_by_name(team_name: str, guild_id: int):
        teams = Team.select().where((Team.name.contains(team_name)) & (Team.guild_id == guild_id))
        return teams

    def get_or_except(team_name: str, guild_id: int):
        results = Team.get_by_name(team_name=team_name, guild_id=guild_id)
        if len(results) == 0:
            raise exceptions.NoMatches(f'No matching team was found for "{team_name}"')
        if len(results) > 1:
            raise exceptions.TooManyMatches(f'More than one matching team was found for "{team_name}"')

        return results[0]

    def completed_game_count(self):

        num_games = GameSide.select().join(Game).where(
            (GameSide.team == self) & (GameSide.game.is_completed == 1) & (GameSide.game.is_ranked == 1)
        ).count()

        return num_games

    def change_elo_after_game(self, chance_of_winning: float, is_winner: bool):

        # if self.completed_game_count() < 11:
        #     max_elo_delta = 50
        # else:
        #     max_elo_delta = 32
        max_elo_delta = 32

        if is_winner is True:
            elo_delta = int(round((max_elo_delta * (1 - chance_of_winning)), 0))
        else:
            elo_delta = int(round((max_elo_delta * (0 - chance_of_winning)), 0))

        # self.elo = int(self.elo + elo_delta)
        # self.save()

        return elo_delta

    def get_record(self, alltime=True):

        if alltime:
            date_cutoff = datetime.date.min
        else:
            date_cutoff = datetime.datetime.strptime(settings.team_elo_reset_date, "%m/%d/%Y").date()

        wins = GameSide.select().join(Game).where(
            (GameSide.size > 1) & (Game.is_completed == 1) & (Game.is_confirmed == 1) &
            (Game.is_ranked == 1) & (GameSide.team == self) &
            (GameSide.id == Game.winner) & (Game.date > date_cutoff)
        ).count()

        losses = GameSide.select().join(Game).where(
            (GameSide.size > 1) & (Game.is_completed == 1) & (Game.is_confirmed == 1) &
            (Game.is_ranked == 1) & (GameSide.team == self) &
            (GameSide.id != Game.winner) & (Game.date > date_cutoff)
        ).count()

        return (wins, losses)


class DiscordMember(BaseModel):
    discord_id = BitField(unique=True, null=False)
    name = TextField(unique=False)
    elo = SmallIntegerField(default=1000)
    elo_max = SmallIntegerField(default=1000)
    polytopia_id = TextField(null=True)
    polytopia_name = TextField(null=True)
    is_banned = BooleanField(default=False)
    timezone_offset = SmallIntegerField(default=None, null=True)
    date_polychamps_invite_sent = DateField(default=None, null=True)

    def advanced_stats(self):

        server_list = settings.servers_included_in_global_lb()

        ranked_games_played = Game.select().join(Lineup).join(Player).where(
            (Player.discord_member == self) &
            (Game.is_completed == 1) &
            (Game.is_ranked == 1) &
            (Game.is_confirmed == 1) &
            (Game.guild_id.in_(server_list))
        ).order_by(Game.completed_ts).prefetch(GameSide, Lineup, Player)

        winning_streak, losing_streak, longest_winning_streak, longest_losing_streak = 0, 0, 0, 0
        v2_count, v3_count = 0, 0  # wins of 1v2 or 1v3 games
        duel_wins, duel_losses = 0, 0  # 1v1 matchup stats
        last_win, last_loss = False, False

        for game in ranked_games_played:

            is_winner = False
            for gs in game.gamesides:
                # going through in this way uses the results already in memory rather than a bunch of new DB queries
                if gs.id == game.winner_id:
                    winner = gs
                    for l in gs.lineup:
                        if l.player.discord_member_id == self.id:
                            is_winner = True
                    break

            logger.debug(f'Game {game.id} completed_ts {game.completed_ts} is a {"win" if is_winner else "loss"} WS: {winning_streak} LS: {losing_streak} last_win: {last_win} last_loss: {last_loss}')
            if is_winner:
                if last_win:
                    # winning streak is extended
                    winning_streak += 1
                    longest_winning_streak = winning_streak if (winning_streak > longest_winning_streak) else longest_winning_streak
                else:
                    # winning streak is broken
                    winning_streak = 1
                    last_win, last_loss = True, False
                if len(winner.lineup) == 1 and len(game.gamesides) == 2:
                    size_of_opponent = game.largest_team()

                    if size_of_opponent == 1:
                        duel_wins += 1
                    elif size_of_opponent == 2:
                        v2_count += 1
                    elif size_of_opponent == 3:
                        v3_count += 1
            else:
                if last_loss:
                    # losing streak is extended
                    losing_streak += 1
                    longest_losing_streak = losing_streak if losing_streak > longest_losing_streak else longest_losing_streak
                else:
                    # winning streak is broken
                    losing_streak = 1
                    last_win, last_loss = False, True

                if len(game.gamesides) == 2 and game.largest_team() == 1 and game.smallest_team() == 1:
                    duel_losses += 1

        return (longest_winning_streak, longest_losing_streak, v2_count, v3_count, duel_wins, duel_losses)

    def update_name(self, new_name: str):
        self.name = new_name
        self.save()
        for guildmember in self.guildmembers:
            guildmember.generate_display_name(player_name=new_name, player_nick=guildmember.nick)

    def wins(self):

        server_list = settings.servers_included_in_global_lb()
        q = Lineup.select().join(Game).join_from(Lineup, GameSide).join_from(Lineup, Player).where(
            (Lineup.game.is_completed == 1) &
            (Lineup.game.is_confirmed == 1) &
            (Lineup.game.is_ranked == 1) &
            (Lineup.game.guild_id.in_(server_list)) &
            (Lineup.player.discord_member == self) &
            (Game.winner == Lineup.gameside.id)
        )

        return q

    def losses(self):
        server_list = settings.servers_included_in_global_lb()
        q = Lineup.select().join(Game).join_from(Lineup, GameSide).join_from(Lineup, Player).where(
            (Lineup.game.is_completed == 1) &
            (Lineup.game.is_confirmed == 1) &
            (Lineup.game.is_ranked == 1) &
            (Lineup.game.guild_id.in_(server_list)) &
            (Lineup.player.discord_member == self) &
            (Game.winner != Lineup.gameside.id)
        )

        return q

    def get_record(self):

        return (self.wins().count(), self.losses().count())

    def games_played(self, in_days: int = None):

        if in_days:
            date_cutoff = (datetime.datetime.now() + datetime.timedelta(days=-in_days))
        else:
            date_cutoff = datetime.date.min  # 'forever' ?

        return Lineup.select(Lineup.game).join(Game).join_from(Lineup, Player).where(
            (Lineup.game.date > date_cutoff) & (Lineup.player.discord_member == self)
        ).order_by(-Game.date)

    def completed_game_count(self, only_ranked=True):

        if only_ranked:
            # default behavior, used for elo max_delta
            server_list = settings.servers_included_in_global_lb()
            num_games = Lineup.select().join(Player).join_from(Lineup, Game).where(
                (Lineup.game.is_completed == 1) &
                (Lineup.game.is_ranked == 1) &
                (Lineup.game.guild_id.in_(server_list)) &
                (Lineup.player.discord_member == self)
            ).count()
        else:
            # full count of all games played - used for achievements role setting
            num_games = Lineup.select().join(Player).join_from(Lineup, Game).where(
                (Lineup.game.is_completed == 1) & (Lineup.player.discord_member == self)
            ).count()

        return num_games

    def leaderboard_rank(self, date_cutoff):
        # TODO: This could be replaced with Postgresql Window functions to have the DB calculate the rank.
        # Advantages: Probably moderately more efficient, and will resolve ties in a sensible way
        # But no idea how to write the query :/
        # http://docs.peewee-orm.com/en/latest/peewee/query_examples.html#find-the-top-three-revenue-generating-facilities

        query = DiscordMember.leaderboard(date_cutoff=date_cutoff)

        is_found = False
        for counter, p in enumerate(query.tuples()):
            if p[0] == self.id:
                is_found = True
                break

        rank = counter + 1 if is_found else None
        return (rank, query.count())

    def leaderboard(date_cutoff, guild_id: int = None, max_flag: bool = False):
        # guild_id is a dummy parameter so DiscordMember.leaderboard and Player.leaderboard can be called in identical ways

        if max_flag:
            elo_field = DiscordMember.elo_max
        else:
            elo_field = DiscordMember.elo

        query = DiscordMember.select().join(Player).join(Lineup).join(Game).where(
            (Game.is_completed == 1) & (Game.date > date_cutoff) & (Game.is_ranked == 1) & (DiscordMember.is_banned == 0)
        ).distinct().order_by(-elo_field)

        if query.count() < 10:
            # Include all registered players on leaderboard if not many games played
            query = DiscordMember.select().order_by(-elo_field)

        return query

    def favorite_tribes(self, limit=3):
        # Returns a list of dicts of format:
        # {'tribe': 7, 'name': 'Luxidoor', 'tribe_count': 14}
        # doesnt include TribeFlair.emoji like Player.favorite_tribes() because it needs to get the emoji based on the context of the discord guild

        q = Lineup.select(Lineup.tribe, Tribe.name, fn.COUNT(Lineup.tribe).alias('tribe_count')).join(
            TribeFlair).join(Tribe).join_from(Lineup, Player).where(
            (Lineup.player.discord_member == self) & (Lineup.tribe.is_null(False))
        ).group_by(Lineup.tribe, Tribe.name).order_by(-SQL('tribe_count')).limit(limit)

        return q.dicts()

    def members_not_on_polychamps():
        # two_weeks = (datetime.datetime.now() + datetime.timedelta(days=-14))

        subq_members_in_polychamps = Player.select(Player.discord_member).where(Player.guild_id == settings.server_ids['polychampions'])

        query = DiscordMember.select().where(
            (DiscordMember.id.not_in(subq_members_in_polychamps)) & (DiscordMember.elo_max > 1075) & (DiscordMember.is_banned == 0) &
            # ((DiscordMember.date_polychamps_invite_sent < two_weeks) | (DiscordMember.date_polychamps_invite_sent.is_null(True)))
            (DiscordMember.date_polychamps_invite_sent.is_null(True))
        )

        return query


class Player(BaseModel):
    discord_member = ForeignKeyField(DiscordMember, unique=False, null=False, backref='guildmembers', on_delete='CASCADE')
    guild_id = BitField(unique=False, null=False)
    nick = TextField(unique=False, null=True)
    name = TextField(unique=False, null=True)
    team = ForeignKeyField(Team, null=True, backref='player', on_delete='SET NULL')
    elo = SmallIntegerField(default=1000)
    elo_max = SmallIntegerField(default=1000)
    trophies = ArrayField(CharField, null=True)
    is_banned = BooleanField(default=False)

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
            self.nick = player_nick
            self.save()
        return display_name

    def upsert(discord_id, guild_id, discord_name=None, discord_nick=None, team=None):
        # Stopped using postgres upsert on_conflict() because it only returns row ID so its annoying to use
        display_name = Player.generate_display_name(player_name=discord_name, player_nick=discord_nick)
        try:
            with db.atomic():
                discord_member = DiscordMember.create(discord_id=discord_id, name=discord_name)
        except IntegrityError:
            discord_member = DiscordMember.get(discord_id=discord_id)
            discord_member.name = discord_name
            discord_member.save()

        try:
            with db.atomic():
                player = Player.create(discord_member=discord_member, guild_id=guild_id, nick=discord_nick, name=display_name, team=team)
            created = True
            logger.debug(f'Inserting new player id {player.id} {display_name} on team {team}')
        except IntegrityError:
            created = False
            player = Player.get(discord_member=discord_member, guild_id=guild_id)
            logger.debug(f'Updating existing player id {player.id} {player.name}')
            if display_name:
                player.name = display_name
            if team:
                player.team = team
                logger.debug(f'Setting player team to {team.id} {team.name}')
            if discord_nick:
                player.nick = discord_nick
            player.save()

        return player, created

    def get_teams_of_players(guild_id, list_of_players):
        # TODO: make function async? Tried but got invalid syntax complaint in linter in the calling function

        # given [List, Of, discord.Member, Objects] - return a, b
        # a = binary flag if all members are on the same Poly team. b = [list] of the Team objects from table the players are on
        # input: [Nelluk, Frodakcin]
        # output: True, [<Ronin>, <Ronin>]

        def get_matching_roles(discord_member, list_of_role_names):
            # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.
            member_roles = [x.name for x in discord_member.roles]
            return set(member_roles).intersection(list_of_role_names)

        query = Team.select(Team.name).where(Team.guild_id == guild_id)
        list_of_teams = [team.name for team in query]               # ['The Ronin', 'The Jets', ...]
        list_of_matching_teams = []
        for player in list_of_players:
            matching_roles = get_matching_roles(player, list_of_teams)
            if len(matching_roles) > 0:
                # TODO: This would be more efficient to do as one query and then looping over the list of teams one time for each player
                name = next(iter(matching_roles))
                list_of_matching_teams.append(
                    Team.select().where(
                        (Team.name == name) & (Team.guild_id == guild_id)
                    ).get()
                )
            else:
                list_of_matching_teams.append(None)
                # Would be here if no player Roles match any known teams

        same_team_flag = True if all(x == list_of_matching_teams[0] for x in list_of_matching_teams) else False
        return same_team_flag, list_of_matching_teams

    def is_in_team(guild_id, discord_member):
        _, list_of_teams = Player.get_teams_of_players(guild_id=guild_id, list_of_players=[discord_member])
        if not list_of_teams or None in list_of_teams:
            logger.debug(f'is_in_team: False / None')
            return (False, None)
        logger.debug(f'is_in_team: True / {list_of_teams[0].id} {list_of_teams[0].name}')
        return (True, list_of_teams[0])

    def string_matches(player_string: str, guild_id: int, include_poly_info: bool = True):
        # Returns QuerySet containing players in current guild matching string. Searches against discord mention ID first, then exact discord name match,
        # then falls back to substring match on name/nick, then a lastly a substring match of polytopia ID or polytopia in-game name

        try:
            p_id = int(player_string.strip('<>!@'))
        except ValueError:
            pass
        else:
            # lookup either on <@####> mention string or raw ID #
            query_by_id = Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.discord_id == p_id) & (Player.guild_id == guild_id)
            )
            if query_by_id.count() > 0:
                return query_by_id

        if len(player_string.split('#', 1)[0]) > 2:
            discord_str = player_string.split('#', 1)[0]
            # If query is something like 'Nelluk#7034', use just the 'Nelluk' to match against discord_name.
            # This happens if user does an @Mention then removes the @ character
        else:
            discord_str = player_string

        name_exact_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
            (DiscordMember.name ** discord_str) & (Player.guild_id == guild_id)  # ** is case-insensitive
        )

        if name_exact_match.count() == 1:
            # String matches DiscordUser.name exactly
            return name_exact_match

        # If no exact match, return any substring matches - prioritized by number of games played

        name_substring_match = Lineup.select(Lineup.player, fn.COUNT('*').alias('games_played')).join(Player).join(DiscordMember).where(
            ((Player.nick.contains(player_string)) | (DiscordMember.name.contains(discord_str))) & (Player.guild_id == guild_id)
        ).group_by(Lineup.player).order_by(-SQL('games_played'))

        if name_substring_match.count() > 0:
            return [l.player for l in name_substring_match]

        if include_poly_info:
            # If no substring name matches, return anything with matching polytopia name or code
            poly_fields_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                ((DiscordMember.polytopia_id.contains(player_string)) | (DiscordMember.polytopia_name.contains(player_string))) & (Player.guild_id == guild_id)
            )
            return poly_fields_match
        else:
            # if include_poly_info == False, then do not fall back to searching by polytopia_id or polytopia_name
            return []

    def get_or_except(player_string: str, guild_id: int):
        results = Player.string_matches(player_string=player_string, guild_id=guild_id)
        if len(results) == 0:
            raise exceptions.NoMatches(f'No matching player was found for "{player_string}"')
        if len(results) > 1:
            raise exceptions.TooManyMatches(f'More than one matching player was found for "{player_string}"')

        logger.debug(f'get_or_except matched string {player_string} to player {results[0].id} {results[0].name} - team {results[0].team}')
        return results[0]

    def get_by_discord_id(discord_id: int, guild_id: int, discord_nick: str = None, discord_name: str = None):
        # if no matching player, will check to see if there is already a DiscordMember created from another guild's player
        # if exists, Player will be upserted
        # return PlayerObj, Bool. bool = True if player was upserted

        try:
            player = Player.select().join(DiscordMember).where(
                (DiscordMember.discord_id == discord_id) & (Player.guild_id == guild_id)).get()
            logger.debug(f'get_by_discord_id loaded player {player.id} {player.name} - team {player.team}')
            return player, False
        except DoesNotExist:
            pass

        # no current player. check to see if DiscordMember exists
        logger.debug(f'get_by_discord_id No matching player for guild {guild_id} and discord_id {discord_id} - discord_name passed {discord_name}')
        try:
            _ = DiscordMember.get(discord_id=discord_id)
        except DoesNotExist:
            # No matching player or discordmember
            logger.debug(f'get_by_discord_id No matching discord_member with discord_id {discord_id} - discord_name passed {discord_name}')
            return None, False
        else:
            # DiscordMember found, upserting new player
            player, _ = Player.upsert(discord_id=discord_id, discord_name=discord_name, discord_nick=discord_nick, guild_id=guild_id)
            logger.debug(f'get_by_discord_id Upserting new guild player for discord ID {discord_id}')
            return player, True

    def completed_game_count(self):

        num_games = Lineup.select().join(Game).where(
            (Lineup.game.is_completed == 1) & (Lineup.game.is_ranked == 1) & (Lineup.player == self)
        ).count()

        return num_games

    def games_played(self, in_days: int = None):

        if in_days:
            date_cutoff = (datetime.datetime.now() + datetime.timedelta(days=-in_days))
        else:
            date_cutoff = datetime.date.min  # 'forever' ?

        return Lineup.select(Lineup.game).join(Game).where(
            (Lineup.game.date > date_cutoff) & (Lineup.player == self)
        ).order_by(-Game.date)

    def wins(self):
        # TODO: Could combine wins/losses into one function that takes an argument and modifies query

        q = Lineup.select().join(Game).join_from(Lineup, GameSide).where(
            (Lineup.game.is_completed == 1) & (Lineup.game.is_confirmed == 1) & (Lineup.game.is_ranked == 1) & (Lineup.player == self) & (Game.winner == Lineup.gameside.id)
        )

        return q

    def losses(self):
        q = Lineup.select().join(Game).join_from(Lineup, GameSide).where(
            (Lineup.game.is_completed == 1) & (Lineup.game.is_confirmed == 1) & (Lineup.game.is_ranked == 1) & (Lineup.player == self) & (Game.winner != Lineup.gameside.id)
        )

        return q

    def get_record(self):

        return (self.wins().count(), self.losses().count())

    def leaderboard_rank(self, date_cutoff):
        # TODO: This could be replaced with Postgresql Window functions to have the DB calculate the rank.
        # Advantages: Probably moderately more efficient, and will resolve ties in a sensible way
        # But no idea how to write the query :/
        # http://docs.peewee-orm.com/en/latest/peewee/query_examples.html#find-the-top-three-revenue-generating-facilities

        query = Player.leaderboard(date_cutoff=date_cutoff, guild_id=self.guild_id)

        player_found = False
        for counter, p in enumerate(query.tuples()):
            if p[0] == self.id:
                player_found = True
                break

        rank = counter + 1 if player_found else None
        return (rank, query.count())

    def leaderboard(date_cutoff, guild_id: int, max_flag: bool = False):
        if max_flag:
            elo_field = Player.elo_max
        else:
            elo_field = Player.elo

        query = Player.select().join(Lineup).join(Game).join_from(Player, DiscordMember).where(
            (Player.guild_id == guild_id) &
            (Game.is_completed == 1) &
            (Game.is_ranked == 1) &
            (Game.date > date_cutoff) &
            (Player.is_banned == 0) & (DiscordMember.is_banned == 0)
        ).distinct().order_by(-elo_field)

        if query.count() < 10:
            # Include all registered players on leaderboard if not many games played
            query = Player.select().where(Player.guild_id == guild_id).order_by(-elo_field)

        return query

    def favorite_tribes(self, limit=3):
        # Returns a list of dicts of format:
        # {'tribe': 7, 'emoji': '<:luxidoor:448015285212151809>', 'name': 'Luxidoor', 'tribe_count': 14}

        q = Lineup.select(Lineup.tribe, TribeFlair.emoji, Tribe.name, fn.COUNT(Lineup.tribe).alias('tribe_count')).join(TribeFlair).join(Tribe).where(
            (Lineup.player == self) & (Lineup.tribe.is_null(False))
        ).group_by(Lineup.tribe, Lineup.tribe.emoji, Tribe.name).order_by(-SQL('tribe_count')).limit(limit)

        return q.dicts()

    def weighted_elo_of_player_list(list_of_discord_ids, guild_id):

        # Given a group of discord_ids (likely teammates) come up with an average ELO for that group, weighted by how active they are
        # ie if a team has two players and the guy with 1500 elo plays a lot and the guy with 1000 elo plays not at all, 1500 will be the weighted median elo
        players = Player.select(Player, DiscordMember).join(DiscordMember).where(
            (DiscordMember.discord_id.in_(list_of_discord_ids)) & (Player.guild_id == guild_id)
        )

        elo_list = []
        elo_list1, elo_list2, elo_list3 = [], [], []
        player_games = 0
        for p in players:
            # print(p.elo, p.games_played(in_days=30).count())
            games_played = p.games_played(in_days=30).count()
            player_elos = [p.elo] * games_played
            elo_list = elo_list + player_elos
            player_games += games_played

            elo_list1 = elo_list1 + [p.elo] * min(games_played, 10)
            elo_list2 = elo_list2 + [p.elo] * min(games_played, 2)
            elo_list3 = elo_list3 + [p.elo]

        if elo_list:
            logger.info(f'Full weighting: {int(statistics.mean(elo_list))}')
            logger.info(f'Min10: {int(statistics.mean(elo_list1))}')
            logger.info(f'Min5: {int(statistics.mean(elo_list2))}')
            logger.info(f'Median no weighting: {int(statistics.median(elo_list3))}')

            # return int(statistics.mean(elo_list)), player_games
            return int(statistics.mean(elo_list1)), player_games

        return 0, 0

    class Meta:
        indexes = ((('discord_member', 'guild_id'), True),)   # Trailing comma is required


class Tribe(BaseModel):
    name = TextField(unique=True, null=False)


class TribeFlair(BaseModel):
    tribe = ForeignKeyField(Tribe, unique=False, null=False, on_delete='CASCADE')
    emoji = TextField(null=False, default='')
    guild_id = BitField(unique=False, null=False)

    class Meta:
        indexes = ((('tribe', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes

    def get_by_name(name: str, guild_id: int):
        tribe_flair_match = TribeFlair.select(TribeFlair, Tribe).join(Tribe).where(
            (Tribe.name.startswith(name)) & (TribeFlair.guild_id == guild_id)
        )

        tribe_name_match = Tribe.select().where(Tribe.name.startswith(name))

        if tribe_flair_match.count() == 0:
            if tribe_name_match.count() == 0:
                logger.warn(f'No TribeFlair -or- Tribe could be matched to {name}')
                return None
            else:
                logger.warn(f'No TribeFlair for this guild matched to {tribe_name_match[0].name}. Creating TribeFlair with blank emoji.')
                with db.atomic():
                    new_tribeflair = TribeFlair.create(tribe=tribe_name_match[0], guild_id=guild_id)
                    return new_tribeflair
        else:
            return tribe_flair_match[0]

    def upsert(name: str, guild_id: int, emoji: str):
        try:
            tribe = Tribe.get(Tribe.name.startswith(name))
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
    is_confirmed = BooleanField(default=False)
    announcement_message = BitField(default=None, null=True)
    announcement_channel = BitField(default=None, null=True)
    date = DateField(default=datetime.datetime.today)
    completed_ts = DateTimeField(null=True, default=None)  # set when game is confirmed and ELO is calculated
    win_claimed_ts = DateTimeField(null=True, default=None)  # set when win is claimed, used to check old unconfirmed wins
    name = TextField(null=True)
    winner = DeferredForeignKey('GameSide', null=True, on_delete='RESTRICT')
    guild_id = BitField(unique=False, null=False)
    host = ForeignKeyField(Player, null=True, backref='hosting', on_delete='SET NULL')
    expiration = DateTimeField(null=True, default=tomorrow)  # For pending/matchmaking status
    notes = TextField(null=True)
    is_pending = BooleanField(default=False)
    is_ranked = BooleanField(default=True)
    game_chan = BitField(default=None, null=True)

    def __setattr__(self, name, value):
        if name == 'name':
            value = value.strip('\"').strip('\'').strip('”').strip('“').title()[:35].strip() if value else value
        return super().__setattr__(name, value)

    async def create_game_channels(self, guild_list, guild_id):
        guild = discord.utils.get(guild_list, id=guild_id)
        game_roster, side_external_servers = [], []
        ordered_side_list = list(self.ordered_side_list())

        for s in ordered_side_list:
            lineup_list = s.ordered_player_list()
            playernames = [l.player.name for l in lineup_list]
            player_external_servers = [l.player.team.external_server if l.player.team else None for l in lineup_list]
            logger.debug(player_external_servers)
            if player_external_servers[0] and all(x == player_external_servers[0] for x in player_external_servers):
                side_external_servers.append(player_external_servers[0])
                #  All players on a side are on a team that uses the same external server, ie all Ronin or Ronin+Bandits
            else:
                #  Truly heterogenous team, ie. Ronin+Lightning players
                side_external_servers.append(None)
            game_roster.append(f'Side **{s.name()}**: {", ".join(playernames)}')
        logger.debug(f'Side_external_servers: {side_external_servers}')
        roster_names = '\n'.join(game_roster)  # "Side **Home**: Nelluk, player2\n Side **Away**: Player 3, Player 4"

        for gameside, side_external_server in zip(ordered_side_list, side_external_servers):
            logger.debug(f'Checking for external server usage for side {gameside.id}: {side_external_server}')
            if side_external_server and discord.utils.get(guild_list, id=side_external_server):
                side_guild = discord.utils.get(guild_list, id=side_external_server)  # use team-specific external server
                using_team_server_flag = True
                logger.debug('using external guild to create team channel')
            else:
                side_guild = guild  # use current guild (ctx.guild)
                using_team_server_flag = False

            player_list = [l.player for l in gameside.ordered_player_list()]
            if len(player_list) < 2:
                continue
            if len(guild.text_channels) > 475 and len(player_list) < 3:
                raise exceptions.MyBaseException('Server has nearly reached the maximum number of channels: skipping channel creation for this game.')
            chan = await channels.create_game_channel(side_guild, game=self, team_name=gameside.team.name, player_list=player_list, using_team_server_flag=using_team_server_flag)
            if chan:
                gameside.team_chan = chan.id
                if side_guild.id != guild_id:
                    gameside.team_chan_external_server = side_guild.id
                gameside.save()

                await channels.greet_game_channel(side_guild, chan=chan, player_list=player_list, roster_names=roster_names, game=self, full_game=False)

        if (len(ordered_side_list) > 2 and len(self.lineup) > 5) or len(ordered_side_list) > 3:
            # create game channel for larger games - 4+ sides, or 3+ sides with 6+ players
            player_list = [l.player for l in self.lineup]
            chan = await channels.create_game_channel(guild, game=self, team_name=None, player_list=player_list)
            if chan:
                self.game_chan = chan.id
                self.save()
                await channels.greet_game_channel(guild, chan=chan, player_list=player_list, roster_names=roster_names, game=self, full_game=True)

    async def delete_game_channels(self, guild_list, guild_id):
        guild = discord.utils.get(guild_list, id=guild_id)

        if self.name and (self.name.lower()[:2] == 's4' or self.name.lower()[:2] == 's5' or self.name.lower()[:3] == 'wwn'):
            last_week = (datetime.datetime.now() + datetime.timedelta(days=-7))
            if self.completed_ts > last_week:
                return logger.warn(f'Skipping team channel deletion for game {self.id} {self.name} since it is a Season game concluded recently')

        for gameside in self.gamesides:
            if gameside.team_chan:
                if gameside.team_chan_external_server:
                    side_guild = discord.utils.get(guild_list, id=gameside.team_chan_external_server)
                    if not side_guild:
                        logger.warn(f'Could not load guild where external team channel is located, gameside ID {gameside.id} guild {gameside.team_chan_external_server}')
                        continue
                else:
                    side_guild = guild
                await channels.delete_game_channel(side_guild, channel_id=gameside.team_chan)
                gameside.team_chan = None
                gameside.save()

        if self.game_chan:
            await channels.delete_game_channel(guild, channel_id=self.game_chan)
            self.game_chan = None
            self.save()

    async def update_squad_channels(self, guild_list, guild_id, message: str = None):
        guild = discord.utils.get(guild_list, id=guild_id)
        game_chan = self.game_chan  # loading early here trying to avoid InterfaceError

        for gameside in list(self.gamesides):
            if gameside.team_chan:
                if gameside.team_chan_external_server:
                    side_guild = discord.utils.get(guild_list, id=gameside.team_chan_external_server)
                    if not side_guild:
                        logger.warn(f'Could not load guild where external team channel is located, gameside ID {gameside.id} guild {gameside.team_chan_external_server}')
                        continue
                    logger.debug(f'Using guild {side_guild} for side_guild')
                else:
                    logger.debug(f'Using default guild {guild} for side_guild')
                    side_guild = guild
                if message:
                    logger.debug(f'Pinging message to channel {gameside.team_chan} in guild {side_guild}')
                    await channels.send_message_to_channel(side_guild, channel_id=gameside.team_chan, message=message)
                else:
                    await channels.update_game_channel_name(side_guild, channel_id=gameside.team_chan, game_id=self.id, game_name=self.name, team_name=gameside.team.name)

        if game_chan:
            if message:
                await channels.send_message_to_channel(guild, channel_id=game_chan, message=message)
            else:
                await channels.update_game_channel_name(guild, channel_id=game_chan, game_id=self.id, game_name=self.name, team_name=None)

    async def update_announcement(self, guild, prefix):
        # Updates contents of new game announcement with updated game_embed card

        if self.announcement_channel is None or self.announcement_message is None:
            return
        channel = guild.get_channel(self.announcement_channel)
        if channel is None:
            return logger.warn('Couldn\'t get channel in update_announacement')

        try:
            message = await channel.get_message(self.announcement_message)
        except (discord.errors.Forbidden, discord.errors.NotFound, discord.errors.HTTPException):
            return logger.warn('Couldn\'t get message in update_announacement')

        try:
            embed, content = self.embed(guild=guild, prefix=prefix)
            await message.edit(embed=embed, content=content)
        except discord.errors.HTTPException:
            return logger.warn('Couldn\'t update message in update_announacement')

    def is_hosted_by(self, discord_id: int):

        if not self.host:
            return False, None
        return self.host.discord_member.discord_id == discord_id, self.host

    def is_created_by(self, discord_id: int):
        creating_player = self.creating_player()
        return creating_player.discord_member.discord_id == discord_id if creating_player else False

    def creating_player(self):
        # return Player who is in 'first position' for this game, ie. the game creator in Polytopia
        # will not always be Game.host if it was a staff member who removed themselves from lineup
        first_side = self.ordered_side_list().limit(1).get()
        side_players = first_side.ordered_player_list()
        if side_players:
            return first_side.ordered_player_list()[0].player
        return None

    def draft_order(self):
        # Returns list of tuples, in order of recommended draft order list:
        # [(Side #, Side Name, Player 1), ... ]

        players, capacity = self.capacity()
        if players < capacity:
            raise exceptions.CheckFailedError('This match is not full')

        sides = self.ordered_side_list()

        picks = []
        side_objs = [{'side': s, 'pick_score': 0, 'size': s.size, 'lineups': s.ordered_player_list()} for s in sides]
        num_tribes, max_tribes = sum([s.size for s in sides]), max([s.size for s in sides])
        num2 = num_tribes

        # adjust for imbalanced team sizes
        for side in side_objs:
            side['pick_score'] -= max_tribes - side['size']

        for pick in range(num_tribes):
            picking_team = None
            lowest_score = 99
            # for team in teams:
            for side_obj in side_objs:
                # Find side with lowest pick_score
                if side_obj['pick_score'] <= lowest_score and lowest_score > 0 and side_obj['size'] > 0:
                    picking_team = side_obj
                    lowest_score = side_obj['pick_score']

            picking_team['pick_score'] = picking_team['pick_score'] + num_tribes
            picking_team['size'] = picking_team['size'] - 1
            num_tribes = num_tribes - 1
            # picks.append(
            #     (picking_team['side'].position, picking_team['side'].sidename, picking_team['lineups'].pop(0))
            # )
            picks.append({'position': picking_team['side'].position,
                          'sidename': picking_team['side'].sidename,
                          'player': picking_team['lineups'].pop(0).player,
                          'picking_team': picking_team})

        changed = 1
        while changed > 0:
            changed = 0
            for i in range(num2 - 1, 1, -1):
                if picks[i - 1]['picking_team']['pick_score'] - picks[i]['picking_team']['pick_score'] > 1:
                    picks[i - 1]['picking_team']['pick_score'] -= 1
                    picks[i]['picking_team']['pick_score'] += 1
                    (picks[i - 1], picks[i]) = (picks[i], picks[i - 1])
                    changed = 1

        return picks

    def ordered_side_list(self):
        return GameSide.select().where(GameSide.game == self).order_by(GameSide.position)

    def embed(self, guild, prefix):
        if self.is_pending:
            return self.embed_pending_game(prefix)
        ranked_str = '' if self.is_ranked else 'Unranked — '
        embed = discord.Embed(title=f'{self.get_headline()} — {ranked_str}*{self.size_string()}*'[:255])

        if self.is_completed == 1:
            if len(embed.title) > 240:
                embed.title = embed.title.replace('**', '')  # Strip bold markdown to regain space in extra-long game titles
            embed.title = (embed.title + f'\n\nWINNER: {self.winner.name()}')[:255]

            # Set embed image (profile picture or team logo)
            if len(self.winner.lineup) == 1:
                # Winner is individual player
                winning_discord_member = guild.get_member(self.winner.lineup[0].player.discord_member.discord_id)
                if winning_discord_member is not None:
                    embed.set_thumbnail(url=winning_discord_member.avatar_url_as(size=512))
            elif self.winner.team and self.winner.team.image_url:
                # Winner is a team of players - use team image if present
                embed.set_thumbnail(url=self.winner.team.image_url)

        game_data = []
        for gameside in self.ordered_side_list():
            team_elo_str, squad_elo_str = gameside.elo_strings()

            if not gameside.team or gameside.team.is_hidden:
                # Hide team ELO if generic Team
                team_elo_str = '\u200b'

            if len(gameside.lineup) == 1:
                # Hide gamesides ELO stats for 1-player teams
                squad_elo_str = '\u200b'

            game_data.append((gameside, team_elo_str, squad_elo_str, gameside.roster()))

        use_separator = False
        for side, elo_str, squad_str, roster in game_data:

            if use_separator:
                embed.add_field(name='\u200b', value='\u200b', inline=False)  # Separator between sides

            if len(side.lineup) > 1:
                team_str = f'__Lineup for Team **{side.team.name if side.team else "None"}**__ {elo_str}'

                embed.add_field(name=team_str, value=squad_str, inline=False)

            for player, player_elo_str, tribe_emoji in roster:
                if len(side.lineup) > 1:
                    embed.add_field(name=f'**{player.name}** {tribe_emoji}', value=f'ELO: {player_elo_str}', inline=True)
                else:
                    embed.add_field(name=f'__**{player.name}**__ {tribe_emoji}', value=f'ELO: {player_elo_str}', inline=True)
            use_separator = True

        if len(self.gamesides) == 2:
            series_record = self.series_record()
            if series_record[0][1] == 0:
                series_record_str = ''
            elif series_record[0][1] == series_record[1][1]:
                series_record_str = f'*The series record for these two opponents is tied at **{series_record[0][1]} - {series_record[0][1]}***'
            else:
                series_record_str = f'***{series_record[0][0].name()}** leads this series **{series_record[0][1]} - {series_record[1][1]}***'
        else:
            series_record_str = ''

        if self.notes or series_record_str:
            embed_content = f'**Notes:** {self.notes}\n' if self.notes else ''
            embed_content = embed_content + f'{series_record_str}' if series_record_str else embed_content
        else:
            embed_content = None

        if guild.id != settings.server_ids['polychampions']:
            # embed.add_field(value='Powered by **PolyChampions** - https://discord.gg/cX7Ptnv', name='\u200b', inline=False)
            embed.set_author(name='PolyChampions', url='https://discord.gg/cX7Ptnv', icon_url='https://cdn.discordapp.com/emojis/488510815893323787.png?v=1')

        if self.host:
            host_str = f' - Hosted by {self.host.name[:20]}'
        else:
            host_str = ''

        if not self.is_completed:
            status_str = 'Incomplete'
        elif self.is_confirmed:
            status_str = 'Completed'
        else:
            status_str = 'Unconfirmed'

        if self.completed_ts:
            completed_str = f' - Completed {self.completed_ts.strftime("%Y-%m-%d %H:%M:%S")}'
        else:
            completed_str = ''

        embed.set_footer(text=f'{status_str} - Created {str(self.date)}{completed_str}{host_str}')

        return embed, embed_content

    def embed_pending_game(self, prefix):
        ranked_str = 'Unranked ' if not self.is_ranked else ''
        title_str = f'**{ranked_str}Open Game {self.id}**\n{self.size_string()}'
        if self.host:
            title_str += f' *hosted by* {self.host.name}'

        embed = discord.Embed(title=title_str)
        notes_str = self.notes if self.notes else "\u200b"
        content_str = None

        if self.expiration < datetime.datetime.now():
            expiration_str = f'*Expired*'
            status_str = 'Expired'
        else:
            expiration_str = f'{int((self.expiration - datetime.datetime.now()).total_seconds() / 3600.0)} hours'
            status_str = f'Open - `{prefix}join {self.id}`'

        players, capacity = self.capacity()
        if players >= capacity:
            if not self.is_pending:
                status_str = f'Started - **{self.name}**' if self.name else 'Started'
            else:
                creating_player = self.creating_player()
                if self.largest_team() > 1:
                    draft_order = ['\n__**Balanced Draft Order**__']
                    for draft in self.draft_order():
                        draft_order.append(f"__Side {draft['sidename'] if draft['sidename'] else draft['position']}__:  {draft['player'].name}")
                    draft_order_str = '\n'.join(draft_order)
                else:
                    draft_order_str = ''
                content_str = (f'This match is now full and **{creating_player.name}** should create the game in Polytopia and mark it as started using `{prefix}start {self.id} Name of Game`'
                        f'\nFriend codes can be copied easily with the command __`{prefix}codes {self.id}`__'
                        f'{draft_order_str}')
                status_str = 'Full - Waiting to start'

        embed.add_field(name='Status', value=status_str, inline=True)
        embed.add_field(name='Expires in', value=f'{expiration_str}', inline=True)
        embed.add_field(name='Notes', value=notes_str, inline=False)
        embed.add_field(name='\u200b', value='\u200b', inline=False)

        for side in self.ordered_side_list():
            side_name = ': **' + side.sidename + '**' if side.sidename else ''
            side_capacity = side.capacity()
            capacity += side_capacity[1]
            player_list = []
            ordered_player_list = list(side.ordered_player_list())
            for player_lineup in ordered_player_list:
                player = player_lineup.player
                players += 1
                tribe_str = player_lineup.tribe.emoji if player_lineup.tribe else ''
                team_str = player.team.emoji if player.team else ''
                poly_id_str = f'\n`{player.discord_member.polytopia_id}`' if len(ordered_player_list) < 10 else ''  # to avoid hidding 1024 char limit on very big sides
                logger.debug(f'Building embed for game {self.id} - player {player.id} {player.name} is associated with team {player.team} - team_str: {team_str}')
                player_list.append(f'**{player.name}** ({player.elo}) {tribe_str} {team_str}{poly_id_str}')
            player_str = '\u200b' if not player_list else '\n'.join(player_list)

            embed.add_field(name=f'__Side {side.position}__{side_name} *({side_capacity[0]}/{side_capacity[1]})*', value=player_str[:1024], inline=False)

        return embed, content_str

    def get_headline(self):
        # yields string like:
        # Game 481   :fried_shrimp: The Crawfish vs :fried_shrimp: TestAccount1 vs :spy: TestBoye1\n*Name of Game*
        gameside_strings = []
        for gameside in self.gamesides:
            # logger.info(f'{self.id} gameside:', gameside)
            emoji = ''
            if gameside.team and len(gameside.lineup) > 1:
                emoji = gameside.team.emoji

            gameside_strings.append(f'{emoji} **{gameside.name()}**')
        full_squad_string = ' *vs* '.join(gameside_strings)[:225]

        game_name = f'\n\u00a0*{self.name}*' if self.name and self.name.strip() else ''
        # \u00a0 is used as an invisible delimeter so game_name can be split out easily
        return f'Game {self.id}   {full_squad_string}{game_name}'

    def largest_team(self):
        return max(len(gameside.lineup) for gameside in self.gamesides)

    def smallest_team(self):
        return min(len(gameside.lineup) for gameside in self.gamesides)

    def size_string(self):

        gamesides = self.ordered_side_list()

        if self.is_pending:
            # use capacity for matchmaking strings
            if max(s.size for s in gamesides) == 1 and len(gamesides) > 2:
                return 'FFA'
            else:
                return 'v'.join(str(s.size) for s in gamesides)

        # this might be superfluous, combined Match and Game functions together
        if self.largest_team() == 1 and len(self.gamesides) > 2:
            return 'FFA'
        else:
            return 'v'.join(str(len(s.lineup)) for s in gamesides)

    def load_full_game(game_id: int):
        # Returns a single Game object with all related tables pre-fetched. or None

        game = Game.select().where(Game.id == game_id)
        subq = GameSide.select(GameSide, Team).join(Team, JOIN.LEFT_OUTER).join_from(GameSide, Squad, JOIN.LEFT_OUTER)

        subq2 = Lineup.select(
            Lineup, Tribe, TribeFlair, Player, DiscordMember).join(
            TribeFlair, JOIN.LEFT_OUTER).join(  # Need LEFT_OUTER_JOIN - default inner join would only return records that have a Tribe chosen
            Tribe, JOIN.LEFT_OUTER).join_from(
            Lineup, Player).join_from(Player, DiscordMember)

        res = prefetch(game, subq, subq2)

        if len(res) == 0:
            raise DoesNotExist()
        return res[0]

    def pregame_check(discord_groups, guild_id, require_teams: bool = False):
        # discord_groups = list of lists [[d1, d2, d3], [d4, d5, d6]]. each item being a discord.Member object
        # returns (ListOfLists1, List2)
        # ListOfLists1 represents one Server Team for each discord member in [[discord_groups]], or None if they arent in one
        # List2 represents Team that each discord_group will be playing for in a game, based on whether the groups are homogeneous and server settings
        # ie discord_groups input = [[nelluk, bakalol], [rickdaheals, jonathan]]
        # returns ([[Ronin, Ronin], [Jets, Jets]], [Ronin, Jets])

        logger.debug('entering pregame_check')
        list_of_detected_teams, list_of_final_teams, teams_for_each_discord_member = [], [], []
        intermingled_flag = False
        # False if all players on each side belong to the same server team, Ronin/Jets.True if players are mixed or on a server without teams

        for discord_group in discord_groups:
            same_team, list_of_teams = Player.get_teams_of_players(guild_id=guild_id, list_of_players=discord_group)
            teams_for_each_discord_member.append(list_of_teams)  # [[Team, Team][Team, Team]] for each team that a discord member is associated with, for Player.upsert()
            logger.debug(f'in pregame_check: discord_group {discord_group} matched to teams: {list_of_teams}')
            if None in list_of_teams:
                if require_teams is True:
                    raise exceptions.CheckFailedError('One or more players listed cannot be matched to a Team (based on Discord Roles). Make sure player has exactly one matching Team role.')
                else:
                    # Player(s) can't be matched to team, but server setting allows that.
                    intermingled_flag = True
                    logger.debug(f'setting intermingled_flag due to None in list_of_teams')
            if not same_team:
                # Mixed players within same side
                intermingled_flag = True
                logger.debug(f'setting intermingled_flag due to mixed teams in discord group')

            if not intermingled_flag:
                if list_of_teams[0] in list_of_detected_teams:
                    # Detected team already present (ie. Ronin players vs Ronin players)
                    intermingled_flag = True
                    logger.debug(f'setting intermingled_flag due to same team being present multiple times')
                else:
                    list_of_detected_teams.append(list_of_teams[0])

        if not intermingled_flag:
            # Use detected server teams for this game
            assert len(list_of_detected_teams) == len(discord_groups), 'Mismatched lists!'
            list_of_final_teams = list_of_detected_teams
        else:
            # Use Generic Teams
            if len(discord_groups) == 2:
                generic_teams = settings.generic_teams_short
            else:
                generic_teams = settings.generic_teams_long

            for count in range(len(discord_groups)):
                team_obj, created = Team.get_or_create(name=generic_teams[count][0], guild_id=guild_id,
                                                       defaults={'emoji': generic_teams[count][1], 'is_hidden': True})
                logger.debug(f'Fetching team object {team_obj.id} {team_obj.name}. Created? {created}')
                list_of_final_teams.append(team_obj)

        logger.debug(f'pregame_check returning {teams_for_each_discord_member} // {list_of_final_teams}')
        return (teams_for_each_discord_member, list_of_final_teams)

    def create_game(discord_groups, guild_id, name: str = None, require_teams: bool = False, is_ranked: bool = True):
        # discord_groups = list of lists [[d1, d2, d3], [d4, d5, d6]]. each item being a discord.Member object

        teams_for_each_discord_member, list_of_final_teams = Game.pregame_check(discord_groups, guild_id, require_teams)
        logger.debug(f'teams_for_each_discord_member: {teams_for_each_discord_member}\nlist_of_final_teams: {list_of_final_teams}')

        with db.atomic():
            newgame = Game.create(name=name,
                                  guild_id=guild_id,
                                  is_ranked=is_ranked)

            side_position = 1
            for team_group, allied_team, discord_group in zip(teams_for_each_discord_member, list_of_final_teams, discord_groups):
                logger.debug(f'Making side {side_position} for new game {newgame.id}: {team_group} - {allied_team} - {discord_group}')
                # team_group is each team that the individual discord.Member is associated with on the server, often None
                # allied_team is the team that this entire group is playing for in this game. Either a Server Team or Generic. Never None.

                player_group = []
                for team, discord_member in zip(team_group, discord_group):
                    # Upsert each discord.Member into a Player database object
                    player_group.append(
                        Player.upsert(discord_id=discord_member.id, discord_name=discord_member.name, discord_nick=discord_member.nick, guild_id=guild_id, team=team)[0]
                    )
                    logger.debug(f'Player {player_group[-1].id} {player_group[-1].name} added to side')

                # Create Squad records if 2+ players are allied
                if len(player_group) > 1:
                    squad = Squad.upsert(player_list=player_group, guild_id=guild_id)
                    logger.debug(f'Using squad {squad.id}')
                else:
                    squad = None

                gameside = GameSide.create(game=newgame, squad=squad, size=len(player_group), team=allied_team, position=side_position)
                side_position = side_position + 1

                # Create Lineup records
                for player in player_group:
                    Lineup.create(game=newgame, gameside=gameside, player=player)

        return newgame

    def reverse_elo_changes(self):
        for lineup in self.lineup:
            lineup.player.elo += lineup.elo_change_player * -1
            lineup.player.save()
            lineup.elo_change_player = 0
            lineup.elo_after_game = None
            if lineup.elo_change_discordmember:
                lineup.player.discord_member.elo += lineup.elo_change_discordmember * -1
                lineup.player.discord_member.save()
                lineup.elo_change_discordmember = 0
            lineup.save()

        for gameside in self.gamesides:
            if gameside.elo_change_squad and gameside.squad:
                gameside.squad.elo += (gameside.elo_change_squad * -1)
                gameside.squad.save()
                gameside.elo_change_squad = 0

            if gameside.elo_change_team and gameside.team:
                gameside.team.elo += (gameside.elo_change_team * -1)
                gameside.team.save()
                gameside.elo_change_team = 0

            if gameside.elo_change_team_alltime and gameside.team:
                gameside.team.elo_alltime += (gameside.elo_change_team_alltime * -1)
                gameside.team.save()
                gameside.elo_change_team_alltime = 0

            gameside.save()

    def delete_game(self):
        # resets any relevant ELO changes to players and teams, deletes related lineup records, and deletes the game entry itself

        logger.info(f'Deleting game {self.id}')
        recalculate = False
        with db.atomic():
            if self.winner:
                self.winner = None

                if self.is_confirmed and self.is_ranked:
                    recalculate = True
                    since = self.completed_ts

                    self.reverse_elo_changes()

                self.save()

            for lineup in self.lineup:
                lineup.delete_instance()

            for gameside in self.gamesides:
                gameside.delete_instance()

            self.delete_instance()

            if recalculate:
                Game.recalculate_elo_since(timestamp=since)

    def get_side_win_chances(largest_team: int, gameside_list, gameside_elo_list):
        n = len(gameside_list)

        # Adjust team elos when the amount of players on each team
        # is imbalanced, e.g. 1v2. It changes nothing when sizes are equal
        adjusted_side_elo, win_chance_list = [], []
        sum_raw_elo = sum(gameside_elo_list)
        for s, elo in zip(gameside_list, gameside_elo_list):
            missing_players = largest_team - len(s.lineup)
            avg_opponent_elos = int(round((sum_raw_elo - elo) / (n - 1)))
            adj_side_elo = s.adjusted_elo(missing_players, elo, avg_opponent_elos)
            adjusted_side_elo.append(adj_side_elo)

        # Compute proper win chances when there are more than 2 teams,
        # e.g. 2v2v2. It changes nothing when there are only 2 teams
        win_chance_unnorm = []
        normalization_factor = 0
        max_elo = max(adjusted_side_elo)
        second_elo = sorted(adjusted_side_elo)[-2]

        for own_elo, side in zip(adjusted_side_elo, gameside_list):
            target_elo = max_elo
            if (target_elo == own_elo):
                target_elo = second_elo
            win_chance = GameSide.calc_win_chance(own_elo, target_elo)
            win_chance_unnorm.append(win_chance)
            normalization_factor += win_chance

        # Apply the win/loss results for each team given their win% chance
        # for i in range(n):
        for side_win_chance_unnorm, adj_side_elo, side in zip(win_chance_unnorm, adjusted_side_elo, gameside_list):
            win_chance = round(side_win_chance_unnorm / normalization_factor, 3)
            win_chance_list.append(win_chance)

        return win_chance_list

    def declare_winner(self, winning_side: 'GameSide', confirm: bool):

        if winning_side.game != self:
            raise exceptions.CheckFailedError(f'GameSide id {winning_side.id} did not play in this game')

        smallest_side = self.smallest_team()

        if smallest_side <= 0:
            return logger.error(f'Cannot declare_winner for game {self.id}: Side with 0 players detected.')

        with db.atomic():
            if confirm is True:
                if not self.completed_ts:
                    self.completed_ts = datetime.datetime.now()  # will be preserved if ELO is re-calculated after initial win.

                self.is_confirmed = True
                if self.is_ranked:
                    # run elo calculations for player, discordmember, team, squad

                    largest_side = self.largest_team()
                    gamesides = list(self.gamesides)

                    side_elos = [s.average_elo() for s in gamesides]
                    side_elos_discord = [s.average_elo(by_discord_member=True) for s in gamesides]
                    team_elos = [s.team.elo if s.team else None for s in gamesides]
                    team_elos_alltime = [s.team.elo_alltime if s.team else None for s in gamesides]
                    squad_elos = [s.squad.elo if s.squad else None for s in gamesides]

                    side_win_chances = Game.get_side_win_chances(largest_side, gamesides, side_elos)
                    side_win_chances_discord = Game.get_side_win_chances(largest_side, gamesides, side_elos_discord)

                    if smallest_side > 1:
                        if None not in team_elos:
                            team_win_chances = Game.get_side_win_chances(largest_side, gamesides, team_elos)
                            team_win_chances_alltime = Game.get_side_win_chances(largest_side, gamesides, team_elos_alltime)
                        else:
                            team_win_chances, team_win_chances_alltime = None, None

                        if None not in squad_elos:
                            squad_win_chances = Game.get_side_win_chances(largest_side, gamesides, squad_elos)
                        else:
                            squad_win_chances = None
                    else:
                        team_win_chances, team_win_chances_alltime, squad_win_chances = None, None, None

                    team_elo_reset_date = datetime.datetime.strptime(settings.team_elo_reset_date, "%m/%d/%Y").date()
                    if self.date < team_elo_reset_date:
                        team_win_chances = None
                        logger.info(f'Game date {self.date} is before reset date of {team_elo_reset_date}. Will not count towards team ELO.')

                    for i in range(len(gamesides)):
                        side = gamesides[i]
                        is_winner = True if side == winning_side else False
                        for p in side.lineup:
                            p.change_elo_after_game(side_win_chances[i], is_winner)
                            p.change_elo_after_game(side_win_chances_discord[i], is_winner, by_discord_member=True)

                        if team_win_chances:
                            team_elo_delta = side.team.change_elo_after_game(team_win_chances[i], is_winner)
                            side.elo_change_team = team_elo_delta
                            side.team.elo = int(side.team.elo + team_elo_delta)
                            side.team.save()
                        if team_win_chances_alltime:
                            team_elo_delta = side.team.change_elo_after_game(team_win_chances_alltime[i], is_winner)
                            side.elo_change_team_alltime = team_elo_delta
                            side.team.elo_alltime = int(side.team.elo_alltime + team_elo_delta)
                            side.team.save()
                        if squad_win_chances:
                            side.elo_change_squad = side.squad.change_elo_after_game(squad_win_chances[i], is_winner)

                        side.save()

            self.winner = winning_side
            self.is_completed = True
            self.save()

    def has_player(self, player: Player = None, discord_id: int = None):
        # if player (or discord_id) was a participant in this game: return True, GameSide
        # else, return False, None
        if player:
            discord_id = player.discord_member.discord_id

        if not discord_id:
            return (False, None)

        for l in self.lineup:
            if l.player.discord_member.discord_id == int(discord_id):
                return (True, l.gameside)
        return (False, None)

    def player(self, player: Player = None, discord_id: int = None, name: str = None):
        # return game.lineup, based on either Player object or discord_id. else None

        if name:
            try:
                discord_id = int(str(name).strip('<>!@'))
            except ValueError:
                pass

        if player:
            discord_id = player.discord_member.discord_id

        if not discord_id and not name:
            return None

        for l in self.lineup:
            if discord_id:
                if l.player.discord_member.discord_id == int(discord_id):
                    return l
            else:
                if name and name.upper() in l.player.name.upper():
                    return l
        return None

    def capacity(self):
        return (len(self.lineup), sum(s.size for s in self.gamesides))

    def list_gameside_membership(self):
        sidenames = []
        gamesides = list(self.gamesides)
        for counter, s in enumerate(gamesides):
            if len(s.lineup) > 1:
                playernames = [l.player.name for l in s.lineup]
                sidenames.append(f'Side {counter + 1} **{s.name()}**: {", ".join(playernames)}')
            else:
                sidenames.append(f'Side {counter + 1}: **{s.name()}**')
        return sidenames

    def gameside_by_name(self, name: str):
        # Given a string representing a game side's name (team name for 2+ players, player name for 1 player)
        # Return a tuple of the participant and their gameside, ie Player, GameSide or Team, gameside

        if len(name) < 3:
            raise exceptions.CheckFailedError('Name given is not enough characters. Be more specific or use a @Mention.')

        matches = []
        gamesides = list(self.gamesides)

        for gameside in gamesides:
            if len(gameside.lineup) == 1:
                try:
                    p_id = int(name.strip('<>!@'))
                except ValueError:
                    pass
                else:
                    # name is a <@PlayerMention> or raw player_id
                    # compare to single squad player's discord ID
                    if p_id == gameside.lineup[0].player.discord_member.discord_id:
                        return (gameside.lineup[0].player, gameside)

                # Compare to single gamesides player's name
                if name.lower() in gameside.lineup[0].player.name.lower():
                    matches.append(
                        (gameside.lineup[0].player, gameside)
                    )
            else:
                # Compare to gamesidess team's name
                assert bool(gameside.team), 'GameSide obj has no team'
                if name.lower() in gameside.team.name.lower():
                    matches.append(
                        (gameside.team, gameside)
                    )

        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            names_str = '\n'.join(self.list_gameside_membership())
            raise exceptions.NoMatches(f'No sides found with name **{name}** in game {self.id}. Sides in this game are:\n{names_str}')
        else:
            raise exceptions.TooManyMatches(f'{len(matches)} matches found for "{name}" in game {self.id}.')

    def elo_requirements(self):

        min_elo, max_elo = 0, 3000
        min_elo_g, max_elo_g = 0, 3000
        notes = self.notes if self.notes else ''

        m = re.search(r'(\d+) elo max', notes, re.I)
        if m:
            max_elo = int(m[1])
        m = re.search(r'(\d+) elo min', notes, re.I)
        if m:
            min_elo = int(m[1])

        m = re.search(r'(\d+) global elo max', notes, re.I)
        if m:
            max_elo_g = int(m[1])
        m = re.search(r'(\d+) global elo min', notes, re.I)
        if m:
            min_elo_g = int(m[1])

        return (min_elo, max_elo, min_elo_g, max_elo_g)

    def waiting_for_creator(creator_discord_id: int):
        # Games for which creator_discord_id is in the 'creating player' slot (first player in GameSide.position == 1) and Game is full/waiting to start

        # subq = List of all lineup IDs for creating player for full pending games
        subq = GameSide.select(fn.MIN(Lineup.id).alias('game_creator')).join(Lineup).join_from(GameSide, Game).where(
            (GameSide.position == 1) & (Game.is_pending == 1) & (Game.id.not_in(Game.subq_open_games_with_capacity()))
        ).group_by(GameSide.game)

        q = Lineup.select(Lineup.game).join(Player).join(DiscordMember).where(
            (Lineup.player.discord_member.discord_id == creator_discord_id) & (Lineup.id.in_(subq))
        )

        return q

    def search_pending(status_filter: int = 0, ranked_filter: int = 2, guild_id: int = None, player_discord_id: int = None, host_discord_id: int = None):
        # status_filter
        # 0 = all open games
        # 1 = full games / waiting to start
        # 2 = games with capacity
        # ranked_filter
        # 0 = unranked (is_ranked == False)
        # 1 = ranked (is_ranked == True)
        # 2 = any

        ranked_filter = [0, 1] if ranked_filter == 2 else [ranked_filter]  # [0] or [1]

        if guild_id:
            guild_filter = Game.select(Game.id).where(Game.guild_id == guild_id)
        else:
            guild_filter = Game.select(Game.id)

        if player_discord_id:
            player_filter = Lineup.select(Game.id).join(Game).join_from(Lineup, Player).join(DiscordMember).where(
                (Lineup.player.discord_member.discord_id == player_discord_id) & (Game.is_pending == 1)
            )

        else:
            player_filter = Game.select(Game.id)

        if host_discord_id == 0:
            # Special case, pass 0 to find games where Game.host == None
            host_filter = Game.select(Game.id).where(
                (Game.host.is_null(True))
            )
        elif host_discord_id:
            host_filter = Game.select(Game.id).join(Player).join(DiscordMember).where(
                (Lineup.player.discord_member.discord_id == host_discord_id)
            )
        else:
            # Pass None to not filter by Game.host
            host_filter = Game.select(Game.id)

        if status_filter == 1:
            # full games / waiting to start
            q = Game.select().where(
                (Game.id.not_in(Game.subq_open_games_with_capacity())) &
                (Game.is_pending == 1) &
                (Game.id.in_(guild_filter)) &
                (Game.id.in_(player_filter)) &
                (Game.id.in_(host_filter)) &
                (Game.is_ranked.in_(ranked_filter))
            )
            return q.prefetch(GameSide, Lineup, Player)

        elif status_filter == 2:
            # games with open capacity
            return Game.select().where(
                (Game.id.in_(Game.subq_open_games_with_capacity())) &
                (Game.is_pending == 1) &
                (Game.id.in_(guild_filter)) &
                (Game.id.in_(player_filter)) &
                (Game.id.in_(host_filter)) &
                (Game.is_ranked.in_(ranked_filter))
            ).order_by(-Game.id).prefetch(GameSide, Lineup, Player)

        else:
            # Any kind of open game
            # sorts by capacity-player_count, so full games are at bottom of list
            return Game.select(
                Game, fn.SUM(GameSide.size).alias('player_capacity'), fn.COUNT(Lineup.id).alias('player_count'),
            ).join(GameSide, on=(GameSide.game == Game.id)).join(Lineup, JOIN.LEFT_OUTER).where(
                (Game.is_pending == 1) &
                (Game.id.in_(guild_filter)) &
                (Game.id.in_(player_filter)) &
                (Game.id.in_(host_filter)) &
                (Game.is_ranked.in_(ranked_filter))
            ).group_by(Game.id).order_by(
                -(fn.SUM(GameSide.size) - fn.COUNT(Lineup.id))
            ).prefetch(GameSide, Lineup, Player)

    def search(player_filter=None, team_filter=None, title_filter=None, status_filter: int = 0, guild_id: int = None):
        # Returns Games by almost any combination of player/team participation, and game status
        # player_filter/team_filter should be a [List, of, Player/Team, objects] (or ID #s)
        # status_filter:
        # 0 = all games, 1 = completed games, 2 = incomplete games
        # 3 = wins, 4 = losses (only for first player in player_list or, if empty, first team in team list)
        # 5 = unconfirmed wins

        confirmed_filter, completed_filter, pending_filter = [0, 1], [0, 1], [0, 1]

        if status_filter == 1:
            # completed games
            completed_filter, pending_filter = [1], [0]
        elif status_filter == 2:
            # incomplete games
            confirmed_filter = [0]
        elif status_filter == 3 or status_filter == 4:
            # wins/losses
            completed_filter, confirmed_filter, pending_filter = [1], [1], [0]
        elif status_filter == 5:
            # Unconfirmed completed games
            completed_filter, confirmed_filter, pending_filter = [1], [0], [0]

        if guild_id:
            guild_filter = Game.select(Game.id).where(Game.guild_id == guild_id)
        else:
            guild_filter = Game.select(Game.id)

        if team_filter:
            team_subq = GameSide.select(GameSide.game).join(Game).where(
                (GameSide.team.in_(team_filter)) & (GameSide.size > 1)
            ).group_by(GameSide.game).having(
                (fn.COUNT(GameSide.team) == len(team_filter))
            )
        else:
            team_subq = Game.select(Game.id)

        if player_filter:
            player_subq = Lineup.select(Lineup.game).join(Game).where(
                (Lineup.player.in_(player_filter))
            ).group_by(Lineup.game).having(
                fn.COUNT(Lineup.player) == len(player_filter)
            )
        else:
            player_subq = Game.select(Game.id)

        if title_filter:
            title_subq = Game.select(Game.id).where(
                (Game.name.contains('%'.join(title_filter))) | (Game.notes.contains('%'.join(title_filter)))
            )
        else:
            title_subq = Game.select(Game.id)

        if (not player_filter and not team_filter) or status_filter not in [3, 4]:
            # No filtering on wins/losses
            victory_subq = Game.select(Game.id)
        else:
            if player_filter:
                # Filter wins/losses on first entry in player_filter
                if status_filter == 3:
                    # Games that player has won
                    victory_subq = Lineup.select(Lineup.game).join(Game).join_from(Lineup, GameSide).where(
                        (Lineup.game.is_completed == 1) & (Lineup.player == player_filter[0]) & (Game.winner == Lineup.gameside.id)
                    )
                elif status_filter == 4:
                    # Games that player has lost
                    victory_subq = Lineup.select(Lineup.game).join(Game).join_from(Lineup, GameSide).where(
                        (Lineup.game.is_completed == 1) & (Lineup.player == player_filter[0]) & (Game.winner != Lineup.gameside.id)
                    )
            else:
                # Filter wins/losses on first entry in team_filter
                if status_filter == 3:
                    # Games that team has won
                    victory_subq = GameSide.select(GameSide.game).join(Game).where(
                        (GameSide.team == team_filter[0]) & (GameSide.id == Game.winner)
                    )
                elif status_filter == 4:
                    # Games that team has lost
                    victory_subq = GameSide.select(GameSide.game).join(Game).where(
                        (GameSide.team == team_filter[0]) & (GameSide.id != Game.winner)
                    )

        game = Game.select().where(
            (
                Game.id.in_(team_subq)
            ) & (
                Game.id.in_(player_subq)
            ) & (
                Game.id.in_(title_subq)
            ) & (
                Game.is_completed.in_(completed_filter)
            ) & (
                Game.is_confirmed.in_(confirmed_filter)
            ) & (
                Game.id.in_(victory_subq)
            ) & (
                Game.id.in_(guild_filter)
            ) & (
                Game.is_pending.in_(pending_filter))
        ).order_by(-Game.completed_ts, -Game.date)

        return game

    def series_record(self):

        gamesides = tuple(self.gamesides)
        if len(gamesides) != 2:
            raise exceptions.CheckFailedError('This can only be used for games with exactly two sides.')

        player_lists = []
        for side in gamesides:
            player_lists.append([lineup.player for lineup in side.lineup])

        games_with_same_teams = Game.by_opponents(player_lists)

        s1_wins, s2_wins = 0, 0
        for game in games_with_same_teams:
            if not game.is_ranked:
                continue
            if game.is_confirmed:
                if game.winner.has_same_players_as(gamesides[0]):
                    s1_wins += 1
                    logger.debug(f'series_record(): s1_wins incremented for game {game.id}')
                else:
                    s2_wins += 1
                    logger.debug(f'series_record(): s2_wins incremented for game {game.id}')

        logger.debug(f'series_record(): game {self.id}, side 0, id {gamesides[0].id}, wins {s1_wins}. side 1, id {gamesides[1].id}, wins {s2_wins}')
        if s2_wins > s1_wins:
            return ((gamesides[1], s2_wins), (gamesides[0], s1_wins))
        return ((gamesides[0], s1_wins), (gamesides[1], s2_wins))

    def by_opponents(player_lists):
        # Given lists of player objects representing game sides, ie:
        # [[p1, p2], [p3, [p4], [p5, p6]] for a 2v2v2 game
        # return all games that have that same exact format and sides. ie return all Nelluk vs Rickdaheals games

        if len(player_lists) < 2:
            raise exceptions.CheckFailedError('At least two sides must be queried, ie: [[p1, p2], [p3, p4]]')

        logger.debug(f'by_opponents() with player_lists = {player_lists}')
        side_games = []
        for player_list in player_lists:
            # for this given player_list, find all games that had this exact list on one side

            query = GameSide.select(GameSide.game).join(Lineup).group_by(GameSide.game, GameSide.id).having(
                (fn.SUM(Lineup.player.in_(player_list).cast('integer')) == len(player_list)) & (fn.SUM(Lineup.player.not_in(player_list).cast('integer')) == 0)
            )
            side_games.append(set((res[0] for res in query.tuples())))
            # side_games will be a list of sets, each set being game_ids for other games with this specific gamesides

        intersection_of_games = set.intersection(*side_games)
        # this becomes a set of game IDs where -all- the game sides participated

        subq_games_with_same_number_of_sides = GameSide.select(GameSide.game).group_by(GameSide.game).having(fn.COUNT('*') == len(player_lists))

        query = Game.select().where(
            (Game.id.in_(intersection_of_games)) & (Game.id.in_(subq_games_with_same_number_of_sides)) & (Game.is_pending == 0)
        )

        return query

        # Commented out block negated by subq_games_with_same_number_of_sides - which needs to be more thoroughly vetted
        # games_with_same_number_of_sides = []
        # for game in query:
        #     # ugly fix - without this block games without the same number of sides will be mixed together.
        #     # if game 1 is p1 vs p2, and game 2 is p1 vs p2 vs p3, they would be compared
        #     # could fix this with better SQL querying but this was a quick fix
        #     if len(game.gamesides) == len(player_lists):
        #         games_with_same_number_of_sides.append(game)

        # return games_with_same_number_of_sides

    def recalculate_elo_since(timestamp):
        games = Game.select().where(
            (Game.is_completed == 1) & (Game.is_confirmed == 1) & (Game.completed_ts >= timestamp) & (Game.winner.is_null(False)) & (Game.is_ranked == 1)
        ).order_by(Game.completed_ts).prefetch(GameSide, Lineup)

        elo_logger.debug(f'recalculate_elo_since {timestamp}')
        for g in games:
            g.reverse_elo_changes()
            g.is_completed = 0  # To have correct completed game counts for new ELO calculations
            g.save()

        for g in games:
            full_game = Game.load_full_game(game_id=g.id)
            full_game.declare_winner(winning_side=full_game.winner, confirm=True)
        elo_logger.debug(f'recalculate_elo_since complete')

    def recalculate_all_elo():
        # Reset all ELOs to 1000, reset completed game counts, and re-run Game.declare_winner() on all qualifying games

        logger.warn('Resetting and recalculating all ELO')
        elo_logger.info(f'recalculate_all_elo')

        with db.atomic():
            Player.update(elo=1000, elo_max=1000).execute()
            Team.update(elo=1000, elo_alltime=1000).execute()
            DiscordMember.update(elo=1000, elo_max=1000).execute()
            Squad.update(elo=1000).execute()

            Game.update(is_completed=0).where(
                (Game.is_confirmed == 1) & (Game.winner.is_null(False)) & (Game.is_ranked == 1)
            ).execute()  # Resets completed game counts for players/squads/team ELO bonuses

            games = Game.select().where(
                (Game.is_completed == 0) & (Game.is_confirmed == 1) & (Game.winner.is_null(False)) & (Game.is_ranked == 1)
            ).order_by(Game.completed_ts)

            for game in games:
                full_game = Game.load_full_game(game_id=game.id)
                full_game.declare_winner(winning_side=full_game.winner, confirm=True)

        elo_logger.info(f'recalculate_all_elo complete')

    def first_open_side(self, roles):

        # first check to see if there are any sides with a role ID that the user has (ie Team Ronin role ID)
        role_locked_sides = GameSide.select().where(
            (GameSide.game == self) & (GameSide.required_role_id.in_(roles))
        ).order_by(GameSide.position).prefetch(Lineup)

        for side in role_locked_sides:
            if len(side.lineup) < side.size:
                return side

        # else just use the first side with no role requirement
        sides = GameSide.select().where(
            (GameSide.game == self) & (GameSide.required_role_id.is_null(True))
        ).order_by(GameSide.position).prefetch(Lineup)

        for side in sides:
            if len(side.lineup) < side.size:
                return side
        return None

    def get_side(self, lookup):
        # lookup can be a side number/position (integer) or side name
        # returns (GameSide, bool) where bool==True if side has space to add a player
        try:
            side_num = int(lookup)
            side_name = None
        except ValueError:
            side_num = None
            side_name = lookup

        for side in self.gamesides:
            if side_num and side.position == side_num:
                return (side, bool(len(side.lineup) < side.size))
            if side_name and side.sidename and len(side_name) > 2 and side_name.upper() in side.sidename.upper():
                return (side, bool(len(side.lineup) < side.size))

        return None, False

    def subq_open_games_with_capacity(guild_id: int = None):
        # All games that have open capacity
        # not restricted by expiration

        # Subq: MatchSides with openings
        subq = GameSide.select(GameSide.id).join(Lineup, JOIN.LEFT_OUTER).group_by(GameSide.id, GameSide.size).having(
            fn.COUNT(Lineup.id) < GameSide.size)

        if guild_id:
            q = GameSide.select(GameSide.game).join(Game).where(
                (GameSide.id.in_(subq)) & (GameSide.game.guild_id == guild_id) & (GameSide.game.is_pending == 1)
            ).group_by(GameSide.game).order_by(GameSide.game)

        else:
            q = GameSide.select(GameSide.game).join(Game).where(
                (GameSide.id.in_(subq)) & (GameSide.game.is_pending == 1)
            ).group_by(GameSide.game).order_by(GameSide.game)

        return q

    def purge_expired_games():

        # Full matches that expired more than 3 days ago (ie. host has 3 days to start match before it vanishes)
        purge_deadline = (datetime.datetime.now() + datetime.timedelta(days=-3))

        delete_query = Game.delete().where(
            (Game.expiration < purge_deadline) & (Game.is_pending == 1)
        )

        # Expired matches that never became full
        delete_query2 = Game.delete().where(
            (Game.expiration < datetime.datetime.now()) & (Game.id.in_(Game.subq_open_games_with_capacity())) & (Game.is_pending == 1)
        )

        logger.info(f'purge_expired_games #1: Purged {delete_query.execute()}  games.')
        logger.info(f'purge_expired_games #2: Purged {delete_query2.execute()}  games.')

    def confirmations_reset(self):
        for side in self.gamesides:
            side.win_confirmed = False
            side.save()
        self.win_claimed_ts = None
        self.save()

    def confirmations_count(self):
        fully_confirmed = True
        confirmed_count, side_count = 0, 0
        for side in self.gamesides:
            side_count = side_count + 1
            if side.win_confirmed:
                confirmed_count = confirmed_count + 1
            else:
                fully_confirmed = False

        return (confirmed_count, side_count, fully_confirmed)


class Squad(BaseModel):
    elo = SmallIntegerField(default=1000)
    guild_id = BitField(unique=False, null=False)

    def upsert(player_list, guild_id: int):

        squads = Squad.get_matching_squad(player_list)

        if len(squads) == 0:
            # Insert new squad based on this combination of players
            sq = Squad.create(guild_id=guild_id)
            for p in player_list:
                SquadMember.create(player=p, squad=sq)
            return sq

        return squads[0]

    def completed_game_count(self):

        num_games = GameSide.select().join(Game).where(
            (Game.is_completed == 1) & (GameSide.squad == self) & (Game.is_ranked == 1)
        ).count()

        return num_games

    def change_elo_after_game(self, chance_of_winning: float, is_winner: bool):
        if self.completed_game_count() < 6:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        if is_winner is True:
            elo_delta = int(round((max_elo_delta * (1 - chance_of_winning)), 0))
        else:
            elo_delta = int(round((max_elo_delta * (0 - chance_of_winning)), 0))

        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta

    def subq_squads_by_size(min_size: int=2, exact=False):

        if exact:
            # Squads with exactly min_size number of members
            return SquadMember.select(SquadMember.squad).group_by(
                SquadMember.squad
            ).having(fn.COUNT('*') == min_size)

        # Squads with at least min_size number of members
        return SquadMember.select(SquadMember.squad).group_by(
            SquadMember.squad
        ).having(fn.COUNT('*') >= min_size)

    def subq_squads_with_completed_games(min_games: int=1):
        # Defaults to squads who have completed more than 0 games

        if min_games <= 0:
            # Squads who at least have one in progress game
            return GameSide.select(GameSide.squad).join(Game).where(Game.is_pending == 0).group_by(
                GameSide.squad
            ).having(fn.COUNT('*') >= min_games)

        return GameSide.select(GameSide.squad).join(Game).where(Game.is_completed == 1).group_by(
            GameSide.squad
        ).having(fn.COUNT('*') >= min_games)

    def leaderboard_rank(self, date_cutoff):

        query = Squad.leaderboard(date_cutoff=date_cutoff, guild_id=self.guild_id)

        squad_found = False
        for counter, s in enumerate(query.tuples()):
            if s[0] == self.id:
                squad_found = True
                break

        rank = counter + 1 if squad_found else None
        return (rank, query.count())

    def leaderboard(date_cutoff, guild_id: int):

        num_squads = Squad.select().where(Squad.guild_id == guild_id).count()
        if num_squads < 15:
            min_games = 0
        elif num_squads < 25:
            min_games = 1
        else:
            min_games = 2

        q = Squad.select().join(GameSide).join(Game).where(
            (
                Squad.id.in_(Squad.subq_squads_with_completed_games(min_games=min_games))
            ) & (Squad.guild_id == guild_id) & (Game.date > date_cutoff)
        ).order_by(-Squad.elo).group_by(Squad)

        return q

    def get_matching_squad(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns squad with exactly the same participating players. See https://stackoverflow.com/q/52010522/1281743
        query = Squad.select().join(SquadMember).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list)) & (fn.SUM(SquadMember.player.not_in(player_list).cast('integer')) == 0)
        )

        return query

    def get_all_matching_squads(player_list, guild_id: int):
        # Takes [List, of, Player, Records] (not names)
        # Returns all squads containing players in player list. Used to look up a squad by partial or complete membership

        # Limited to squads with at least 2 members and at least min_games completed game
        num_squads = Squad.select().where(Squad.guild_id == guild_id).count()
        if num_squads < 15:
            min_games = 0
        elif num_squads < 25:
            min_games = 1
        else:
            min_games = 2

        squad_with_matching_members = Squad.select().join(SquadMember).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list))
        )

        query = GameSide.select(GameSide.squad, fn.COUNT('*').alias('games_played')).where(
            (GameSide.squad.in_(Squad.subq_squads_by_size(min_size=2))) &
            (GameSide.squad.in_(Squad.subq_squads_with_completed_games(min_games=min_games))) &
            (GameSide.squad.in_(squad_with_matching_members))
        ).group_by(GameSide.squad).order_by(-SQL('games_played'))

        return query

    def get_record(self):

        wins = GameSide.select(GameSide.id).join(Game).where(
            (Game.is_completed == 1) & (Game.is_confirmed == 1) & (Game.is_ranked == 1) & (GameSide.squad == self) & (GameSide.id == Game.winner)
        ).count()

        losses = GameSide.select(GameSide.id).join(Game).where(
            (Game.is_completed == 1) & (Game.is_confirmed == 1) & (Game.is_ranked == 1) & (GameSide.squad == self) & (GameSide.id != Game.winner)
        ).count()

        return (wins, losses)

    def get_members(self):
        members = [member.player for member in self.squadmembers]
        return members

    def get_names(self):
        member_names = [member.player.name for member in self.squadmembers]
        return member_names


class SquadMember(BaseModel):
    player = ForeignKeyField(Player, null=False, on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadmembers', on_delete='CASCADE')


class GameSide(BaseModel):
    game = ForeignKeyField(Game, null=False, backref='gamesides', on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=True, backref='gamesides', on_delete='CASCADE')
    team = ForeignKeyField(Team, null=True, backref='gamesides', on_delete='RESTRICT')
    required_role_id = BitField(default=None, null=True)
    elo_change_squad = SmallIntegerField(default=0)
    elo_change_team = SmallIntegerField(default=0)
    elo_change_team_alltime = SmallIntegerField(default=0)
    team_chan = BitField(default=None, null=True)
    sidename = TextField(null=True)  # for pending open games/matchmaking
    size = SmallIntegerField(null=False, default=1)
    position = SmallIntegerField(null=False, unique=False, default=1)
    win_confirmed = BooleanField(default=False)
    team_chan_external_server = BitField(unique=False, null=True, default=None)

    def has_same_players_as(self, gameside):
        # Given side1.has_same_players_as(side2)
        # Return True if both players have exactly the same players in their lineup

        s1_players = [l.player for l in self.lineup]
        s2_players = [l.player for l in gameside.lineup]

        if len(s1_players) != len(s2_players):
            return False

        return all(p in s2_players for p in s1_players)

    def calc_win_chance(my_side_elo: int, opponent_elo: int):
        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - my_side_elo) / 400.0))), 3)
        return chance_of_winning

    def elo_strings(self):
        # Returns a tuple of strings for team ELO and squad ELO display. ie:
        # ('1200 +30', '1300')

        if self.team:
            team_elo_str = str(self.elo_change_team) if self.elo_change_team != 0 else ''
            if self.elo_change_team > 0:
                team_elo_str = '+' + team_elo_str
            team_elo_str = f'({self.team.elo} {team_elo_str})'
        else:
            team_elo_str = None

        if self.squad:
            squad_elo_str = str(self.elo_change_squad) if self.elo_change_squad != 0 else ''
            if self.elo_change_squad > 0:
                squad_elo_str = '+' + squad_elo_str
            if squad_elo_str:
                squad_elo_str = '(' + squad_elo_str + ')'

            squad_elo_str = f'{self.squad.elo} {squad_elo_str}'
        else:
            squad_elo_str = None

        return (team_elo_str, squad_elo_str)

    def average_elo(self, by_discord_member: bool = False):
        if by_discord_member is True:
            elo_list = [l.player.discord_member.elo for l in self.lineup]
        else:
            elo_list = [l.player.elo for l in self.lineup]

        return int(round(sum(elo_list) / len(elo_list)))

    def adjusted_elo(self, missing_players: int, own_elo: int, opponent_elos: int):
        # If teams have imbalanced size, adjust win% based on a
        # function of the team's elos involved, e.g.
        # 1v2  [1400] vs [1100, 1100] adjusts to represent 50% win
        # (compared to 58.8% for 1v1v1 for the 1400 player)
        handicap = 200  # the elo difference for a 50% 1v2 chance
        handicap_elo = handicap * 2 + max(own_elo - opponent_elos - handicap, 0)
        size = len(self.lineup)

        # "fill up" missing players with placeholder handicapped elos
        missing_player_elo = own_elo - handicap_elo
        return int(round((own_elo * size + missing_player_elo * missing_players) / (size + missing_players)))

    def name(self):

        side_players = len(self.lineup)
        if side_players == 0 and self.size == 1:
            # Display ________ for an empty 1-player side
            return '_____\u200b________\u200b_____'
        elif side_players == 1 and self.size == 1:
            # 1-player side, show player name
            if len(self.game.lineup) > 10:
                return self.lineup[0].player.discord_member.name[:10]
            elif len(self.game.lineup) > 6:
                return self.lineup[0].player.discord_member.name[:20]
            else:
                return self.lineup[0].player.name[:30]
        else:
            # Team game
            if self.team:
                return self.team.name
            elif self.sidename:
                return self.sidename
            else:
                return 'Unknown Team'
            # return self.team.name if self.team else 'Unknown Team'

    def roster(self):
        # Returns list of tuples [(player, elo string (1000 +50), :tribe_emoji:)]
        players = []

        is_confirmed = self.game.is_confirmed
        # for l in self.lineup:
        for l in Lineup.select(Lineup, Player).join(Player).where(Lineup.gameside == self).order_by(Lineup.id):

            if is_confirmed and l.elo_after_game:
                # build elo string showing change in elo from this game
                elo_change_str = f'+{l.elo_change_player}' if l.elo_change_player >= 0 else str(l.elo_change_player)
                elo_str = f'{l.elo_after_game} {elo_change_str}'
            else:
                # build elo string showing current elo only
                elo_str = f'{l.player.elo}'
            players.append(
                (l.player, f'{elo_str}', l.emoji_str())
            )

        return players

    def capacity(self):
        return (len(self.lineup), self.size)

    def ordered_player_list(self):
        player_list = []
        q = Lineup.select(Lineup, Player).join(Player).where(Lineup.gameside == self).order_by(Lineup.id)
        for l in q:
            # player_list.append(l.player)
            player_list.append(l)

        return player_list


class Lineup(BaseModel):
    tribe = ForeignKeyField(TribeFlair, null=True, on_delete='SET NULL')
    game = ForeignKeyField(Game, null=False, backref='lineup', on_delete='CASCADE')
    gameside = ForeignKeyField(GameSide, null=False, backref='lineup', on_delete='CASCADE')
    player = ForeignKeyField(Player, null=False, backref='lineup', on_delete='RESTRICT')
    elo_change_player = SmallIntegerField(default=0)
    elo_change_discordmember = SmallIntegerField(default=0)
    elo_after_game = SmallIntegerField(default=None, null=True)  # snapshot of what elo was after game concluded

    def change_elo_after_game(self, chance_of_winning: float, is_winner: bool, by_discord_member: bool = False):
        # Average(Away Side Elo) is compared to Average(Home_Side_Elo) for calculation - ie all members on a side will have the same elo_delta
        # Team A: p1 900 elo, p2 1000 elo = 950 average
        # Team B: p1 1000 elo, p2 1200 elo = 1100 average
        # ELO is compared 950 vs 1100 and all players treated equally

        if by_discord_member is True:
            if not settings.guild_setting(self.game.guild_id, 'include_in_global_lb'):
                logger.info(f'Skipping ELO change by discord member because {self.game.guild_id} is set to be excluded.')
                return
            num_games = self.player.discord_member.completed_game_count()
            elo = self.player.discord_member.elo
        else:
            num_games = self.player.completed_game_count()
            elo = self.player.elo

        max_elo_delta = 32

        if num_games < 6:
            max_elo_delta = 75
        elif num_games < 11:
            max_elo_delta = 50

        if is_winner is True:
            elo_delta = int(round((max_elo_delta * (1 - chance_of_winning)), 0))
        else:
            elo_delta = int(round((max_elo_delta * (0 - chance_of_winning)), 0))

        elo_boost = .60 * ((1200 - max(min(elo, 1200), 900)) / 300)  # 60% boost to delta at elo 900, gradually shifts to 0% boost at 1200 ELO
        # elo_boost = 1.0 * ((1200 - min(elo, 1200)) / 200)  # 100% boost to delta at elo 1000, gradually shifts to 0% boost at 1200 ELO
        # elo_boost = 0.95 * ((1200 - min(elo, 1200)) / 200)

        elo_bonus = int(abs(elo_delta) * elo_boost)
        elo_delta += elo_bonus

        elo_logger.debug(f'game: {self.game.id}, {"discordmember" if by_discord_member else "player"}, {self.player.name[:15]}, '
            f'elo: {elo}, CoW: {chance_of_winning}, elo_delta: {elo_delta}, new_elo: {int(elo + elo_delta)}')

        # logger.debug(f'Player {self.player.id} chance of winning: {chance_of_winning} game {self.game.id},'
        #     f'elo_delta {elo_delta}, current_player_elo {self.player.elo}, new_player_elo {int(self.player.elo + elo_delta)}')

        with db.atomic():
            if by_discord_member is True:
                self.player.discord_member.elo = int(elo + elo_delta)
                if self.player.discord_member.elo > self.player.discord_member.elo_max:
                    self.player.discord_member.elo_max = self.player.discord_member.elo
                self.elo_change_discordmember = elo_delta
                self.player.discord_member.save()
                self.save()
            else:
                self.player.elo = int(elo + elo_delta)
                self.elo_after_game = int(elo + elo_delta)
                if self.player.elo > self.player.elo_max:
                    self.player.elo_max = self.player.elo
                self.elo_change_player = elo_delta
                self.player.save()
                self.save()

        # logger.debug(f'elo after save: {self.player.elo}')

    def emoji_str(self):

        if self.tribe and self.tribe.emoji:
            return self.tribe.emoji
        else:
            return ''


with db:
    db.create_tables([Team, DiscordMember, Game, Player, Tribe, Squad, GameSide, SquadMember, Lineup, TribeFlair])
    # Only creates missing tables so should be safe to run each time
    try:
        # Creates deferred FK http://docs.peewee-orm.com/en/latest/peewee/models.html#circular-foreign-key-dependencies
        Game._schema.create_foreign_key(Game.winner)
    except (ProgrammingError, DuplicateObject):
        pass
        # Will throw one of above exceptions if foreign key already exists - exception depends on which version of psycopg2 is running
