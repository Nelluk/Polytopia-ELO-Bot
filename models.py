import datetime
from peewee import *
db = SqliteDatabase('bot_database.db', pragmas={
    'journal_mode': 'wal',
    'cache_size': -1 * 64000,  # 64MB
    'foreign_keys': 1,
    'ignore_check_constraints': 0,
    'synchronous': 0})


def calc_squad_elo(players_in_squad):
    # Given [Player1, Player2, ...], calculate ELO and return as an int
    list_of_elos = [player.elo for player in players_in_squad]
    ave_elo = round(sum(list_of_elos) / len(list_of_elos))
    return ave_elo


class BaseModel(Model):
    class Meta:
        database = db


class Team(BaseModel):
    teamname = CharField(unique=True, null=False, constraints=[SQL('COLLATE NOCASE')])    # team name needs to == discord role name for bot to check player's team membership
    elo = IntegerField(default=1000)
    emoji = CharField(null=True)
    image_url = CharField(null=True)

    def change_elo_after_game(self, opponent_elo, is_winner):

        max_elo_delta = 75
        print('Team Opponent ELO: {}'.format(opponent_elo))
        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - self.elo) / 400))), 3)

        if is_winner is True:
            new_elo = round(self.elo + (max_elo_delta * (1 - chance_of_winning)), 0)

        else:
            new_elo = round(self.elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - self.elo)
        print('Team chance of winning: {} current ELO {}, new elo {}, elo_delta {}'.format(chance_of_winning, self.elo, new_elo, elo_delta))

        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta

    def set_elo_from_delta(self, elo_delta):
        self.elo += elo_delta
        return self.elo

    def get_record(self):
        # wins = Game.select().where(Game.winner == self).count()
        # losses = Game.select().where(Game.loser == self).count()
        wins = len(self.winning_games)
        losses = len(self.losing_games)
        return (wins, losses)


class Game(BaseModel):
    winner = ForeignKeyField(Team, null=True, backref='winning_games')
    loser = ForeignKeyField(Team, null=True, backref='losing_games')
    home_team = ForeignKeyField(Team, null=False, backref='games')
    away_team = ForeignKeyField(Team, null=False, backref='games')
    team_size = IntegerField(null=False)
    is_completed = BooleanField(default=0)
    winner_delta = IntegerField(default=0)
    loser_delta = IntegerField(default=0)
    timestamp = DateTimeField(default=datetime.datetime.now)

    def get_roster(self, team):
        # Returns list of tuples [(player), (elo_change_from_this_game)]
        players = []

        for lineup in self.lineup:
            if lineup.team == team:
                emoji_str = lineup.tribe.emoji if (lineup.tribe and lineup.tribe.emoji) else ''
                players.append((lineup.player, lineup.elo_change, emoji_str))

        return players

    def declare_winner(self, winning_team, losing_team):

        winning_players = []
        losing_players = []
        for lineup in self.lineup:
            if lineup.team == winning_team:
                winning_players.append(lineup.player)
            else:
                losing_players.append(lineup.player)

        if self.team_size == 1:
            # 1v1 game - compare player vs player ELO
            winning_players[0].change_elo_after_game(self, winning_players[0].elo, is_winner=True)
            losing_players[0].change_elo_after_game(self, losing_players[0].elo, is_winner=False)
        else:
            winning_squad = Squad.get_matching_squad(winning_players)[0]
            losing_squad = Squad.get_matching_squad(losing_players)[0]

            for winning_player in winning_players:
                winning_player.change_elo_after_game(self, losing_squad.elo, is_winner=True)

            for losing_player in losing_players:
                losing_player.change_elo_after_game(self, winning_squad.elo, is_winner=False)

            winning_squad.change_elo_after_game(self, losing_squad.elo, is_winner=True)
            losing_squad.change_elo_after_game(self, winning_squad.elo, is_winner=False)

        self.winner = winning_team
        self.loser = losing_team
        losing_team_elo = losing_team.elo
        winning_team_elo = winning_team.elo
        self.winner_delta = winning_team.change_elo_after_game(losing_team_elo, is_winner=True)
        self.loser_delta = losing_team.change_elo_after_game(winning_team_elo, is_winner=False)
        self.is_completed = 1
        self.save()

    def delete_game(self):
        # resets any relevant ELO changes to players and teams, deletes related lineup records, and deletes the game entry itself

        for lineup in self.lineup:
            lineup.player.set_elo_from_delta(lineup.elo_change * -1)
            lineup.player.save()
            lineup.delete_instance()

        for squadgame in self.squadgame:
            squadgame.squad.set_elo_from_delta(squadgame.elo_change * -1)
            squadgame.squad.save()
            squadgame.delete_instance()

        self.winner.set_elo_from_delta(self.winner_delta * -1)
        self.loser.set_elo_from_delta(self.loser_delta * -1)

        self.winner.save()
        self.loser.save()

        self.delete_instance()


class Player(BaseModel):
    discord_name = CharField(unique=False, constraints=[SQL('COLLATE NOCASE')])
    discord_id = IntegerField(unique=True, null=False)
    elo = IntegerField(default=1000)
    team = ForeignKeyField(Team, null=True, backref='player')
    polytopia_id = CharField(null=True, constraints=[SQL('COLLATE NOCASE')])
    polytopia_name = CharField(null=True, constraints=[SQL('COLLATE NOCASE')])

    def return_elo_delta(self, game):
        try:
            game_lineup = Lineup.get(Lineup.game == game, Lineup.player == self)
            return game_lineup.elo_change
        except DoesNotExist:
            return None

    def set_elo_from_delta(self, elo_delta):
        self.elo += elo_delta
        return self.elo

    def change_elo_after_game(self, game, opponent_elo, is_winner):
        game_lineup = Lineup.get(Lineup.game == game, Lineup.player == self)
        print('Squad opponent elo: {}'.format(opponent_elo))

        max_elo_delta = 75
        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - self.elo) / 400))), 3)

        if is_winner is True:
            new_elo = round(self.elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(self.elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - self.elo)
        print('Player chance of winning: {} opponent elo:{} current ELO {}, new elo {}, elo_delta {}'.format(chance_of_winning, opponent_elo, self.elo, new_elo, elo_delta))

        self.elo = int(self.elo + elo_delta)
        game_lineup.elo_change = elo_delta
        game_lineup.save()
        self.save()

        return elo_delta

    def get_record(self):
        wins = Lineup.select().join(Game).where(Lineup.game.winner == Lineup.team, Lineup.player == self).count()
        losses = Lineup.select().join(Game).where(Lineup.game.loser == Lineup.team, Lineup.player == self).count()
        return (wins, losses)


class Tribe(BaseModel):
    name = CharField(unique=True, null=False, constraints=[SQL('COLLATE NOCASE')])
    emoji = CharField(null=True)


class Lineup(BaseModel): # Connect Players to Games
    game = ForeignKeyField(Game, null=False, backref='lineup')
    player = ForeignKeyField(Player, null=False, backref='lineup')
    team = ForeignKeyField(Team, null=False, backref='lineup')
    tribe = ForeignKeyField(Tribe, null=True, backref='lineup')
    elo_change = IntegerField(default=0)


class Squad(BaseModel):
    elo = IntegerField(default=1000)

    def change_elo_after_game(self, game, opponent_elo, is_winner):
        squadgame = SquadGame.get(SquadGame.game == game, SquadGame.squad == self)
        print('Squad opponent elo: {}'.format(opponent_elo))

        max_elo_delta = 75
        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - self.elo) / 400))), 3)

        if is_winner is True:
            new_elo = round(self.elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(self.elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - self.elo)
        print('Squad chance of winning: {} opponent elo:{} current ELO {}, new elo {}, elo_delta {}'.format(chance_of_winning, opponent_elo, self.elo, new_elo, elo_delta))

        self.elo = int(self.elo + elo_delta)
        squadgame.elo_change = elo_delta
        squadgame.save()
        self.save()

        return elo_delta

    def set_elo_from_delta(self, elo_delta):
        self.elo += elo_delta
        return self.elo

    def get_names(self):
        member_names = [member.player.discord_name for member in self.squadmembers]
        return member_names

    def get_record(self):
        wins = SquadGame.select().join(Game).where((SquadGame.game.winner == SquadGame.team) & (SquadGame.squad == self)).count()
        losses = SquadGame.select().join(Game).where((SquadGame.game.loser == SquadGame.team) & (SquadGame.squad == self)).count()
        return (wins, losses)



    def get_matching_squad(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns squad with exactly the same participating players. See https://stackoverflow.com/q/52010522/1281743
        query = Squad.select().join(SquadMember).group_by(SquadMember.squad).having(
            (fn.SUM(SquadMember.player.in_(player_list)) == len(player_list)) & (fn.SUM(SquadMember.player.not_in(player_list)) == 0)
            )
        return query

    def upsert_squad(player_list, game, team):

        squads = Squad.get_matching_squad(player_list)

        if len(squads) == 0:
            # Insert new squad based on this combination of players
            sq = Squad.create(elo=calc_squad_elo(player_list))
            for p in player_list:
                SquadMember.create(player=p, squad=sq)
            SquadGame.create(game=game, squad=sq, team=team)
            return sq
        else:
            # Update existing squad with new game
            SquadGame.create(game=game, squad=squads[0], team=team)
            return squads[0]


class SquadMember(BaseModel):
    player = ForeignKeyField(Player, null=False)
    squad = ForeignKeyField(Squad, null=False, backref='squadmembers')


class SquadGame(BaseModel):
    game = ForeignKeyField(Game, null=False, backref='squadgame')
    squad = ForeignKeyField(Squad, null=False, backref='squadgame')
    team = ForeignKeyField(Team, null=False, backref='squadgame')
    elo_change = IntegerField(default=0)

