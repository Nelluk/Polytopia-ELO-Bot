import datetime
import discord
from peewee import *
from playhouse.postgres_ext import *
import modules.exceptions as exceptions
# from modules import utilities
from modules import channels
import logging

logger = logging.getLogger('polybot.' + __name__)

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

        num_games = SquadGame.select().join(Game).where(
            (SquadGame.team == self) & (SquadGame.game.is_completed == 1)
        ).count()

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

        q = Lineup.select(Lineup.game).join(Game).where(Game.is_pending == 0).group_by(Lineup.game).having(fn.COUNT('*') > 2)
        return q

    def get_record(self):

        wins = SquadGame.select().join(Game).where(
            (Game.id.in_(Team.team_games_subq())) & (Game.is_completed == 1) & (SquadGame.team == self) & (SquadGame.id == Game.winner)
        ).count()

        losses = SquadGame.select().join(Game).where(
            (Game.id.in_(Team.team_games_subq())) & (Game.is_completed == 1) & (SquadGame.team == self) & (SquadGame.id != Game.winner)
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
        except IntegrityError:
            created = False
            player = Player.get(discord_member=discord_member, guild_id=guild_id)
            player.nick = discord_nick
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

        def get_matching_roles(discord_member, list_of_role_names):
            # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.polytopia_id
            member_roles = [x.name for x in discord_member.roles]
            return set(member_roles).intersection(list_of_role_names)

        with db:
            query = Team.select(Team.name).where(Team.guild_id == guild_id)
            list_of_teams = [team.name for team in query]               # ['The Ronin', 'The Jets', ...]
            list_of_matching_teams = []
            for player in list_of_players:
                matching_roles = get_matching_roles(player, list_of_teams)
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

    def string_matches(player_string: str, guild_id: int):
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
            if name_exact_match.count() == 1:
                # String matches DiscordUser.name exactly
                return name_exact_match

            # If no exact match, return any substring matches
            name_substring_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                ((Player.nick.contains(player_string)) | (DiscordMember.name.contains(discord_str))) & (Player.guild_id == guild_id)
            )

            if name_substring_match.count() > 0:
                return name_substring_match

            # If no substring name matches, return anything with matching polytopia name or code
            poly_fields_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                ((DiscordMember.polytopia_id.contains(player_string)) | (DiscordMember.polytopia_name.contains(player_string))) & (Player.guild_id == guild_id)
            )
            return poly_fields_match

    def get_or_except(player_string: str, guild_id: int):
        results = Player.string_matches(player_string=player_string, guild_id=guild_id)
        if len(results) == 0:
            raise exceptions.NoMatches(f'No matching player was found for "{player_string}"')
        if len(results) > 1:
            raise exceptions.TooManyMatches(f'More than one matching player was found for "{player_string}"')

        return results[0]

    def completed_game_count(self):

        num_games = Lineup.select().join(Game).where(
            (Lineup.game.is_completed == 1) & (Lineup.player == self)
        ).count()

        return num_games

    def wins(self):
        # TODO: Could combine wins/losses into one function that takes an argument and modifies query

        q = Lineup.select().join(Game).join_from(Lineup, SquadGame).where(
            (Lineup.game.is_completed == 1) & (Lineup.player == self) & (Game.winner == Lineup.squadgame.id)
        )

        return q

    def losses(self):
        q = Lineup.select().join(Game).join_from(Lineup, SquadGame).where(
            (Lineup.game.is_completed == 1) & (Lineup.player == self) & (Game.winner != Lineup.squadgame.id)
        )

        return q

    def get_record(self):

        return (self.wins().count(), self.losses().count())

    def leaderboard_rank(self, date_cutoff):
        # TODO: This could be replaced with Postgresql Window functions to have the DB calculate the rank.
        # Advantages: Probably moderately more efficient, and will resolve ties in a sensible way
        # But no idea how to write the query :/

        query = Player.leaderboard(date_cutoff=date_cutoff, guild_id=self.guild_id)

        player_found = False
        for counter, p in enumerate(query.tuples()):
            if p[0] == self.id:
                player_found = True
                break

        rank = counter + 1 if player_found else None
        return (rank, query.count())

    def leaderboard(date_cutoff, guild_id: int):
        query = Player.select().join(Lineup).join(Game).where(
            (Player.guild_id == guild_id) & (Game.is_completed == 1) & (Game.date > date_cutoff)
        ).distinct().order_by(-Player.elo)

        if query.count() < 10:
            # Include all registered players on leaderboard if not many games played
            query = Player.select().where(Player.guild_id == guild_id).order_by(-Player.elo)

        return query

    def favorite_tribes(self, limit=3):
        # Returns a list of dicts of format:
        # {'tribe': 7, 'emoji': '<:luxidoor:448015285212151809>', 'name': 'Luxidoor', 'tribe_count': 14}

        q = Lineup.select(Lineup.tribe, TribeFlair.emoji, Tribe.name, fn.COUNT(Lineup.tribe).alias('tribe_count')).join(TribeFlair).join(Tribe).where(
            (Lineup.player == self) & (Lineup.tribe.is_null(False))
        ).group_by(Lineup.tribe, Lineup.tribe.emoji, Tribe.name).order_by(-SQL('tribe_count')).limit(limit)

        return q.dicts()

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
        tribe_flair_match = TribeFlair.select(TribeFlair, Tribe).join(Tribe).where(
            (Tribe.name.contains(name)) & (TribeFlair.guild_id == guild_id)
        )

        tribe_name_match = Tribe.select().where(Tribe.name.contains(name))

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
    is_pending = BooleanField(default=False)     # For matchmaking
    announcement_message = BitField(default=None, null=True)
    announcement_channel = BitField(default=None, null=True)
    date = DateField(default=datetime.datetime.today)
    completed_ts = DateTimeField(null=True, default=None)
    name = TextField(null=True)
    winner = DeferredForeignKey('SquadGame', null=True)
    guild_id = BitField(unique=False, null=False)

    async def create_squad_channels(self, ctx):
        game_roster = []
        for squadgame in self.squads:
            game_roster.append([r[0].name for r in squadgame.roster()])

        roster_names = ' -vs- '.join([' '.join(side) for side in game_roster])
        # yields a string like 'Player1 Player2 -vs- Player3 Player4'

        for squadgame in self.squads:
            player_list = [r[0] for r in squadgame.roster()]
            if len(player_list) < 2:
                continue
            chan = await channels.create_squad_channel(ctx, game=self, team_name=squadgame.team.name, player_list=player_list)
            if chan:
                squadgame.team_chan = chan.id
                squadgame.save()

                await channels.greet_squad_channel(ctx, chan=chan, player_list=player_list, roster_names=roster_names, game=self)

    async def delete_squad_channels(self, ctx):

        if self.name.lower()[:2] == 's3' or self.name.lower()[:2] == 's4' or self.name.lower()[:2] == 's5':
            return logger.warn(f'Skipping team channel deletion for game {self.id} {self.name} since it is a Season game')

        for squadgame in self.squads:
            if squadgame.team_chan:
                await channels.delete_squad_channel(ctx, channel_id=squadgame.team_chan)

    async def update_squad_channels(self, ctx):

        for squadgame in self.squads:
            if squadgame.team_chan:
                await channels.update_squad_channel_name(ctx, channel_id=squadgame.team_chan, game_id=self.id, game_name=self.name, team_name=squadgame.team.name)

    async def update_announcement(self, ctx):
        # Updates contents of new game announcement with updated game_embed card

        if self.announcement_channel is None or self.announcement_message is None:
            return
        channel = ctx.guild.get_channel(self.announcement_channel)
        if channel is None:
            return logger.warn('Couldn\'t get channel in update_announacement')

        try:
            message = await channel.get_message(self.announcement_message)
        except (discord.errors.Forbidden, discord.errors.NotFound, discord.errors.HTTPException):
            return logger.warn('Couldn\'t get message in update_announacement')

        try:
            embed = self.embed(ctx)
            await message.edit(embed=embed)
        except discord.errors.HTTPException:
            return logger.warn('Couldn\'t update message in update_announacement')

    def embed(self, ctx):
        if len(self.squads) != 2:
            raise exceptions.CheckFailedError('Support for games with >2 sides not yet implemented')

        home_side = self.squads[0]
        away_side = self.squads[1]

        winner = self.get_winner()

        game_headline = self.get_headline()
        game_headline = game_headline.replace('\u00a0', '\n')   # Put game.name onto its own line if its there

        embed = discord.Embed(title=game_headline)

        if self.is_completed == 1:
            embed.title += f'\n\nWINNER: {winner.name}'

        # Set embed image (profile picture or team logo)
            if self.team_size() == 1:
                winning_discord_member = ctx.guild.get_member(winner.discord_member.discord_id)
                if winning_discord_member is not None:
                    embed.set_thumbnail(url=winning_discord_member.avatar_url_as(size=512))
            elif winner.image_url:
                embed.set_thumbnail(url=winner.image_url)

        # TEAM/SQUAD ELOs and ELO DELTAS
        home_team_elo_str, home_squad_elo_str = home_side.elo_strings()
        away_team_elo_str, away_squad_elo_str = away_side.elo_strings()

        if home_side.team.name == 'Home' and away_side.team.name == 'Away':
            # Hide team ELO if its just generic Home/Away
            home_team_elo_str = away_team_elo_str = ''

        if self.team_size() == 1:
            # Hide squad ELO stats for 1v1 games
            home_squad_elo_str = away_squad_elo_str = '\u200b'

        game_data = [(home_side, home_team_elo_str, home_squad_elo_str, home_side.roster()), (away_side, away_team_elo_str, away_squad_elo_str, away_side.roster())]

        for side, elo_str, squad_str, roster in game_data:
            if self.team_size() > 1:
                embed.add_field(name=f'Lineup for Team **{side.team.name}** {elo_str}', value=squad_str, inline=False)

            for player, player_elo_str, tribe_emoji in roster:
                embed.add_field(name=f'**{player.name}** {tribe_emoji}', value=f'ELO: {player_elo_str}', inline=True)

        embed.set_footer(text=f'Status: {"Completed" if self.is_completed else "Incomplete"}  -  Creation Date {str(self.date)}')

        return embed

    def get_headline(self):
        if len(self.squads) != 2:
            raise exceptions.CheckFailedError('Support for games with >2 sides not yet implemented')

        home_name, away_name = self.squads[0].name(), self.squads[1].name()
        home_emoji = self.squads[0].team.emoji if self.squads[0].team.emoji else ''
        away_emoji = self.squads[1].team.emoji if self.squads[1].team.emoji else ''
        game_name = f'\u00a0*{self.name}*' if self.name and self.name.strip() else ''  # \u00a0 is used as an invisible delimeter so game_name can be split out easily

        return f'Game {self.id}   {home_emoji} **{home_name}** *vs* **{away_name}** {away_emoji}{game_name}'

    def team_size(self):
        return len(self.squads[0].lineup)

    def load_full_game(game_id: int):
        # Returns a single Game object with all related tables pre-fetched. or None

        game = Game.select().where(Game.id == game_id)
        subq = SquadGame.select(SquadGame, Team).join(Team, JOIN.LEFT_OUTER).join_from(SquadGame, Squad, JOIN.LEFT_OUTER)

        subq2 = Lineup.select(
            Lineup, Tribe, TribeFlair, Player, DiscordMember).join(
            TribeFlair, JOIN.LEFT_OUTER).join(  # Need LEFT_OUTER_JOIN - default inner join would only return records that have a Tribe chosen
            Tribe, JOIN.LEFT_OUTER).join_from(
            Lineup, Player).join_from(Player, DiscordMember)

        res = prefetch(game, subq, subq2)

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
                                  is_pending=False,
                                  guild_id=guild_id)

            side_home_players = []
            side_away_players = []
            # Create/update Player records
            for player_discord, player_team in zip(teams[0], list_of_home_teams):
                side_home_players.append(
                    Player.upsert(discord_id=player_discord.id, discord_name=player_discord.name, discord_nick=player_discord.nick, guild_id=guild_id, team=player_team)[0]
                )

            for player_discord, player_team in zip(teams[1], list_of_away_teams):
                side_away_players.append(
                    Player.upsert(discord_id=player_discord.id, discord_name=player_discord.name, discord_nick=player_discord.nick, guild_id=guild_id, team=player_team)[0]
                )

            # Create/update Squad records
            home_squad, away_squad = None, None
            if len(side_home_players) > 1:
                home_squad = Squad.upsert(player_list=side_home_players, guild_id=guild_id)
            if len(side_away_players) > 1:
                away_squad = Squad.upsert(player_list=side_away_players, guild_id=guild_id)

            home_squadgame = SquadGame.create(game=newgame, squad=home_squad, team=home_side_team)

            for p in side_home_players:
                Lineup.create(game=newgame, squadgame=home_squadgame, player=p)

            away_squadgame = SquadGame.create(game=newgame, squad=away_squad, team=away_side_team)

            for p in side_away_players:
                Lineup.create(game=newgame, squadgame=away_squadgame, player=p)

        return newgame

    def delete_game(self):
        # resets any relevant ELO changes to players and teams, deletes related lineup records, and deletes the game entry itself

        self.winner = None
        self.save()

        for lineup in self.lineup:
            lineup.player.elo += lineup.elo_change_player * -1
            lineup.player.save()
            lineup.delete_instance()

        for squadgame in self.squads:
            squadgame.squad.elo += (squadgame.elo_change_squad * -1)
            squadgame.squad.save()

            squadgame.team.elo += (squadgame.elo_change_team * -1)
            squadgame.team.save()
            squadgame.delete_instance()

        self.delete_instance()

    def declare_winner(self, winning_side, confirm: bool):

        # TODO: does not support games != 2 sides
        if len(self.squads) != 2:
            raise exceptions.CheckFailedError('Support for games with >2 sides not yet implemented')

        for squadgame in self.squads:
            if squadgame != winning_side:
                losing_side = squadgame

        if confirm:
            self.is_confirmed = True

            # STEP 1: INDIVIDUAL/PLAYER ELO
            winning_side_ave_elo = winning_side.get_member_average_elo()
            losing_side_ave_elo = losing_side.get_member_average_elo()

            for winning_member in winning_side.lineup:
                winning_member.change_elo_after_game(my_side_elo=winning_side_ave_elo, opponent_elo=losing_side_ave_elo, is_winner=True)

            for losing_member in losing_side.lineup:
                losing_member.change_elo_after_game(my_side_elo=losing_side_ave_elo, opponent_elo=winning_side_ave_elo, is_winner=False)

            if self.team_size() > 1:
                # STEP 2: SQUAD ELO
                winning_squad_elo, losing_squad_elo = winning_side.squad.elo, losing_side.squad.elo
                winning_side.elo_change_squad = winning_side.squad.change_elo_after_game(opponent_elo=losing_squad_elo, is_winner=True)
                losing_side.elo_change_squad = losing_side.squad.change_elo_after_game(opponent_elo=winning_squad_elo, is_winner=False)

                # STEP 3: TEAM ELO
                winning_team_elo, losing_team_elo = winning_side.team.elo, losing_side.team.elo
                winning_side.elo_change_team = winning_side.team.change_elo_after_game(opponent_elo=losing_team_elo, is_winner=True)
                losing_side.elo_change_team = losing_side.team.change_elo_after_game(opponent_elo=winning_team_elo, is_winner=False)

            winning_side.save()
            losing_side.save()

        self.winner = winning_side
        self.is_completed = True
        self.completed_ts = datetime.datetime.now()
        self.save()

    def return_participant(self, ctx, player=None, team=None):
        # Given a string representing a player or a team (team name, player name/nick/ID)
        # Return a tuple of the participant and their squadgame, ie Player, SquadGame or Team, Squadgame

        if player:
            player_obj = Player.get_or_except(player_string=player, guild_id=ctx.guild.id)

            for squadgame in self.squads:
                for p in squadgame.lineup:
                    if p.player == player_obj:
                        return player_obj, squadgame

            raise exceptions.CheckFailedError(f'{player_obj.name} did not play in game {self.id}.')

        elif team:
            team_obj = Team.get_or_except(team_name=team, guild_id=ctx.guild.id)

            for squadgame in self.squads:
                if squadgame.team == team_obj:
                    return team_obj, squadgame

            raise exceptions.CheckFailedError(f'{team_obj.name} did not play in game {self.id}.')
        else:
            raise exceptions.CheckFailedError('Player name or team name must be supplied for this function')

    def get_winner(self):
        # Returns player name of winner if its a 1v1, or team-name of winning side if its a group game

        for squadgame in self.squads:
            if squadgame == self.winner:
                if len(squadgame.lineup) > 1:
                    return squadgame.team
                else:
                    return squadgame.lineup[0].player

        return None

    def search(player_filter=None, team_filter=None, status_filter: int = 0, guild_id: int = None):
        # Returns Games by almost any combination of player/team participation, and game status
        # player_filter/team_filter should be a [List, of, Player/Team, objects] (or ID #s)
        # status_filter:
        # 0 = all games, 1 = completed games, 2 = incomplete games
        # 3 = wins, 4 = losses (only for first player in player_list or, if empty, first team in team list)
        # 5 = unconfirmed wins, 6 = pending games (matchmaking sessions)

        confirmed_filter, completed_filter, pending_filter = [0, 1], [0, 1], [0]

        if status_filter == 1:
            # completed games
            completed_filter = [1]
        elif status_filter == 2:
            # incomplete games
            completed_filter = [0]
        elif status_filter == 5:
            # Unconfirmed completed games
            completed_filter, confirmed_filter = [1], [0]
        elif status_filter == 6:
            # 'pending' matchmaking games
            pending_filter = [1]

        if guild_id:
            guild_filter = Game.select(Game.id).where(Game.guild_id == guild_id)
        else:
            guild_filter = Game.select(Game.id)

        if team_filter:
            team_subq = SquadGame.select(SquadGame.game).join(Game).where(
                (SquadGame.team.in_(team_filter)) & (SquadGame.game.in_(Team.team_games_subq()))
            ).group_by(SquadGame.game).having(
                fn.COUNT(SquadGame.team) == len(team_filter)
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

        if (not player_filter and not team_filter) or status_filter not in [3, 4]:
            # No filtering on wins/losses
            victory_subq = Game.select(Game.id)
        else:
            if player_filter:
                # Filter wins/losses on first entry in player_filter
                if status_filter == 3:
                    # Games that player has won
                    victory_subq = Lineup.select(Lineup.game).join(Game).join_from(Lineup, SquadGame).where(
                        (Lineup.game.is_completed == 1) & (Lineup.player == player_filter[0]) & (Game.winner == Lineup.squadgame.id)
                    )
                elif status_filter == 4:
                    # Games that player has lost
                    victory_subq = Lineup.select(Lineup.game).join(Game).join_from(Lineup, SquadGame).where(
                        (Lineup.game.is_completed == 1) & (Lineup.player == player_filter[0]) & (Game.winner != Lineup.squadgame.id)
                    )
            else:
                # Filter wins/losses on first entry in team_filter
                if status_filter == 3:
                    # Games that team has won
                    victory_subq = SquadGame.select(SquadGame.game).join(Game).where(
                        (SquadGame.team == team_filter[0]) & (SquadGame.id == Game.winner)
                    )
                elif status_filter == 4:
                    # Games that team has lost
                    victory_subq = SquadGame.select(SquadGame.game).join(Game).where(
                        (SquadGame.team == team_filter[0]) & (SquadGame.id != Game.winner)
                    )

        game = Game.select().where(
            (
                Game.id.in_(team_subq)
            ) & (
                Game.id.in_(player_subq)
            ) & (
                Game.is_completed.in_(completed_filter)
            ) & (
                Game.is_confirmed.in_(confirmed_filter)
            ) & (
                Game.is_pending.in_(pending_filter)
            ) & (
                Game.id.in_(victory_subq)
            ) & (
                Game.id.in_(guild_filter)
            )
        ).order_by(-Game.date).prefetch(SquadGame, Team, Lineup, Player)

        return game


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

        num_games = SquadGame.select().join(Game).where(
            (Game.is_completed == 1) & (SquadGame.squad == self)
        ).count()

        return num_games

    def change_elo_after_game(self, opponent_elo, is_winner):

        if self.completed_game_count() < 6:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - self.elo) / 400.0))), 3)

        if is_winner is True:
            new_elo = round(self.elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(self.elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - self.elo)
        print('Squad chance of winning: {} opponent elo:{} current ELO {}, new elo {}, elo_delta {}'.format(chance_of_winning, opponent_elo, self.elo, new_elo, elo_delta))

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
        return SquadGame.select(SquadGame.squad).join(Game).where(Game.is_completed == 1).group_by(
            SquadGame.squad
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

        games_logged = Game.select().count()
        if games_logged < 10:
            min_games = 0
        elif games_logged < 30:
            min_games = 1
        else:
            min_games = 2

        q = Squad.select().join(SquadGame).join(Game).where(
            (
                Squad.id.in_(Squad.subq_squads_with_completed_games(min_games=min_games))
            ) & (Squad.guild_id == guild_id) & (Game.date > date_cutoff)
        ).order_by(-Squad.elo).group_by(Squad).prefetch(SquadMember, Player)

        return q

    def get_matching_squad(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns squad with exactly the same participating players. See https://stackoverflow.com/q/52010522/1281743
        query = Squad.select().join(SquadMember).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list)) & (fn.SUM(SquadMember.player.not_in(player_list).cast('integer')) == 0)
        )

        return query

    def get_all_matching_squads(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns all squads containing players in player list. Used to look up a squad by partial or complete membership

        # Limited to squads with at least 2 members and at least 1 completed game
        query = Squad.select().join(SquadMember).where(
            (Squad.id.in_(Squad.subq_squads_by_size(min_size=2))) & (Squad.id.in_(Squad.subq_squads_with_completed_games()))
        ).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list))
        )

        return query

    def get_record(self):

        # Filter 1v1 games from results
        subq = Lineup.select(Lineup.squadgame.game).join(SquadGame).group_by(
            Lineup.squadgame.game
        ).having(fn.COUNT('*') > 2)

        wins = Game.select(Game, SquadGame).join(SquadGame).where(
            (Game.id.in_(subq)) & (Game.is_completed == 1) & (SquadGame.squad == self) & (SquadGame.id == Game.winner)
        ).count()

        losses = Game.select(Game, SquadGame).join(SquadGame).where(
            (Game.id.in_(subq)) & (Game.is_completed == 1) & (SquadGame.squad == self) & (SquadGame.id != Game.winner)
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


class SquadGame(BaseModel):
    game = ForeignKeyField(Game, null=False, backref='squads', on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=True, backref='squadgame', on_delete='CASCADE')
    team = ForeignKeyField(Team, null=False, backref='squadgame')
    elo_change_squad = SmallIntegerField(default=0)
    elo_change_team = SmallIntegerField(default=0)
    # team_chan_category = BitField(default=None, null=True)
    team_chan = BitField(default=None, null=True)   # Store category/ID of team channel for more consistent renaming-deletion

    def elo_strings(self):
        # Returns a tuple of strings for team ELO and squad ELO display. ie:
        # ('1200 +30', '1300')

        team_elo_str = str(self.elo_change_team) if self.elo_change_team != 0 else ''
        if self.elo_change_team > 0:
            team_elo_str = '+' + team_elo_str

        if self.squad:
            squad_elo_str = str(self.elo_change_squad) if self.elo_change_squad != 0 else ''
            if self.elo_change_squad > 0:
                squad_elo_str = '+' + squad_elo_str
            if squad_elo_str:
                squad_elo_str = '(' + squad_elo_str + ')'

            return (f'({self.team.elo} {team_elo_str})', f'{self.squad.elo} {squad_elo_str}')
        else:
            return (f'({self.team.elo} {team_elo_str})', None)

    def get_member_average_elo(self):
        elo_list = [l.player.elo for l in self.lineup]
        return round(sum(elo_list) / len(elo_list))

    def name(self):
        if len(self.lineup) == 1:
            # 1v1 game
            return self.lineup[0].player.name
        else:
            # Team game
            return self.team.name

    def roster(self):
        # Returns list of tuples [(player, elo string (1000 +50), :tribe_emoji:)]
        players = []

        for l in self.lineup:
            elo_str = str(l.elo_change_player) if l.elo_change_player != 0 else ''
            if l.elo_change_player > 0:
                elo_str = '+' + elo_str
            players.append(
                (l.player, f'{l.player.elo} {elo_str}', l.emoji_str())
            )

        return players


class Lineup(BaseModel):
    tribe = ForeignKeyField(TribeFlair, null=True)
    game = ForeignKeyField(Game, null=False, backref='lineup', on_delete='CASCADE')
    squadgame = ForeignKeyField(SquadGame, null=False, backref='lineup')
    player = ForeignKeyField(Player, null=False, backref='lineup')
    elo_change_player = SmallIntegerField(default=0)

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
        elo_boost = .30 * ((1200 - max(min(self.player.elo, 1200), 900)) / 150)  # 30% boost to delta at elo 1000, gradually shifts to 0% boost at 1200 ELO
        elo_bonus = int(abs(elo_delta) * elo_boost)

        elo_delta += elo_bonus

        print(f'Player chance of winning: {chance_of_winning} opponent elo:{opponent_elo} my_side_elo: {my_side_elo},'
                f'elo_delta {elo_delta}, current_player_elo {self.player.elo}, new_player_elo {int(self.player.elo + elo_delta)}')

        self.player.elo = int(self.player.elo + elo_delta)
        self.elo_change_player = elo_delta
        self.player.save()
        self.save()

    def emoji_str(self):

        if self.tribe and self.tribe.emoji:
            return self.tribe.emoji
        else:
            return ''


with db:
    db.create_tables([Team, DiscordMember, Game, Player, Tribe, Squad, SquadGame, SquadMember, Lineup, TribeFlair])
    # Only creates missing tables so should be safe to run each time
    try:
        # Creates deferred FK http://docs.peewee-orm.com/en/latest/peewee/models.html#circular-foreign-key-dependencies
        Game._schema.create_foreign_key(Game.winner)
    except ProgrammingError:
        pass
        # Will throw this exception if the foreign key has already been created
