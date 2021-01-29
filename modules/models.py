import datetime
import discord
from discord.ext import commands
import re
# import psycopg2
from psycopg2.errors import DuplicateObject
from peewee import *
from playhouse.postgres_ext import *
import modules.exceptions as exceptions
# from modules import utilities
# import modules.utilities as utilities
from modules import channels
import statistics
import settings
import logging

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')

db = PostgresqlExtDatabase(settings.psql_db, autorollback=True, user=settings.psql_user, autoconnect=False, password='password')


def tomorrow():
    return (datetime.datetime.now() + datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")


def is_post_moonrise():
    return bool(datetime.datetime.today().date() >= settings.moonrise_reset_date)


def moonrise_or_air_date_range(version: str = None):
    # given todays date, return pre-moonrise dates (epoch thru moonrise) or post-moonrise dates (moonrise thru end of time)
    # returns (beginning_datetime, end_datetime)
    moonrise = (settings.moonrise_reset_date, datetime.date.max)
    air = (datetime.date.min, settings.moonrise_reset_date - datetime.timedelta(days=1))

    if version and version.upper() not in ['AIR', 'MOONRISE', 'ALLTIME']:
        raise ValueError('Valid arguments are "air", "moonrise", or "alltime". Leave as None to use the current verson based on today\'s date.')

    if version and version.upper() == 'AIR':
        return air
    elif version and version.upper() == 'MOONRISE':
        return moonrise
    elif version and version.upper() == 'ALLTIME':
        return (datetime.date.min, datetime.date.max)
    elif is_post_moonrise():
        return moonrise
    else:
        return air


def string_to_user_id(input):
    # copied from Utilities - probably a better way to structure this but currently importing utilities creates circular import

    # given a user @Mention or a raw user ID, returns just the raw user ID (does not validate the ID itself, but does sanity check length)
    match = re.match(r'([0-9]{15,21})$', input) or re.match(r'<@!?([0-9]+)>$', input)
    # regexp is from https://github.com/Rapptz/discord.py/blob/02397306b2ed76b3bc42b2b28e8672e839bdeaf5/discord/ext/commands/converter.py#L117

    try:
        return int(match.group(1))
    except (ValueError, AttributeError):
        return None


def is_registered_member():
    async def predicate(ctx):
        db.connect(reuse_if_open=True)
        member_match = DiscordMember.select(DiscordMember.discord_id).where(
            (DiscordMember.discord_id == ctx.author.id)
        ).count()

        if member_match:
            return True
        if ctx.invoked_with == 'help' and ctx.command.name != 'help':
            return False
        else:
            await ctx.send(f'This command requires bot registration first. Type __`{ctx.prefix}setname Your Mobile Name`__ or  __`{ctx.prefix}steamname Your Steam Username`__ to get started.')
        return False
    return commands.check(predicate)


class BaseModel(Model):
    class Meta:
        database = db
        legacy_table_names = True


class Configuration(BaseModel):
    def draft_config_defaults():
        return {'announcement_message': None, 'announcement_channel': None,
                'draft_open': False, 'date_opened': None, 'added_message': ''}

    polychamps_draft = BinaryJSONField(null=True, default=draft_config_defaults())
    guild_id = BitField(unique=True, null=False)


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

    def get_by_name(team_name: str, guild_id: int, require_exact: bool = False):
        if require_exact:
            teams = Team.select().where((Team.name == team_name) & (Team.guild_id == guild_id) & (Team.is_hidden == 0))
        else:
            teams = Team.select().where((Team.name.contains(team_name)) & (Team.guild_id == guild_id) & (Team.is_hidden == 0))
        return teams

    def get_or_except(team_name: str, guild_id: int, require_exact: bool = False):
        results = Team.get_by_name(team_name=team_name, guild_id=guild_id, require_exact=require_exact)
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

        max_elo_delta = 32

        if is_winner is True:
            elo_delta = int(round((max_elo_delta * (1 - chance_of_winning)), 0))
        else:
            elo_delta = int(round((max_elo_delta * (0 - chance_of_winning)), 0))

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

    def get_season_record(self, season=None):

        if self.guild_id != settings.server_ids['polychampions'] or self.is_hidden:
            return ()

        full_season_games, regular_season_games, post_season_games = Game.polychamps_season_games(league='all', season=season)

        losses = Game.search(status_filter=4, team_filter=[self])
        wins = Game.search(status_filter=3, team_filter=[self])
        incomplete = Game.search(status_filter=2, team_filter=[self])

        win_count_reg = Game.select(Game.id).where(Game.id.in_(wins) & Game.id.in_(regular_season_games)).count()
        loss_count_reg = Game.select(Game.id).where(Game.id.in_(losses) & Game.id.in_(regular_season_games)).count()
        incomplete_count_reg = Game.select(Game.id).where(Game.id.in_(incomplete) & Game.id.in_(regular_season_games)).count()

        win_count_post = Game.select(Game.id).where(Game.id.in_(wins) & Game.id.in_(post_season_games)).count()
        loss_count_post = Game.select(Game.id).where(Game.id.in_(losses) & Game.id.in_(post_season_games)).count()
        incomplete_count_post = Game.select(Game.id).where(Game.id.in_(incomplete) & Game.id.in_(post_season_games)).count()

        return (win_count_reg, loss_count_reg, incomplete_count_reg, win_count_post, loss_count_post, incomplete_count_post)

    def related_external_severs(guild_id: int):
        # return a list of external server IDs from a given guild_id
        # basically used to list all PolyChampions league server IDs

        query = Team.select().where(
            (Team.guild_id == guild_id) & (Team.external_server > 0)
        )
        return list(set([team.external_server for team in query]))


class DiscordMember(BaseModel):
    discord_id = BitField(unique=True, null=False)
    name = TextField(unique=False)
    name_steam = TextField(null=True)
    elo = SmallIntegerField(default=1000)
    elo_max = SmallIntegerField(default=1000)
    elo_alltime = SmallIntegerField(default=1000)
    elo_max_alltime = SmallIntegerField(default=1000)
    elo_moonrise = SmallIntegerField(default=1000)
    elo_max_moonrise = SmallIntegerField(default=1000)
    polytopia_id = TextField(null=True)
    polytopia_name = TextField(null=True)
    is_banned = BooleanField(default=False)
    timezone_offset = SmallIntegerField(default=None, null=True)
    date_polychamps_invite_sent = DateField(default=None, null=True)
    boost_level = SmallIntegerField(default=None, null=True)

    def mention(self):
        return f'<@{self.discord_id}>'

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
        wins_as_host = 0
        last_win, last_loss = False, False

        for game in ranked_games_played:

            is_winner = False
            won_as_host = False
            gamesides = game.ordered_side_list()
            for gs in gamesides:
                # going through in this way uses the results already in memory rather than a bunch of new DB queries
                if gs.id == game.winner_id:
                    winner = gs
                    first_loop = True
                    for l in gs.ordered_player_list():
                        if l.player.discord_member_id == self.id:
                            is_winner = True
                            if first_loop:
                                won_as_host = True
                        first_loop = False
                    break

            logger.debug(f'Game {game.id} completed_ts {game.completed_ts} is a {"win" if is_winner else "loss"} WS: {winning_streak} LS: {losing_streak} last_win: {last_win} last_loss: {last_loss}')
            if is_winner:
                if won_as_host:
                    wins_as_host += 1
                if last_win:
                    # winning streak is extended
                    winning_streak += 1
                    longest_winning_streak = winning_streak if (winning_streak > longest_winning_streak) else longest_winning_streak
                else:
                    # winning streak is broken
                    winning_streak = 1
                    last_win, last_loss = True, False
                if len(winner.lineup) == 1 and len(gamesides) == 2:
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

        return (longest_winning_streak, longest_losing_streak, v2_count, v3_count, duel_wins, duel_losses, wins_as_host, len(ranked_games_played))

    def update_name(self, new_name: str):
        self.name = new_name
        self.save()
        for guildmember in self.guildmembers:
            guildmember.generate_display_name(player_name=new_name, player_nick=guildmember.nick)

    def wins(self, version: str = None):

        date_min, date_max = moonrise_or_air_date_range(version=version)

        server_list = settings.servers_included_in_global_lb()
        q = Lineup.select().join(Game).join_from(Lineup, GameSide).join_from(Lineup, Player).where(
            (Lineup.game.is_completed == 1) &
            (Lineup.game.is_confirmed == 1) &
            (Lineup.game.is_ranked == 1) &
            (Lineup.game.guild_id.in_(server_list)) &
            (Lineup.player.discord_member == self) &
            (Game.winner == Lineup.gameside.id) &
            (Game.date >= date_min) & (Game.date <= date_max)
        )

        return q

    def losses(self, version: str = None):

        date_min, date_max = moonrise_or_air_date_range(version=version)

        server_list = settings.servers_included_in_global_lb()
        q = Lineup.select().join(Game).join_from(Lineup, GameSide).join_from(Lineup, Player).where(
            (Lineup.game.is_completed == 1) &
            (Lineup.game.is_confirmed == 1) &
            (Lineup.game.is_ranked == 1) &
            (Lineup.game.guild_id.in_(server_list)) &
            (Lineup.player.discord_member == self) &
            (Game.winner != Lineup.gameside.id) &
            (Game.date >= date_min) & (Game.date <= date_max)
        )

        return q

    def get_record(self, version: str = None):

        return (self.wins(version=version).count(), self.losses(version=version).count())

    def get_polychamps_record(self):

        try:
            pc_player = Player.get_or_except(player_string=self.discord_id, guild_id=settings.server_ids['polychampions'])
        except exceptions.NoSingleMatch:
            return None

        all_season_games = Game.polychamps_season_games(league='all')[0]
        pro_season_games = Game.polychamps_season_games(league='pro')[0]
        junior_season_games = Game.polychamps_season_games(league='junior')[0]

        losses = Game.search(status_filter=4, player_filter=[pc_player])
        wins = Game.search(status_filter=3, player_filter=[pc_player])

        total_win_count = Game.select(Game.id).where(Game.id.in_(wins) & Game.id.in_(all_season_games)).count()

        total_loss_count = Game.select(Game.id).where(Game.id.in_(losses) & Game.id.in_(all_season_games)).count()

        if not total_win_count and not total_loss_count:
            return None

        pro_win_count = Game.select(Game.id).where(Game.id.in_(wins) & Game.id.in_(pro_season_games)).count()

        pro_loss_count = Game.select(Game.id).where(Game.id.in_(losses) & Game.id.in_(pro_season_games)).count()

        junior_win_count = Game.select(Game.id).where(Game.id.in_(wins) & Game.id.in_(junior_season_games)).count()

        junior_loss_count = Game.select(Game.id).where(Game.id.in_(losses) & Game.id.in_(junior_season_games)).count()

        return {
            'full_record': (total_win_count, total_loss_count),
            'pro_record': (pro_win_count, pro_loss_count),
            'junior_record': (junior_win_count, junior_loss_count)
        }

    def games_played(self, in_days: int = None):

        if in_days:
            date_cutoff = (datetime.datetime.now() + datetime.timedelta(days=-in_days))
        else:
            date_cutoff = datetime.date.min  # 'forever' ?

        return Lineup.select(Lineup.game).join(Game).join_from(Lineup, Player).where(
            ((Lineup.game.date > date_cutoff) | (Lineup.game.completed_ts > date_cutoff)) &
            (Lineup.player.discord_member == self)
        ).order_by(-Game.date)

    def completed_game_count(self, only_ranked: bool = True, moonrise: bool = False):

        if moonrise:
            date_cutoff = settings.moonrise_reset_date
        else:
            date_cutoff = datetime.date.min

        if only_ranked:
            # default behavior, used for elo max_delta
            server_list = settings.servers_included_in_global_lb()
            num_games = Lineup.select().join(Player).join_from(Lineup, Game).where(
                (Lineup.game.is_completed == 1) &
                (Lineup.game.is_ranked == 1) &
                (Lineup.game.guild_id.in_(server_list)) &
                (Lineup.player.discord_member == self) &
                (Lineup.game.date >= date_cutoff)
            ).count()
        else:
            # full count of all games played - used for achievements role setting
            num_games = Lineup.select().join(Player).join_from(Lineup, Game).where(
                (Lineup.game.is_completed == 1) & (Lineup.player.discord_member == self) & (Lineup.game.date >= date_cutoff)
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

    def leaderboard(date_cutoff, guild_id: int = None, max_flag: bool = False, version: str = None):
        # guild_id is a dummy parameter so DiscordMember.leaderboard and Player.leaderboard can be called in identical ways

        if not version:
            version = 'MOONRISE' if is_post_moonrise() else 'AIR'

        version = version.upper()
        if version not in ['AIR', 'MOONRISE', 'ALLTIME']:
            raise ValueError('Valid arguments are "air", "moonrise", or "alltime". Leave as None to use the current verson based on today\'s date.')

        if version == 'AIR':
            elo_field = DiscordMember.elo_max if max_flag else DiscordMember.elo
        elif version == 'MOONRISE':
            elo_field = DiscordMember.elo_max_moonrise if max_flag else DiscordMember.elo_moonrise
        elif version == 'ALLTIME':
            elo_field = DiscordMember.elo_max_alltime if max_flag else DiscordMember.elo_alltime

        query = DiscordMember.select(DiscordMember, elo_field.alias('elo_field')).join(Player).join(Lineup).join(Game).where(
            (Game.is_completed == 1) & (Game.completed_ts > date_cutoff) & (Game.is_ranked == 1) & (DiscordMember.is_banned == 0)
        ).distinct().order_by(-elo_field, DiscordMember.id)

        if query.count() < 10:
            # Include all registered players on leaderboard if not many games played
            query = DiscordMember.select(DiscordMember, elo_field.alias('elo_field')).order_by(-elo_field, DiscordMember.id)

        return query

    def favorite_tribes(self, limit=3):
        # Returns a list of dicts of format:
        # {'tribe': 7, 'emoji': '<:luxidoor:448015285212151809>', 'name': 'Luxidoor', 'tribe_count': 14}

        q = Lineup.select(Lineup.tribe, Tribe.emoji, Tribe.name, fn.COUNT(Lineup.tribe).alias('tribe_count')).join(Tribe).join_from(Lineup, Player).where(
            (Lineup.player.discord_member == self) & (Lineup.tribe.is_null(False))
        ).group_by(Lineup.tribe, Lineup.tribe.emoji, Tribe.name).order_by(-SQL('tribe_count')).limit(limit)

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

    def is_booster(self):
        # boost_level default of None intended to be used in the future to indicate that they have not yet received a one-time advertising PM
        # once it is sent set boost_level to 0

        if self.boost_level:
            return (True, self.boost_level)
        else:
            return (False, 0)


class Player(BaseModel):
    discord_member = ForeignKeyField(DiscordMember, unique=False, null=False, backref='guildmembers', on_delete='CASCADE')
    guild_id = BitField(unique=False, null=False)
    nick = TextField(unique=False, null=True)
    name = TextField(unique=False, null=True)
    team = ForeignKeyField(Team, null=True, backref='player', on_delete='SET NULL')
    elo = SmallIntegerField(default=1000)
    elo_max = SmallIntegerField(default=1000)
    elo_alltime = SmallIntegerField(default=1000)
    elo_max_alltime = SmallIntegerField(default=1000)
    elo_moonrise = SmallIntegerField(default=1000)
    elo_max_moonrise = SmallIntegerField(default=1000)
    trophies = ArrayField(CharField, null=True)
    is_banned = BooleanField(default=False)

    def mention(self):
        return self.discord_member.mention()

    def generate_display_name(self=None, player_name=None, player_nick=None):

        player_name = discord.utils.escape_markdown(discord.utils.escape_mentions(player_name), as_needed=True)

        if player_nick:
            player_nick = discord.utils.escape_markdown(discord.utils.escape_mentions(player_nick), as_needed=True)

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

        player_string = str(player_string)
        p_id = string_to_user_id(player_string)
        if p_id:
            # lookup either on <@####> mention string or raw ID #
            query_by_id = Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.discord_id == p_id) & (Player.guild_id == guild_id)
            )
            if len(query_by_id) > 0:
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

        if len(name_exact_match) == 1:
            # String matches DiscordUser.name exactly
            return name_exact_match
        # If no exact match, return any substring matches - prioritized by number of games played (will not return players with 0 games)

        name_substring_match = Lineup.select(Lineup.player, fn.COUNT('*').alias('games_played')).join(Player).join(DiscordMember).where(
            ((Player.nick.contains(player_string)) | (DiscordMember.name.contains(discord_str))) & (Player.guild_id == guild_id)
        ).group_by(Lineup.player).order_by(-SQL('games_played'))

        if len(name_substring_match) > 0:
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

        logger.debug(f'get_or_except matched string {player_string} to player {results[0].id} {results[0].name} - team {results[0].team_id}')
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

    def completed_game_count(self, moonrise: bool = False):

        if moonrise:
            date_cutoff = settings.moonrise_reset_date
        else:
            date_cutoff = datetime.date.min

        num_games = Lineup.select().join(Game).where(
            (Lineup.game.is_completed == 1) & (Lineup.game.is_ranked == 1) & (Lineup.player == self) & (Lineup.game.date >= date_cutoff)
        ).count()

        return num_games

    def games_played(self, in_days: int = None, min_players: int = None):

        if in_days:
            date_cutoff = (datetime.datetime.now() + datetime.timedelta(days=-in_days))
        else:
            date_cutoff = datetime.date.min  # 'forever' ?

        if not min_players:
            # default: include any game type
            return Lineup.select(Lineup.game).join(Game).where(
                ((Lineup.game.date > date_cutoff) | (Lineup.game.completed_ts > date_cutoff)) & (Lineup.player == self)
            ).order_by(-Game.date)

        # else restrict to games with minimum gamesize. use this to find out how many 2v2 games a player has played, for example.
        subq_games_with_minimum_side_size = Lineup.select(Lineup.game).join(Game).join_from(Lineup, GameSide).where(
            (Lineup.player == self) & (GameSide.size >= min_players)
        )

        # logger.debug(f'Player {self.name} min_players: {min_players} - {len(subq_games_with_minimum_side_size)}')

        return Game.select().where(
            ((Game.date > date_cutoff) | (Game.completed_ts > date_cutoff)) &
            (Game.id.in_(subq_games_with_minimum_side_size))
        ).order_by(-Game.date)

    def wins(self, version: str = None):

        date_min, date_max = moonrise_or_air_date_range(version=version)

        q = Lineup.select().join(Game).join_from(Lineup, GameSide).where(
            (Lineup.game.is_completed == 1) &
            (Lineup.game.is_confirmed == 1) &
            (Lineup.game.is_ranked == 1) &
            (Lineup.player == self) &
            (Game.winner == Lineup.gameside.id) &
            (Game.date >= date_min) & (Game.date <= date_max)
        )

        return q

    def losses(self, version: str = None):

        date_min, date_max = moonrise_or_air_date_range(version=version)

        q = Lineup.select().join(Game).join_from(Lineup, GameSide).where(
            (Lineup.game.is_completed == 1) &
            (Lineup.game.is_confirmed == 1) &
            (Lineup.game.is_ranked == 1) &
            (Lineup.player == self) &
            (Game.winner != Lineup.gameside.id) &
            (Game.date >= date_min) & (Game.date <= date_max)
        )

        return q

    def get_record(self, version: str = None):

        return (self.wins(version=version).count(), self.losses(version=version).count())

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

    def leaderboard(date_cutoff, guild_id: int, max_flag: bool = False, version: str = None):

        if not version:
            version = 'MOONRISE' if is_post_moonrise() else 'AIR'

        version = version.upper()
        if version not in ['AIR', 'MOONRISE', 'ALLTIME']:
            raise ValueError('Valid arguments are "air", "moonrise", or "alltime". Leave as None to use the current verson based on today\'s date.')

        if version == 'AIR':
            elo_field = Player.elo_max if max_flag else Player.elo
        elif version == 'MOONRISE':
            elo_field = Player.elo_max_moonrise if max_flag else Player.elo_moonrise
        elif version == 'ALLTIME':
            elo_field = Player.elo_max_alltime if max_flag else Player.elo_alltime

        query = Player.select(Player, elo_field.alias('elo_field')).join(Lineup).join(Game).join_from(Player, DiscordMember).where(
            (Player.guild_id == guild_id) &
            (Game.is_completed == 1) &
            (Game.is_ranked == 1) &
            (Game.completed_ts > date_cutoff) &
            (Player.is_banned == 0) & (DiscordMember.is_banned == 0)
        ).distinct().order_by(-elo_field, Player.id)

        if query.count() < 10:
            # Include all registered players on leaderboard if not many games played
            query = Player.select(Player, elo_field.alias('elo_field')).where(Player.guild_id == guild_id).order_by(-elo_field, Player.id)

        return query

    def favorite_tribes(self, limit=3):
        # Returns a list of dicts of format:
        # {'tribe': 7, 'emoji': '<:luxidoor:448015285212151809>', 'name': 'Luxidoor', 'tribe_count': 14}

        q = Lineup.select(Lineup.tribe, Tribe.emoji, Tribe.name, fn.COUNT(Lineup.tribe).alias('tribe_count')).join(Tribe).where(
            (Lineup.player == self) & (Lineup.tribe.is_null(False))
        ).group_by(Lineup.tribe, Lineup.tribe.emoji, Tribe.name).order_by(-SQL('tribe_count')).limit(limit)

        return q.dicts()

    def discord_ids_to_elo_list(list_of_discord_ids, guild_id):
        players = Player.select(Player, DiscordMember).join(DiscordMember).where(
            (DiscordMember.discord_id.in_(list_of_discord_ids)) & (Player.guild_id == guild_id)
        )

        elo_list = [p.elo_alltime for p in players] if players else []
        elo_list.sort(reverse=True)
        return elo_list

    def average_elo_of_player_list(list_of_discord_ids, guild_id, weighted=True):

        # Given a group of discord_ids (likely teammates) come up with an average ELO for that group, weighted by how active they are
        # ie if a team has two players and the guy with 1500 elo plays a lot and the guy with 1000 elo plays not at all, 1500 will be the weighted median elo
        players = Player.select(Player, DiscordMember).join(DiscordMember).where(
            (DiscordMember.discord_id.in_(list_of_discord_ids)) & (Player.guild_id == guild_id)
        )

        elo_list = []
        player_games = 0

        for p in players:
            games_played = p.games_played(in_days=30, min_players=2).count()

            # player_elos = [p.elo] * games_played
            # elo_list = elo_list + player_elos
            player_games += games_played

            if weighted:
                max_weighted_games = min(games_played, 10)
                elo_list = elo_list + [p.elo_alltime] * max_weighted_games
            else:
                # Straight average of player elo scores with no weighting
                elo_list.append(p.elo_alltime)

        if elo_list:
            return int(statistics.mean(elo_list)), player_games

        return 0, 0

    class Meta:
        indexes = ((('discord_member', 'guild_id'), True),)   # Trailing comma is required


class Tribe(BaseModel):
    name = TextField(unique=True, null=False)
    emoji = TextField(null=False, default='')

    def get_by_name(name: str):

        tribe_name_match = Tribe.select().where(Tribe.name.startswith(name))

        if tribe_name_match.count() == 0:
            logger.warning(f'No Tribe could be matched to {name}')
            return None
        return tribe_name_match[0]

    def update_emoji(name: str, emoji: str):
        try:
            tribe = Tribe.get(Tribe.name.startswith(name))
        except DoesNotExist:
            raise exceptions.CheckFailedError(f'Could not find any tribe name containing "{name}"')

        tribe.emoji = emoji
        tribe.save()
        return tribe


class Game(BaseModel):
    is_completed = BooleanField(default=False)
    is_confirmed = BooleanField(default=False)
    announcement_message = BitField(default=None, null=True)
    announcement_channel = BitField(default=None, null=True)
    date = DateField(default=datetime.datetime.today)
    completed_ts = DateTimeField(null=True, default=None)  # set when game is confirmed and ELO is calculated (the first time, preserved for subsequent recalcs)
    win_claimed_ts = DateTimeField(null=True, default=None)  # set when win is claimed, used to check old unconfirmed wins
    name = TextField(null=True)
    winner = DeferredForeignKey('GameSide', null=True, on_delete='RESTRICT')
    guild_id = BitField(unique=False, null=False)
    host = ForeignKeyField(Player, null=True, backref='hosting', on_delete='SET NULL')
    expiration = DateTimeField(null=True, default=tomorrow)  # For pending/matchmaking status
    notes = TextField(null=True)
    is_pending = BooleanField(default=False)  # True == open, unstarted game
    is_ranked = BooleanField(default=True)
    game_chan = BitField(default=None, null=True)
    size = ArrayField(SmallIntegerField, default=[0])
    is_mobile = BooleanField(default=True)

    def __setattr__(self, name, value):
        if name == 'name':
            value = value.strip('\"').strip('\'').strip('”').strip('“').title()[:35].strip() if value else value
        return super().__setattr__(name, value)

    async def create_game_channels(self, guild_list, guild_id):
        logger.debug(f'in create_game_channels for game {self.id}')
        guild = discord.utils.get(guild_list, id=guild_id)
        game_roster, side_external_servers = [], []
        ordered_side_list = list(self.ordered_side_list())
        skipping_team_chans, skipping_central_chan = False, False
        exception_encountered, exception_messages = False, []

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
                logger.debug(f'using external guild {side_guild.name} to create team channel')
            else:
                side_guild = guild  # use current guild (ctx.guild)
                using_team_server_flag = False

            player_list = [l.player for l in gameside.ordered_player_list()]
            if len(player_list) < 2:
                continue
            team_name = gameside.team.name if gameside.team else ''
            if (len(guild.text_channels) > 440 and  # Give server some breathing room for non-game channels
                   len(player_list) < 3 and  # Large-team chans still get created
                   not using_team_server_flag and  # if on external server, skip check
                   'Nova' not in team_name):  # skip check for game chans locked to Nova Blue or Nova Red

                # TODO: maybe, have different thresholds, ie start skipping NOva or 3-player channels or full-game channels is server is at a higher mark like 475

                skipping_team_chans = True
                logger.warning('Skipping channel creation for a team due to server exceeding 425 channels')
                continue

            try:
                chan = await channels.create_game_channel(side_guild, game=self, team_name=gameside.team.name, player_list=player_list, using_team_server_flag=using_team_server_flag)
            except exceptions.MyBaseException as e:
                exception_encountered = True
                exception_messages.append(f'Team: {gameside.team.name} - {e}')
                chan = None

            if chan:
                gameside.team_chan = chan.id
                if side_guild.id != guild_id:
                    gameside.team_chan_external_server = side_guild.id
                else:
                    gameside.team_chan_external_server = None
                    # Making sure this is set to None for the edge case of a restarted game that previously had been on a team server
                    # and now no longer needs to be
                gameside.save()

                await channels.greet_game_channel(side_guild, chan=chan, player_list=player_list, roster_names=roster_names, game=self, full_game=False)

        if (len(ordered_side_list) > 2 and len(self.lineup) > 5) or len(ordered_side_list) > 3:
            # create game channel for larger games - 4+ sides, or 3+ sides with 6+ players
            if len(guild.text_channels) < 425:
                player_list = [l.player for l in self.lineup]

                try:
                    chan = await channels.create_game_channel(guild, game=self, team_name=None, player_list=player_list)
                except exceptions.MyBaseException as e:
                    exception_encountered = True
                    exception_messages.append(f'Central Channel: {e}')
                    chan = None

                if chan:
                    self.game_chan = chan.id
                    self.save()
                    await channels.greet_game_channel(guild, chan=chan, player_list=player_list, roster_names=roster_names, game=self, full_game=True)
            else:
                skipping_central_chan = True
                logger.warning('skipping central game channel creation due to server capacity')

        if skipping_central_chan and skipping_team_chans:
            raise exceptions.MyBaseException(f'Skipping 2-player team channels and central game channel creation. Server is at {len(guild.text_channels)}/500 channels.')
        elif skipping_central_chan:
            raise exceptions.MyBaseException(f'Skipping central game channel creation. Server is at {len(guild.text_channels)}/500 channels.')
        elif skipping_team_chans:
            raise exceptions.MyBaseException(f'Skipping 2-player team channels creation. Server is at {len(guild.text_channels)}/500 channels.')
        elif exception_encountered:
            exception_messages = '\n'.join(exception_messages)
            raise exceptions.MyBaseException(f'Unhandled error(s) occured causing some channels to be skipped:\n{exception_messages}')
        else:
            return

    async def delete_game_channels(self, guild_list, guild_id, channel_id_to_delete: int = None):
        guild = discord.utils.get(guild_list, id=guild_id)
        old_4d = (datetime.datetime.now() + datetime.timedelta(days=-4))

        if self.is_season_game():
            return logger.debug(f'Skipping team channel deletion for game {self.id} {self.name} since it is a Season game.')

        if self.notes and 'NOVA RED' in self.notes.upper() and 'NOVA BLUE' in self.notes.upper():
            if self.completed_ts and self.completed_ts > old_4d:
                return logger.warning(f'Skipping team channel deletion for game {self.id} {self.name} since it is a Nova League game concluded recently')

        for gameside in self.gamesides:
            if gameside.team_chan:
                if channel_id_to_delete and gameside.team_chan != channel_id_to_delete:
                    continue
                if gameside.team_chan_external_server:
                    side_guild = discord.utils.get(guild_list, id=gameside.team_chan_external_server)
                    if not side_guild:
                        logger.warning(f'Could not load guild where external team channel is located, gameside ID {gameside.id} guild {gameside.team_chan_external_server}')
                        continue
                else:
                    side_guild = guild
                await channels.delete_game_channel(side_guild, channel_id=gameside.team_chan)
                gameside.team_chan = None
                gameside.save()

        if self.game_chan:
            if channel_id_to_delete and self.game_chan != channel_id_to_delete:
                return
            await channels.delete_game_channel(guild, channel_id=self.game_chan)
            self.game_chan = None
            self.save()

    async def update_squad_channels(self, guild_list, guild_id, message: str = None, suppress_errors: bool = True, include_message_mentions: bool = False):
        guild = discord.utils.get(guild_list, id=guild_id)

        for gameside in list(self.gamesides):
            if gameside.team_chan:
                if gameside.team_chan_external_server:
                    side_guild = discord.utils.get(guild_list, id=gameside.team_chan_external_server)
                    if not side_guild:
                        logger.warning(f'Could not load guild where external team channel is located, gameside ID {gameside.id} guild {gameside.team_chan_external_server}')
                        continue
                    logger.debug(f'Using guild {side_guild} for side_guild')
                else:
                    logger.debug(f'Using default guild {guild} for side_guild')
                    side_guild = guild
                if message:
                    side_message = f'{message}\n{" ".join(gameside.mentions())}' if include_message_mentions else message
                    logger.debug(f'Pinging message to channel {gameside.team_chan} in guild {side_guild}')
                    await channels.send_message_to_channel(side_guild, channel_id=gameside.team_chan, message=side_message, suppress_errors=suppress_errors)
                else:
                    await channels.update_game_channel_name(side_guild, channel_id=gameside.team_chan, game=self, team_name=gameside.team.name)

        if self.game_chan:
            if message:
                game_chan_message = f'{message}\n{" ".join(self.mentions())}' if include_message_mentions else message
                await channels.send_message_to_channel(guild, channel_id=self.game_chan, message=game_chan_message, suppress_errors=suppress_errors)
            else:
                await channels.update_game_channel_name(guild, channel_id=self.game_chan, game=self, team_name=None)

    async def update_announcement(self, guild, prefix):
        # Updates contents of new game announcement with updated game_embed card

        if self.announcement_channel is None or self.announcement_message is None:
            return
        channel = guild.get_channel(self.announcement_channel)
        if channel is None:
            return logger.warning('Couldn\'t get channel in update_announacement')

        try:
            message = await channel.fetch_message(self.announcement_message)
        except discord.DiscordException:
            return logger.warning('Couldn\'t get message in update_announacement')

        try:
            embed, content = self.embed(guild=guild, prefix=prefix)
            await message.edit(embed=embed, content=content)
        except discord.DiscordException:
            return logger.warning('Couldn\'t update message in update_announacement')

    async def update_external_broadcasts(self, deleted=False):
        # update announcement messges sent to external team servers when game is deleted or starts
        for broadcast in self.broadcasts:

            message = await broadcast.fetch_message()
            if not message:
                broadcast.delete_instance()
                continue

            try:
                if deleted:
                    # update messages to reflect game has been deleted
                    await message.edit(content=f'~~{message.content}~~\n(This game has been deleted and can no longer be joined.)')
                    await message.clear_reactions()
                else:
                    # update messages to reflect game has started
                    await message.edit(content=f'~~{message.content}~~\n(This game has started and can no longer be joined.)')
                    await message.remove_reaction(settings.emoji_join_game, message.guild.me)
            except discord.DiscordException as e:
                logger.warn(f'update_external_broadcasts(): could not edit {broadcast.channel_id}/{broadcast.message_id}\n{e}')

            broadcast.delete_instance()

    def reaction_join_string(self):
        return f'Join game {self.id} by reacting with {settings.emoji_join_game}' if self.is_pending else ''

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
            lowest_score = 199  # changed from 99 with the change from 12 max tribes to 15. lowest_score values were going over 100
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

    def platform_emoji(self):
        return '' if self.is_mobile else '🖥'

    def embed(self, prefix, guild=None):
        if self.is_pending:
            return self.embed_pending_game(prefix)
        ranked_str = '' if self.is_ranked else 'Unranked — '
        embed = discord.Embed(title=f'{self.get_headline()} — {ranked_str}*{self.size_string()}*'[:255])

        if self.is_completed == 1:
            if len(embed.title) > 240:
                embed.title = embed.title.replace('**', '')  # Strip bold markdown to regain space in extra-long game titles
            embed.title = (embed.title + f'\n\nWINNER: {self.winner.name()}')[:255]

            # Set embed image (profile picture or team logo)
            if len(self.winner.lineup) == 1 and guild:
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

        # if guild.id != settings.server_ids['polychampions']:
        #     # embed.add_field(value='Powered by **PolyChampions** - https://discord.gg/cX7Ptnv', name='\u200b', inline=False)
        #     embed.set_author(name='PolyChampions', url='https://discord.gg/cX7Ptnv', icon_url='https://cdn.discordapp.com/emojis/488510815893323787.png?v=1')

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

        embed.set_footer(text=f'{self.platform_emoji()} {status_str} - Created {str(self.date)}{completed_str}{host_str}')

        return embed, embed_content

    def embed_pending_game(self, prefix):
        ranked_str = 'Unranked ' if not self.is_ranked else ''
        title_str = f'**{ranked_str}Open Game {self.id}** {self.platform_emoji()}\n{self.size_string()}'
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
        else:
            content_str = self.reaction_join_string()

        embed.add_field(name='Status', value=status_str, inline=True)
        embed.add_field(name='Expires in', value=f'{expiration_str}', inline=True)
        embed.add_field(name='Notes', value=notes_str, inline=False)
        embed.add_field(name='\u200b', value='\u200b', inline=False)
        # embed.set_author(name=title_str)
        # embed.set_thumbnail(url="https://icons-for-free.com/iconfiles/png/512/mobile+phone+multimedia+phone+smartphone+icon-1320168217591317840.png")

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
                if self.is_mobile:
                    poly_id_str = f'\n`{player.discord_member.polytopia_name}`' if len(ordered_player_list) < 10 else ''  # to avoid hitting 1024 char limit on very big sides
                else:
                    poly_id_str = f'\n`{player.discord_member.name_steam if player.discord_member.name_steam else ""}`' if len(ordered_player_list) < 10 else ''
                player_list.append(f'**{player.name}** ({player.elo_moonrise if self.is_post_moonrise() else player.elo}) {tribe_str} {team_str}{poly_id_str}')
            player_str = '\u200b' if not player_list else '\n'.join(player_list)

            embed.add_field(name=f'__Side {side.position}__{side_name} *({side_capacity[0]}/{side_capacity[1]})*', value=player_str[:1024], inline=False)
        return embed, content_str

    def get_gamesides_string(self, include_emoji=True):
        # yields string like:
        # :fried_shrimp: The Crawfish vs :fried_shrimp: TestAccount1 vs :spy: TestBoye1
        gameside_strings = []
        for gameside in self.ordered_side_list():
            # logger.info(f'{self.id} gameside:', gameside)
            emoji = ''
            if gameside.team and len(gameside.lineup) > 1 and include_emoji:
                emoji = gameside.team.emoji

            gameside_strings.append(f'{emoji} **{gameside.name()}**')
        full_squad_string = ' *vs* '.join(gameside_strings)[:225]
        return full_squad_string

    def get_game_status_string(self):
        game = self
        if game.is_pending:
            status_str = 'Not Started'
        elif game.is_completed is False:
            status_str = 'Incomplete'
        else:
            if game.is_confirmed is False:
                (confirmed_count, side_count, _) = game.confirmations_count()
                if side_count > 2:
                    status_str = f'**WINNER** (Unconfirmed by {side_count - confirmed_count} of {side_count}): {game.winner.name()}'
                else:
                    status_str = f'**WINNER** (Unconfirmed): {game.winner.name()}'
            else:
                status_str = f'**WINNER:** {game.winner.name()}'
        return status_str

    def get_headline(self):
        # yields string like:
        # Game 481   :fried_shrimp: The Crawfish vs :fried_shrimp: TestAccount1 vs :spy: TestBoye1\n*Name of Game*
        full_squad_string = self.get_gamesides_string()

        game_name = f'\n\u00a0*{self.name}*' if self.name and self.name.strip() else ''
        # \u00a0 is used as an invisible delimeter so game_name can be split out easily
        return f'Game {self.id}   {full_squad_string}{game_name}'

    def largest_team(self):
        return max(self.size)

    def smallest_team(self):
        return min(self.size)

    def size_string(self):

        if max(self.size) == 1 and len(self.size) > 2:
            return 'FFA'
        return 'v'.join([str(s) for s in self.size])

    def load_full_game(game_id: int):
        # Returns a single Game object with all related tables pre-fetched. or None

        game = Game.select().where(Game.id == game_id)
        subq = GameSide.select(GameSide, Team).join(Team, JOIN.LEFT_OUTER).join_from(GameSide, Squad, JOIN.LEFT_OUTER)

        subq2 = Lineup.select(
            Lineup, Tribe, Player, DiscordMember).join(
            Tribe, JOIN.LEFT_OUTER).join_from(  # Need LEFT_OUTER_JOIN - default inner join would only return records that have a Tribe chosen
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

    def create_game(discord_groups, guild_id, name: str = None, require_teams: bool = False, is_ranked: bool = True, is_mobile: bool = True):
        # discord_groups = list of lists [[d1, d2, d3], [d4, d5, d6]]. each item being a discord.Member object

        teams_for_each_discord_member, list_of_final_teams = Game.pregame_check(discord_groups, guild_id, require_teams)
        logger.debug(f'teams_for_each_discord_member: {teams_for_each_discord_member}\nlist_of_final_teams: {list_of_final_teams}')

        with db.atomic():
            newgame = Game.create(name=name,
                                  guild_id=guild_id,
                                  is_ranked=is_ranked,
                                  is_mobile=is_mobile,
                                  size=[len(g) for g in discord_groups])

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
        logger.debug(f'reverse_elo_changes for game {self.id}')
        for lineup in self.lineup:
            lineup.player.elo += lineup.elo_change_player * -1
            lineup.player.elo_alltime += lineup.elo_change_player_alltime * -1
            lineup.player.elo_moonrise += lineup.elo_change_player_moonrise * -1
            lineup.player.save()
            lineup.elo_change_player = 0
            lineup.elo_change_player_alltime = 0
            lineup.elo_change_player_moonrise = 0
            lineup.elo_after_game = None
            lineup.elo_after_game_alltime = None
            lineup.elo_after_game_global = None
            lineup.elo_after_game_global_alltime = None
            lineup.elo_after_game_moonrise = None
            lineup.elo_after_game_global_moonrise = None

            if lineup.elo_change_discordmember_alltime or lineup.elo_change_discordmember or lineup.elo_change_discordmember_moonrise:
                lineup.player.discord_member.elo += lineup.elo_change_discordmember * -1
                lineup.player.discord_member.elo_alltime += lineup.elo_change_discordmember_alltime * -1
                lineup.player.discord_member.elo_moonrise += lineup.elo_change_discordmember_moonrise * -1
                lineup.player.discord_member.save()
                lineup.elo_change_discordmember = 0
                lineup.elo_change_discordmember_alltime = 0
                lineup.elo_change_discordmember_moonrise = 0
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

            gameside.team_elo_after_game = None
            gameside.team_elo_after_game_alltime = None
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

    def get_side_win_chances(largest_team: int, gameside_list, gameside_elo_list, calc_version: int = 1):
        n = len(gameside_list)

        # Adjust team elos when the amount of players on each team
        # is imbalanced, e.g. 1v2. It changes nothing when sizes are equal
        adjusted_side_elo, win_chance_list = [], []
        sum_raw_elo = sum(gameside_elo_list)
        for s, elo in zip(gameside_list, gameside_elo_list):
            missing_players = largest_team - len(s.lineup)
            avg_opponent_elos = int(round((sum_raw_elo - elo) / (n - 1)))
            adj_side_elo = s.adjusted_elo(missing_players, elo, avg_opponent_elos, calc_version)
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
        logger.debug(f'Running declare_winner for game {self.id}')

        if winning_side.game != self:
            raise exceptions.CheckFailedError(f'GameSide id {winning_side.id} did not play in this game')

        smallest_side = self.smallest_team()

        if smallest_side <= 0:
            return logger.error(f'Cannot declare_winner for game {self.id}: Side with 0 players detected.')

        with db.atomic():
            if confirm is True:
                if self.is_confirmed:
                    # Without this check, possible for a race condition in which two $win commands are issued near-simultaneously and ELO changes are double counted
                    raise exceptions.CheckFailedError(f'Cannot process win. This may happen if this game is closed by multiple people at the same time.')

                if not self.completed_ts:
                    self.completed_ts = datetime.datetime.now()  # will be preserved if ELO is re-calculated after initial win.

                self.is_confirmed = True
                if self.is_ranked:
                    # run elo calculations for player, discordmember, team, squad

                    largest_side = self.largest_team()
                    # gamesides = list(self.gamesides)
                    gamesides = list(self.ordered_side_list())

                    side_elos = [s.average_elo() for s in gamesides]
                    side_elos_discord = [s.average_elo(by_discord_member=True) for s in gamesides]

                    side_elos_alltime = [s.average_elo(alltime=True) for s in gamesides]
                    side_elos_discord_alltime = [s.average_elo(by_discord_member=True, alltime=True) for s in gamesides]

                    team_elos = [s.team.elo if s.team else None for s in gamesides]
                    team_elos_alltime = [s.team.elo_alltime if s.team else None for s in gamesides]

                    squad_elos = [s.squad.elo if s.squad else None for s in gamesides]

                    if self.date >= settings.elo_calc_v2_date:
                        # added two adjustments for games starting 8/2/20:
                        # 50 elo point host advantage for 1-player host sides (1 v X)
                        # smaller handicap for uneven sides in GameSide.adjusted_elo()
                        calc_version = 2
                        if self.size[0] == 1:
                            # For a solo host game (1v1, 1v3, etc), give them an adjustment for an assumed host advantage
                            # Gives them an extra 50 phantom ELO which will make their calculated chance of winning higher, thus ELO prize lower
                            side_elos[0] = side_elos[0] + 50
                            side_elos_discord[0] = side_elos_discord[0] + 50
                            side_elos_alltime[0] = side_elos_alltime[0] + 50
                            side_elos_discord_alltime[0] = side_elos_discord_alltime[0] + 50
                    else:
                        calc_version = 1

                    side_win_chances = Game.get_side_win_chances(largest_side, gamesides, side_elos, calc_version)
                    side_win_chances_discord = Game.get_side_win_chances(largest_side, gamesides, side_elos_discord, calc_version)

                    side_win_chances_alltime = Game.get_side_win_chances(largest_side, gamesides, side_elos_alltime, calc_version)
                    side_win_chances_discord_alltime = Game.get_side_win_chances(largest_side, gamesides, side_elos_discord_alltime, calc_version)

                    if smallest_side > 1:
                        if None not in team_elos:
                            team_win_chances = Game.get_side_win_chances(largest_side, gamesides, team_elos, calc_version)
                            team_win_chances_alltime = Game.get_side_win_chances(largest_side, gamesides, team_elos_alltime, calc_version)
                        else:
                            team_win_chances, team_win_chances_alltime = None, None

                        if None not in squad_elos:
                            squad_win_chances = Game.get_side_win_chances(largest_side, gamesides, squad_elos, calc_version)
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
                            p.change_elo_after_game(side_win_chances_alltime[i], is_winner, alltime=True, moonrise=False)
                            p.change_elo_after_game(side_win_chances_discord_alltime[i], is_winner, by_discord_member=True, alltime=True, moonrise=False)
                            if self.is_post_moonrise():
                                if smallest_side == 1 and self.guild_id in [settings.server_ids['polychampions']]:
                                    logger.info(f'Skipping local ELO for non-team game (polychampions-specific rule')
                                else:
                                    p.change_elo_after_game(side_win_chances[i], is_winner, alltime=False, moonrise=True)
                                p.change_elo_after_game(side_win_chances_discord[i], is_winner, by_discord_member=True, alltime=False, moonrise=True)
                                logger.info(f'Game date {self.date} is after ELO reset date of {settings.moonrise_reset_date}. Counts towards POST-Moonrise ELO')
                            else:
                                p.change_elo_after_game(side_win_chances[i], is_winner, alltime=False, moonrise=False)
                                p.change_elo_after_game(side_win_chances_discord[i], is_winner, by_discord_member=True, alltime=False, moonrise=False)
                                logger.info(f'Game date {self.date} is before ELO reset date of {settings.moonrise_reset_date}. Counts towards pre-Moonrise ELO')

                        if team_win_chances:
                            team_elo_delta = side.team.change_elo_after_game(team_win_chances[i], is_winner)
                            elo_logger.debug(f'Team.change_elo_after_game team.id: {side.team.id} ELO {side.team.elo} adding delta {team_elo_delta}')
                            side.elo_change_team = team_elo_delta
                            side.team.elo = int(side.team.elo + team_elo_delta)
                            side.team_elo_after_game = side.team.elo
                            side.team.save()
                        if team_win_chances_alltime:
                            team_elo_delta = side.team.change_elo_after_game(team_win_chances_alltime[i], is_winner)
                            elo_logger.debug(f'Team.change_elo_after_game team.id: {side.team.id} Alltime ELO {side.team.elo_alltime} adding delta {team_elo_delta}')
                            side.elo_change_team_alltime = team_elo_delta
                            side.team.elo_alltime = int(side.team.elo_alltime + team_elo_delta)
                            side.team_elo_after_game_alltime = side.team.elo_alltime
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
        # return game.lineup, based on either Player object or discord_id. else None if player did not play in this game.

        if name and not discord_id:
            discord_id = string_to_user_id(name)

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
        return (len(self.lineup), sum(s for s in self.size))

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

                p_id = string_to_user_id(name)
                if p_id and p_id == gameside.lineup[0].player.discord_member.discord_id:
                    # name is a <@PlayerMention> or raw player_id
                    # compare to single squad player's discord ID
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
            raise exceptions.TooManyMatches(f'{len(matches)} matches found for "{name}" in game {self.id}. Be more specific or use a @Mention.')

    def mentions(self):
        # return a single flat list of mentions. equivalent to:
        # [f'<@{l.player.discord_member.discord_id}>' for l in game.lineup]
        # except lineup and side ordering is preserved
        side_mentions = [side.mentions() for side in list(self.ordered_side_list())]
        return [mention for side in side_mentions for mention in side]

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

    def search_pending(status_filter: int = 0, ranked_filter: int = 2, guild_id: int = None, player_discord_id: int = None, host_discord_id: int = None, platform_filter: int = 2):
        # status_filter
        # 0 = all open games
        # 1 = full games / waiting to start
        # 2 = games with capacity
        # ranked_filter
        # 0 = unranked (is_ranked == False)
        # 1 = ranked (is_ranked == True)
        # 2 = any
        # platform_filter
        # 0 = desktop (is_mobile == False)
        # 1 = mobile (is_mobile == True)
        # 2 = any

        ranked_filter = [0, 1] if ranked_filter == 2 else [ranked_filter]  # [0] or [1]
        platform_filter = [0, 1] if platform_filter == 2 else [platform_filter]

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
                (Game.is_ranked.in_(ranked_filter)) &
                (Game.is_mobile.in_(platform_filter))
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
                (Game.is_ranked.in_(ranked_filter)) &
                (Game.is_mobile.in_(platform_filter))
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
                (Game.is_ranked.in_(ranked_filter)) &
                (Game.is_mobile.in_(platform_filter))
            ).group_by(Game.id).order_by(
                -(fn.SUM(GameSide.size) - fn.COUNT(Lineup.id))
            ).prefetch(GameSide, Lineup, Player)

    def search(player_filter=None, team_filter=None, title_filter=None, status_filter: int = 0, guild_id: int = None, size_filter=None, platform_filter: int = 2):
        # Returns Games by almost any combination of player/team participation, and game status
        # player_filter/team_filter should be a [List, of, Player/Team, objects] (or ID #s)
        # status_filter:
        # 0 = all games, 1 = completed games, 2 = incomplete games
        # 3 = wins, 4 = losses (only for first player in player_list or, if empty, first team in team list)
        # 5 = unconfirmed wins
        # size filter: array of ints, eg [3, 2] will return games that are 3v2. Ordering matters (will not return 2v3)
        # platform_filter
        # 0 = desktop (is_mobile == False)
        # 1 = mobile (is_mobile == True)
        # 2 = any
        # title_filter should be a [list, of, words] to search for in game notes or title. using iregexp (case insensitive). ordering doesn't matter

        confirmed_filter, completed_filter, pending_filter = [0, 1], [0, 1], [0, 1]
        platform_filter = [0, 1] if platform_filter == 2 else [platform_filter]

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

        if size_filter:
            size_query = Game.select(Game.id).where(Game.size == size_filter)
        else:
            size_query = Game.select(Game.id)

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

            strip_regexp = re.compile('[^1-9a-zA-Z ]')  # strip out everything except alphanumerics and spaces
            clean_search_terms = strip_regexp.sub('', ' '.join(title_filter)).split()
            search_regexp = '^' + ''.join([f'(?=.*{arg})' for arg in clean_search_terms]) + '.+'
            # https://stackoverflow.com/questions/24656131/regex-for-existence-of-some-words-whose-order-doesnt-matter

            title_subq = Game.select(Game.id).where(
                (Game.notes.iregexp(search_regexp)) | (Game.name.iregexp(search_regexp))
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
                Game.id.in_(size_query)
            ) & (
                Game.is_mobile.in_(platform_filter)
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

    def by_channel_id(chan_id: int):
        # Given a discord channel id (such as 722725679443214347) return a Game that uses that channel as its gameside or game channel ID
        # Raise exception if no match or more than one match

        query = Game.select().join(GameSide, on=(GameSide.game == Game.id)).where(
            (GameSide.team_chan == int(chan_id)) | (Game.game_chan == int(chan_id))
        ).distinct()

        if len(query) == 0:
            raise exceptions.NoMatches(f'No matching game found for given channel')
        if len(query) > 1:
            logger.warning(f'by_channel_id - More than one game matches channel ID {chan_id}')
            raise exceptions.TooManyMatches(f'More than game found with this associated channel')

        return query[0]

    def uses_channel_id(self, chan_id: int):
        # Given a discord channel ID, return True if self is associated with that channel

        game_channels = [gs.team_chan for gs in self.gamesides] + [self.game_chan]
        return bool(chan_id in game_channels)

    def by_channel_or_arg(chan_id: int = None, arg: str = None):

        # given a channel_id and/or a string argument, return matching Game if channel_id is associated with a game,
        # else return a Game if arg is a numeric game id.
        # raise exception if invalid argument or no game found

        if chan_id:
            try:
                game = Game.by_channel_id(chan_id=chan_id)
                logger.debug(f'by_channel_or_arg found game {game.id} by chan_id {chan_id}')
                return game
            except exceptions.NoSingleMatch:
                logger.debug(f'by_channel_or_arg - failed channel lookup')

        if not arg:
            logger.debug(f'by_channel_or_arg - no arg provided')
            if chan_id:
                raise exceptions.NoMatches(f'No game found related to the current channel.')
            else:
                raise ValueError('No argument supplied to search for channel.')

        try:
            numeric_arg = int(arg)
        except ValueError:
            logger.debug(f'by_channel_or_arg - non-numeric arg {arg} provided')
            raise ValueError(f'Non-numeric game ID *{arg}* is invalid.')

        try:
            game = Game.get_by_id(numeric_arg)
            logger.debug(f'by_channel_or_arg found game {game.id} by arg {numeric_arg}')
            return game
        except DoesNotExist:
            logger.debug(f'by_channel_or_arg - failed lookup by numeric arg')
            raise exceptions.NoMatches(f'No game found matching game ID `{int(arg)}`.')

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
        db.connect(reuse_if_open=True)
        games = Game.select().where(
            (Game.is_completed == 1) & (Game.is_confirmed == 1) & (Game.completed_ts >= timestamp) & (Game.winner.is_null(False)) & (Game.is_ranked == 1)
        ).order_by(Game.completed_ts).prefetch(GameSide, Lineup)

        elo_logger.debug(f'recalculate_elo_since {timestamp}')
        for g in games:
            g.reverse_elo_changes()
            g.is_completed = 0  # To have correct completed game counts for new ELO calculations
            g.is_confirmed = 0
            g.save()

        for g in games:
            full_game = Game.load_full_game(game_id=g.id)
            full_game.declare_winner(winning_side=full_game.winner, confirm=True)
        elo_logger.debug(f'recalculate_elo_since complete')

    def recalculate_all_elo():
        # Reset all ELOs to 1000, reset completed game counts, and re-run Game.declare_winner() on all qualifying games

        logger.warning('Resetting and recalculating all ELO')
        elo_logger.info(f'recalculate_all_elo')
        settings.recalculation_mode = True

        with db.atomic():
            Player.update(elo=1000, elo_max=1000, elo_alltime=1000, elo_max_alltime=1000, elo_moonrise=1000, elo_max_moonrise=1000).execute()
            Team.update(elo=1000, elo_alltime=1000).execute()
            DiscordMember.update(elo=1000, elo_max=1000, elo_alltime=1000, elo_max_alltime=1000, elo_moonrise=1000, elo_max_moonrise=1000).execute()
            Squad.update(elo=1000).execute()

            bot_members = DiscordMember.select().where(
                DiscordMember.discord_id.in_([settings.bot_id, settings.bot_id_beta])
            )
            bot_update1 = Player.update(elo=0, elo_max=0, elo_alltime=0, elo_max_alltime=0, elo_moonrise=0, elo_max_moonrise=0).where(Player.discord_member_id.in_(bot_members))
            bot_update2 = DiscordMember.update(elo=0, elo_max=0, elo_alltime=0, elo_max_alltime=0, elo_moonrise=0, elo_max_moonrise=0).where(DiscordMember.id.in_(bot_members))
            logger.info(f'Updating {bot_update1.execute()} bot Player records with 0 elo and {bot_update2.execute()} bot DiscordMember records with 0 elo.')

            Game.update(is_completed=0, is_confirmed=0).where(
                (Game.is_confirmed == 1) & (Game.winner.is_null(False)) & (Game.is_ranked == 1) & (Game.completed_ts.is_null(False))
            ).execute()  # Resets completed game counts for players/squads/team ELO bonuses

            games = Game.select().where(
                (Game.is_completed == 0) & (Game.completed_ts.is_null(False)) & (Game.winner.is_null(False)) & (Game.is_ranked == 1)
            ).order_by(Game.completed_ts)

            for game in games:
                full_game = Game.load_full_game(game_id=game.id)
                full_game.declare_winner(winning_side=full_game.winner, confirm=True)

        settings.recalculation_mode = False
        elo_logger.info(f'recalculate_all_elo complete')

    def first_open_side(self, roles):

        # first check to see if there are any sides with a role ID that the user has (ie Team Ronin role ID)
        # returns Side, bool(role_locked_sides)
        # Side will be the first open side that can be joined, or None
        # bool(role_locked_sides) is True if the game contains a side that is role locked to one of the given roles, regardless of capacity

        role_locked_sides = GameSide.select().where(
            (GameSide.game == self) & (GameSide.required_role_id.in_(roles))
        ).order_by(GameSide.position).prefetch(Lineup)

        for side in role_locked_sides:
            if len(side.lineup) < side.size:
                return side, True

        # else just use the first side with no role requirement
        sides = GameSide.select().where(
            (GameSide.game == self) & (GameSide.required_role_id.is_null(True))
        ).order_by(GameSide.position).prefetch(Lineup)

        for side in sides:
            if len(side.lineup) < side.size:
                return side, bool(role_locked_sides)
        return None, bool(role_locked_sides)

    async def join(self, member, side_arg=None, author_member=None, log_note=''):
        # Try to join a guild member to a game. Performs various sanity checks on if join is allowed.
        # Returns (LineupObject=None, MessageList[str])
        # ie for a successful join:  (Lineup, ['Please set your in-game name', 'You will be the host since you are joining side 1'`])
        # if side_arg = None then will use Game.first_open_side(). side_arg can be a numeric position or a side name
        # author_member should be set if the person requesting the join is different than the person to be joined, ie a staff member using $join on a third party

        # address_string = 'You' if not staff_member else member.display_name
        prefix = settings.guild_setting(member.guild.id, 'command_prefix')
        author_member = member if not author_member else author_member
        message_list = []
        inactive_role = discord.utils.get(member.guild.roles, name=settings.guild_setting(member.guild.id, 'inactive_role'))
        # season_inactive_role = discord.utils.get(member.guild.roles, name='Season Inactive')
        log_by_str = f'(Command issued by {GameLog.member_string(author_member)})' if author_member != member else ''
        players, capacity = self.capacity()

        if not self.is_pending:
            return (None, [f'The game has already started and can no longer be joined.'])

        player, _ = Player.get_by_discord_id(discord_id=member.id, discord_name=member.name, discord_nick=member.nick, guild_id=member.guild.id)
        if not player:
            # No Player or DiscordMember
            return (None, [f'*{member.name}* was found in the server but is not registered with me. '
                f'Players can register themselves with `{prefix}setname` for Mobile, or `{prefix}steamname` for Steam/Desktop.'])

        if self.has_player(player)[0]:
            leave_kick_str = f'`{prefix}leave {self.id}`' if author_member == member else f'`{prefix}kick {self.id} {member.name}`'
            return (None, [f'**{player.name}** is already in game {self.id}. If you are trying to change sides, use {leave_kick_str} first.'])

        if player.is_banned or player.discord_member.is_banned:
            if settings.is_mod(author_member):
                message_list.append(f'**{player.name}** has been **ELO Banned** -- *moderator over-ride* :thinking:')
            else:
                return (None, [f'**{player.name}** has been **ELO Banned** and cannot join any new games. :cry:'])

        if not player.discord_member.polytopia_name and self.is_mobile:
            return (None, [f'**{player.name}** does not have a Polytopia in-game name on file. Use `{prefix}setname` to set one.'])

        if not self.is_mobile and not player.discord_member.name_steam:
            return (None, [f'**{player.name}** does not have a Steam username on file and this is a Steam game {self.platform_emoji()}. Use `{prefix}steamname` to set one.'])

        if inactive_role and inactive_role in member.roles:
            if author_member == member:
                await member.remove_roles(inactive_role, reason='Player joined a game so should no longer be inactive')
                message_list.append(f'You have the inactive role **{inactive_role.name}**. Removing it since you seem to be active! :smiling_face_with_3_hearts:')
            else:
                return (None, [f'**{player.name}** has the inactive role *{inactive_role.name}* - cannot join them to a game until the role is removed. The role will be removed if they use the `{prefix}join` command themselves.'])

        # if season_inactive_role and season_inactive_role in member.roles:
            # if self.is_uncaught_season_game():
                # logger.info('Detected member with season_inactive_role joining a potential season game')
                # return (None, [f'**{player.name}** has the season inactive role *{season_inactive_role.name}* and this game appears to be a *Season Game*'])

        waitlist_hosting = [f'{g.id}' for g in Game.search_pending(status_filter=1, guild_id=member.guild.id, host_discord_id=member.id)]
        waitlist_creating = [f'{g.game}' for g in Game.waiting_for_creator(creator_discord_id=member.id)]
        waitlist = set(waitlist_hosting + waitlist_creating)

        if len(waitlist) > 2 and settings.get_user_level(member) < 3:
            # Prevent newer players from having a big backlog of games needing to start and then joining more games
            return (None, [f'You are the host of {len(waitlist)} games that are waiting to start. You cannot join new games until that is complete. Game IDs: **{", ".join(waitlist)}**\n'
                f'Type __`{prefix}game IDNUM`__ for more details, ie `{prefix}game {(waitlist_hosting + waitlist_creating)[0]}`\n'
                f'You must create each game in Polytopia and invite the other players using their friend codes, and then use the `{prefix}start` command in this bot.'])

        on_team, player_team = Player.is_in_team(guild_id=member.guild.id, discord_member=member)
        if settings.guild_setting(member.guild.id, 'require_teams') and not on_team:
            return (None, [f'**{member.name}** must join a Team in order to participate in games on this server.'])

        if side_arg:
            # side specified
            side, side_open = self.get_side(lookup=side_arg)

            if not side:
                return (None, [f'Could not find side with matching {side_arg} in game {self.id}. You can use a side number or name if available.'])
            if not side_open:
                return (None, [f'That side of game {self.id} is already full. See `{prefix}game {self.id}` for details.'])
        else:
            # find first open side
            (side, has_role_locked_side) = self.first_open_side(roles=[role.id for role in member.roles])

            if not side:
                if players < capacity:
                    if has_role_locked_side:
                        return (None, [f'Game {self.id} is limited to specific roles, and your eligible side is **full**. See details with `{prefix}game {self.id}`'])
                    if settings.get_user_level(author_member) >= 5:
                        return (None, [f'Game {self.id} is limited to specific roles. You can override this restriction by specifying the side to join.'])
                    return (None, [f'Game {self.id} is limited to specific roles. You are not allowed to join. See details with`{prefix}game {self.id}`'])
                return (None, [f'Game {self.id} is completely full!'])

        if side.required_role_id and not discord.utils.get(member.roles, id=side.required_role_id):
            if settings.get_user_level(author_member) >= 5:
                message_list.append(f'Side {side.position} of game {self.id} is limited to players with the **@{side.sidename}** role. *Overriding restriction due to staff privileges.*')
            else:
                return (None, [f'Side {side.position} of game {self.id} is limited to players with the **@{side.sidename}** role. You are not allowed to join.'])

        if self.is_hosted_by(player.discord_member.discord_id)[0] and side.position != 1:
            message_list.append(':bulb: Since you are not joining side 1 you will not be the game creator.')

        game_allowed, join_error_message = settings.can_user_join_game(user_level=settings.get_user_level(author_member), game_size=capacity, is_ranked=self.is_ranked, is_host=False)
        if not game_allowed:
            return (None, [join_error_message])

        (min_elo, max_elo, min_elo_g, max_elo_g) = self.elo_requirements()

        if player.elo_moonrise < min_elo or player.elo_moonrise > max_elo:
            if not self.is_hosted_by(author_member.id)[0] and not settings.is_mod(author_member):
                return (None, [f'This game has an ELO restriction of {min_elo} - {max_elo} and **{player.name}** has an ELO of **{player.elo_moonrise}**. Cannot join! :cry: Use `{prefix}games` to list games you *can* join.'])
            message_list.append(f'This game has an ELO restriction of {min_elo} - {max_elo}. Bypassing because you are game host or a mod.')

        if player.discord_member.elo_moonrise < min_elo_g or player.discord_member.elo_moonrise > max_elo_g:
            if not self.is_hosted_by(author_member.id)[0] and not settings.is_mod(author_member):
                return (None, [f'This game has a global ELO restriction of {min_elo_g} - {max_elo_g} and **{player.name}** has a global ELO of **{player.discord_member.elo_moonrise}**. Cannot join! :cry:'])
            message_list.append(f'This game has a global ELO restriction of {min_elo_g} - {max_elo_g}. Bypassing because you are game host or a mod.')

        # list of ID strings that are allowed to join game, e.g. ['272510639124250625', '481527584107003904']
        notes = self.notes if self.notes else ''
        player_restricted_list = re.findall(r'<@!?(\d+)>', notes)

        if player_restricted_list and str(member.id) not in player_restricted_list and (len(player_restricted_list) >= capacity - 1):
            # checking length of player_restricted_list compared to game capacity.. only using restriction if capacity is at least game_size - 1
            # if its game_size - 1, assuming that the host is the 'other' person
            # this isnt really ideal.. could have some games where the restriction should be honored but people are allowed to join.. but better than making the lock too restrictive
            return (None, [f'Game {self.id} is limited to specific players. You are not allowed to join. See game notes for details: `{prefix}game {self.id}`'])

        logger.info(f'Checks passed. Joining player {member.id} {member.display_name} to side {side.position} of game {self.id}')

        with db.atomic():
            lineup = Lineup.create(player=player, game=self, gameside=side)
            player.team = player_team  # update player record with detected team in case its changed since last game.
            player.save()
        message_list.append(f'Joining {member.mention} to side {side.position} of game {self.id}')
        GameLog.write(game_id=self, guild_id=member.guild.id, message=f'Side {side.position} joined by {GameLog.member_string(player.discord_member)} {log_by_str} {log_note}')

        creating_player = self.creating_player()
        if players + 1 < capacity and creating_player == player and member == author_member and settings.get_user_level(member) <= 1:
            message_list.append(':bulb: Since you are joining **side 1**, you will be the host of this game and will be notified when it is full. It will be your responsibility to create the game in Polytopia. '
                f'You can specify a non-host side to join; see `{prefix}help join` in a bot channel.')
        elif creating_player and creating_player != player and settings.get_user_level(member) <= 3:
            message_list.append(f':bulb: To help get the game set up more quickly, send the game host a friend request within Polytopia. The in-game name of the host is `{creating_player.discord_member.polytopia_name}`.')

        if self.is_mobile and not player.discord_member.polytopia_name:
            message_list.append(f':warning: Use `{prefix}setname Your Mobile Name` to set your in-game name. This will replace your friend code in the near future.')

        return (lineup, message_list)

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

        # Full matches that expired more than 4 days ago (ie. host has 3 days to start match before it vanishes)
        purge_deadline = (datetime.datetime.now() + datetime.timedelta(days=-4))

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
        with db.atomic():
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

    def polychamps_season_games(league='all', season=None):
        # infers polychampions season games based on Game.name, something like "PS8W7 Blah Blah" or "JS8 Finals Foo"
        # Junior seasons began with S4
        # relies on name being set reliably
        # default season=None returns all seasons (any digit character). Otherwise pass an integer representing season #
        # Returns three lists: ([All season games], [Regular season games], [Post season games])

        if season:
            season_str = str(season)
        else:
            season_str = '\\d'

        pc_games = Game.select(Game.id).where(
            ((Game.guild_id == settings.server_ids['polychampions']) & ((Game.size == [2, 2]) | (Game.size == [3, 3])))
        )

        if league == 'all':
            full_season = Game.select().where(
                Game.name.iregexp(f'[PJ]?S{season_str}') & Game.id.in_(pc_games)  # matches S5 or PS5 or any S#
            )
        elif league == 'pro':
            if not season:
                early_season_str = 'S[1234]'  # pro seasons before S5 had no 'P' designator
            elif season and season <= 4:
                early_season_str = f'S{str(season)}'
            else:
                early_season_str = None

            full_season = Game.select().where(
                (Game.name.iregexp(f'PS{season_str}') | Game.name.iregexp(early_season_str)) & Game.id.in_(pc_games)  # matches PS5 or S3 (before juniors started)
            )
        elif league == 'junior':
            full_season = Game.select().where(
                Game.name.iregexp(f'JS{season_str}') & Game.id.in_(pc_games)  # matches JS5
            )
        else:
            return ([], [], [])

        playoff_filter = Game.select(Game.id).where(Game.name.contains('FINAL') | Game.name.contains('SEMI'))

        regular_season = Game.select().where(Game.id.in_(full_season) & ~Game.id.in_(playoff_filter))

        post_season = Game.select().where(Game.id.in_(full_season) & Game.id.in_(playoff_filter))

        return (full_season, regular_season, post_season)

    def is_league_game(self):
        # return True if one of the teams participating in the game is a League team like Ronin, etc (is_hidden == 0)
        # seems like I should be able to do it with one less query but could not get it to return the correct result that way
        league_teams = Team.select().where(
            (Team.guild_id == settings.server_ids['polychampions']) & (Team.is_hidden == 0)
        )

        team_subq = GameSide.select(GameSide.game).join(Game).where(
            (GameSide.team.in_(league_teams)) & (GameSide.size > 1)
        ).group_by(GameSide.game)

        league_games = Game.select(Game).where(Game.id.in_(team_subq))

        if self in league_games:
            return True
        return False

    def is_season_game(self):

        # If game is a PolyChamps season game, return tuple like (5, 'P') indicating season 5, pro league (or 'J' for junior)
        # If not, return empty tuple (which has a False boolean value)

        if self.guild_id != settings.server_ids['polychampions']:
            return ()

        if self not in Game.polychamps_season_games()[0]:
            # Making sure game is caught by the polychamps_season_games regexp first
            return ()

        m = re.match(r"([PJ]?)S(\d+)", self.name.upper())
        if not m:
            logger.error(f'Game {self.id} matched regexp in polychamps_season_games() but not is_season_game() - {self.name}')
            return ()

        season = int(m[2])
        if season <= 4:
            league = 'P'
        else:
            league = m[1].upper()

        return (season, league)

    def is_uncaught_season_game(self):
        # Look for games that have a season tag in the notes or not at the beginning of name
        if self.guild_id != settings.server_ids['polychampions']:
            return False

        if self.is_season_game():
            return False

        if self.size == [2, 2] or self.size == [3, 3]:

            name_notes = f'{self.name} {self.notes}'
            return bool(re.search(r'[PJ]?S\d', name_notes, flags=re.IGNORECASE))

        return False

    def is_post_moonrise(self):

        # if True, use moonrise ELO fields such as elo_moonrise / elo_after_game_global_moonrise
        # if False, use the old/archived fields such as elo / elo_after_game_global
        return bool(self.date >= settings.moonrise_reset_date)


class Squad(BaseModel):
    elo = SmallIntegerField(default=1000)
    guild_id = BitField(unique=False, null=False)
    name = TextField(null=False, default='')

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

        elo_logger.debug(f'Squad.change_elo_after_game squad.id: {self.id} ELO {self.elo} adding delta {elo_delta}')
        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta

    def subq_squads_by_size(min_size: int = 2, exact=False):

        if exact:
            # Squads with exactly min_size number of members
            return SquadMember.select(SquadMember.squad).group_by(
                SquadMember.squad
            ).having(fn.COUNT('*') == min_size)

        # Squads with at least min_size number of members
        return SquadMember.select(SquadMember.squad).group_by(
            SquadMember.squad
        ).having(fn.COUNT('*') >= min_size)

    def subq_squads_with_completed_games(min_games: int = 1):
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
            ) & (Squad.guild_id == guild_id) & (Game.completed_ts > date_cutoff)
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

    def has_player(self, player: Player = None, discord_id: int = None):
        # check if player (or discord_id) is a member of this Squad

        if player:
            discord_id = player.discord_member.discord_id

        if not discord_id:
            return False

        for player in self.get_members():
            if player.discord_member.discord_id == int(discord_id):
                logger.debug(f'Found player with id {discord_id} in squad {self.id}')
                return True
        return False


class SquadMember(BaseModel):
    player = ForeignKeyField(Player, null=False, on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadmembers', on_delete='CASCADE')


class GameSide(BaseModel):
    game = ForeignKeyField(Game, null=False, backref='gamesides', on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=True, backref='gamesides', on_delete='SET NULL')
    team = ForeignKeyField(Team, null=True, backref='gamesides', on_delete='RESTRICT')
    required_role_id = BitField(default=None, null=True)
    elo_change_squad = SmallIntegerField(default=0)
    elo_change_team = SmallIntegerField(default=0)
    elo_change_team_alltime = SmallIntegerField(default=0)
    team_elo_after_game = SmallIntegerField(default=None, null=True)  # snapshot of what team elo was after game concluded
    team_elo_after_game_alltime = SmallIntegerField(default=None, null=True)  # snapshot of what alltime team elo was after game concluded
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
            if self.elo_change_team_alltime > 0:
                team_elo_str = f'({self.team.elo_alltime} +{self.elo_change_team_alltime})'
            elif self.elo_change_team_alltime < 0:
                team_elo_str = f'({self.team.elo_alltime} {self.elo_change_team_alltime})'
            else:
                team_elo_str = f'({self.team.elo_alltime})'
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

    def average_elo(self, by_discord_member: bool = False, alltime: bool = False):

        if by_discord_member and alltime:
            elo_list = [l.player.discord_member.elo_alltime for l in self.lineup]
        elif by_discord_member and not alltime:
            elo_list = [l.player.discord_member.elo for l in self.lineup] if self.game.date < settings.moonrise_reset_date else [l.player.discord_member.elo_moonrise for l in self.lineup]
        elif not by_discord_member and alltime:
            elo_list = [l.player.elo_alltime for l in self.lineup]
        elif not by_discord_member and not alltime:
            elo_list = [l.player.elo for l in self.lineup] if self.game.date < settings.moonrise_reset_date else [l.player.elo_moonrise for l in self.lineup]
        else:
            raise ValueError(f'average_elo: should not be here!')

        return int(round(sum(elo_list) / len(elo_list)))

    def adjusted_elo(self, missing_players: int, own_elo: int, opponent_elos: int, calc_version: int = 1):
        # If teams have imbalanced size, adjust win% based on a
        # function of the team's elos involved, e.g.
        # 1v2  [1400] vs [1100, 1100] adjusts to represent 50% win
        # (compared to 58.8% for 1v1v1 for the 1400 player)

        if calc_version == 1:
            handicap = 200  # the elo difference for a 50% 1v2 chance
            # with a 200 handicap the calc will give you a pretend player with 200 less elo as a partner to balance out the team
            # ie in a [1200] vs [1000, 1000] it will give the first side a fake 1000 elo player - 50% chance of 1200 player winning
            # that scenario.
        else:
            handicap = 100
            # changed handicap to 100 8/1/2020, indicating that unbalanced games are easier for the smaller side than previous assumed.
            # ie a [1200] vs [1000, 1000] game should be more like 60% chance to win for the host, not 50%
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

            if is_confirmed and (l.elo_after_game_moonrise or l.elo_after_game):
                # build elo string showing change in elo from this game
                if self.game.is_post_moonrise():
                    elo_change_str = f'+{l.elo_change_player_moonrise}' if l.elo_change_player_moonrise >= 0 else str(l.elo_change_player_moonrise)
                    elo_str = f'{l.elo_after_game_moonrise} {elo_change_str}'
                else:
                    elo_change_str = f'+{l.elo_change_player}' if l.elo_change_player >= 0 else str(l.elo_change_player)
                    elo_str = f'{l.elo_after_game} {elo_change_str}'
            else:
                # build elo string showing current elo only
                elo_str = f'{l.player.elo_moonrise}' if self.game.is_post_moonrise() else f'{l.player.elo}'
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

    def mentions(self):
        return [l.player.mention() for l in self.ordered_player_list()]


class GameLog(BaseModel):
    message = TextField(null=True)
    message_ts = DateTimeField(default=datetime.datetime.now)
    guild_id = BitField(unique=False, null=False, default=0)
    is_protected = BooleanField(default=False)

    # Entries will have guild_id of 0 for things like $setcode and $setname that arent guild-specific

    def member_string(member):

        try:
            # discord.Member API object
            name = member.display_name
            d_id = member.id
        except AttributeError:
            # local discordmember database entry
            name = member.name
            d_id = member.discord_id
        return f'**{discord.utils.escape_markdown(name)}** (`{d_id}`)'

    def write(message, guild_id, game_id=0, is_protected=False):
        if game_id:
            message = f'__{str(game_id)}__ - {message}'

        logger.debug(f'Writing gamelog for game_id {game_id} and guild_id {guild_id}\n{message}')
        return GameLog.create(guild_id=guild_id, message=message, is_protected=is_protected)

    def search(keywords=None, negative_keyword=None, guild_id=None, limit=500):
        if not keywords:
            keywords = '%'  # Wildcard/return all matches
        else:
            keywords = '%' + keywords.replace(' ', '%').replace('_', r'\_') + '%'  # match multiple words with ALL

        if not negative_keyword:
            negative_keyword = 'fakeplaceholderstringthatwonteverbefound'
        else:
            negative_keyword = '%' + negative_keyword + '%'

        if guild_id:
            subq_by_guild = GameLog.select(GameLog.id).where((GameLog.guild_id == guild_id) | (GameLog.guild_id == 0))
        else:
            subq_by_guild = GameLog.select(GameLog.id)

        return GameLog.select().where(
            GameLog.id.in_(subq_by_guild) &
            GameLog.message ** (keywords) &
            ~(GameLog.message ** (negative_keyword)) &
            (GameLog.is_protected == 0)
        ).order_by(-GameLog.message_ts).limit(limit)


class Lineup(BaseModel):
    tribe = ForeignKeyField(Tribe, null=True, on_delete='SET NULL')
    game = ForeignKeyField(Game, null=False, backref='lineup', on_delete='CASCADE')
    gameside = ForeignKeyField(GameSide, null=False, backref='lineup', on_delete='CASCADE')
    player = ForeignKeyField(Player, null=False, backref='lineup', on_delete='RESTRICT')
    elo_change_player = SmallIntegerField(default=0)
    elo_change_discordmember = SmallIntegerField(default=0)
    elo_change_player_alltime = SmallIntegerField(default=0)
    elo_change_discordmember_alltime = SmallIntegerField(default=0)
    elo_change_player_moonrise = SmallIntegerField(default=0)
    elo_change_discordmember_moonrise = SmallIntegerField(default=0)

    elo_after_game = SmallIntegerField(default=None, null=True)
    elo_after_game_global = SmallIntegerField(default=None, null=True)
    elo_after_game_alltime = SmallIntegerField(default=None, null=True)
    elo_after_game_global_alltime = SmallIntegerField(default=None, null=True)
    elo_after_game_moonrise = SmallIntegerField(default=None, null=True)
    elo_after_game_global_moonrise = SmallIntegerField(default=None, null=True)

    def change_elo_after_game(self, chance_of_winning: float, is_winner: bool, by_discord_member: bool = False, alltime: bool = False, moonrise: bool = True):
        # Average(Away Side Elo) is compared to Average(Home_Side_Elo) for calculation - ie all members on a side will have the same elo_delta
        # Team A: p1 900 elo, p2 1000 elo = 950 average
        # Team B: p1 1000 elo, p2 1200 elo = 1100 average
        # ELO is compared 950 vs 1100 and all players treated equally

        # 'moonrise' elo fields reflect a reset on 2020-12-01, shortly after a major game update. 'old'/original ELO kept for posterity.

        if by_discord_member is True:
            if self.game.guild_id not in settings.servers_included_in_global_lb():
                return logger.info(f'Skipping ELO change by discord member because {self.game.guild_id} is set to be excluded.')

            record = self.player.discord_member
        else:
            record = self.player

        if moonrise:
            elo_field = 'elo_moonrise'
            max_field = 'elo_max_moonrise'
            change_field = 'elo_change_discordmember_moonrise' if by_discord_member else 'elo_change_player_moonrise'
            aftergame_field = 'elo_after_game_global_moonrise' if by_discord_member else 'elo_after_game_moonrise'
        elif alltime:
            elo_field = 'elo_alltime'
            max_field = 'elo_max_alltime'
            change_field = 'elo_change_discordmember_alltime' if by_discord_member else 'elo_change_player_alltime'
            aftergame_field = 'elo_after_game_global_alltime' if by_discord_member else 'elo_after_game_alltime'
        else:
            elo_field = 'elo'
            max_field = 'elo_max'
            change_field = 'elo_change_discordmember' if by_discord_member else 'elo_change_player'
            aftergame_field = 'elo_after_game_global' if by_discord_member else 'elo_after_game'

        elo = getattr(record, elo_field)
        num_games = record.completed_game_count(moonrise=moonrise)

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

        elo_bonus = int(abs(elo_delta) * elo_boost)
        elo_delta += elo_bonus

        if self.player.discord_member.discord_id in [settings.bot_id, settings.bot_id_beta]:
            # keep elobot's elo at 0 always - for penalty games
            return logger.info('Skipping elo set for bot user')

        elo_logger.debug(f'Lineup.change_elo_after_game: Global: {by_discord_member} is_winner: {is_winner} alltime: {alltime} moonrise: {moonrise} Game.id {self.game.id} Lineup.id: {self.id} Player/DM.id: {record.id} Original ELO: {elo} Delta: {elo_delta} Original max ELO: {getattr(record, max_field)}')

        new_elo = int(elo + elo_delta)
        with db.atomic():
            setattr(record, elo_field, new_elo)
            if new_elo > getattr(record, max_field):
                setattr(record, max_field, new_elo)
            setattr(self, change_field, elo_delta)
            setattr(self, aftergame_field, new_elo)
            record.save()
            self.save()

    def emoji_str(self):

        if self.tribe and self.tribe.emoji:
            return self.tribe.emoji
        else:
            return ''


class TeamServerBroadcastMessage(BaseModel):
    class Meta:
        table_name = 'team_server_broadcast_message'

    game = ForeignKeyField(Game, null=False, backref='broadcasts', on_delete='CASCADE')
    message_ts = DateTimeField(default=datetime.datetime.now)
    channel_id = BitField(unique=False, null=False)
    message_id = BitField(unique=False, null=False)

    async def fetch_message(self):
        channel = settings.bot.get_channel(self.channel_id)
        try:
            message = await channel.fetch_message(self.message_id) if channel else None
        except discord.DiscordException:
            message = None

        if not message:
            logger.warn(f'TeamServerBroadcastMessage.fetch_message(): could not load {self.channel_id}/{self.message_id}')
            return None
        logger.debug(f'TeamServerBroadcastMessage.fetch_message(): processing message {message.id} in channel {channel.name} guild {message.guild.name}')
        return message


with db.connection_context():
    db.create_tables([Configuration, Team, DiscordMember, Game, Player, Tribe, Squad, GameSide, SquadMember, Lineup, GameLog, TeamServerBroadcastMessage])
    # Only creates missing tables so should be safe to run each time

    try:
        # Creates deferred FK http://docs.peewee-orm.com/en/latest/peewee/models.html#circular-foreign-key-dependencies
        Game._schema.create_foreign_key(Game.winner)
    except (ProgrammingError, DuplicateObject):
        pass
        # Will throw one of above exceptions if foreign key already exists - exception depends on which version of psycopg2 is running
        # if exception is caught inside a transaction then the transaction will be rolled back (create_tables reverted),
        # so using the connection_context() and this section is not run using any transactions
