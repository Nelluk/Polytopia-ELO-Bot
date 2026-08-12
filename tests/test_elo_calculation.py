"""Characterization tests for the retained legacy ELO calculations.

These tests deliberately lock down observable numeric behavior.  They are not
an invitation to clean up or rebalance the formulas during modernization.
"""

from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


models = import_offline_runtime('modules.models')


def _record(*, elo, completed_games):
    record = SimpleNamespace(
        id=101,
        elo=elo,
        elo_max=elo,
        elo_alltime=elo,
        elo_max_alltime=elo,
        elo_moonrise=elo,
        elo_max_moonrise=elo,
        saved=0,
    )
    record.completed_game_count = mock.Mock(return_value=completed_games)
    record.save = lambda: setattr(record, 'saved', record.saved + 1)
    return record


def _lineup(*, elo=1000, completed_games=0, discord_id=91_001):
    member = _record(elo=elo, completed_games=completed_games)
    member.discord_id = discord_id
    player = _record(elo=elo, completed_games=completed_games)
    player.discord_member = member
    lineup = SimpleNamespace(
        id=301,
        game=SimpleNamespace(id=201, guild_id=401),
        player=player,
        elo_change_player=0,
        elo_change_discordmember=0,
        elo_change_player_alltime=0,
        elo_change_discordmember_alltime=0,
        elo_change_player_moonrise=0,
        elo_change_discordmember_moonrise=0,
        elo_after_game=None,
        elo_after_game_global=None,
        elo_after_game_alltime=None,
        elo_after_game_global_alltime=None,
        elo_after_game_moonrise=None,
        elo_after_game_global_moonrise=None,
        saved=0,
    )
    lineup.save = lambda: setattr(lineup, 'saved', lineup.saved + 1)
    return lineup


class WinChanceCharacterizationTests(unittest.TestCase):
    def test_two_side_logistic_vectors_are_stable(self):
        vectors = (
            (1000, 1000, 0.500),
            (1200, 1000, 0.760),
            (1000, 1200, 0.240),
            (1400, 1000, 0.909),
        )

        for own_elo, opponent_elo, expected in vectors:
            with self.subTest(own_elo=own_elo, opponent_elo=opponent_elo):
                self.assertEqual(
                    models.GameSide.calc_win_chance(own_elo, opponent_elo),
                    expected,
                )

    def test_uneven_side_handicap_vectors_preserve_both_versions(self):
        side = SimpleNamespace(lineup=(object(),))
        vectors = (
            # A 1200 solo side against a 1000 average opponent gets one
            # synthetic partner.  Version 2 reduced the legacy handicap.
            (1, 1200, 1000, 1, 1000),
            (1, 1200, 1000, 2, 1050),
            # Equal-sized sides are unchanged regardless of formula version.
            (0, 1175, 1300, 1, 1175),
            (0, 1175, 1300, 2, 1175),
        )

        for missing, own, opponents, version, expected in vectors:
            with self.subTest(version=version, missing=missing):
                self.assertEqual(
                    models.GameSide.adjusted_elo(
                        side, missing, own, opponents, version
                    ),
                    expected,
                )

    def test_multiside_probabilities_preserve_legacy_normalization(self):
        class Side:
            lineup = (object(),)

            def adjusted_elo(
                self, missing_players, own_elo, opponent_elos, calc_version
            ):
                return models.GameSide.adjusted_elo(
                    self,
                    missing_players,
                    own_elo,
                    opponent_elos,
                    calc_version,
                )

        sides = (Side(), Side(), Side())
        self.assertEqual(
            models.Game.get_side_win_chances(
                1, sides, (1200, 1100, 900), calc_version=2
            ),
            [0.556, 0.313, 0.131],
        )


class RatingDeltaCharacterizationTests(unittest.TestCase):
    def test_team_delta_uses_constant_32_point_factor(self):
        vectors = (
            (0.500, True, 16),
            (0.500, False, -16),
            (0.760, True, 8),
            (0.760, False, -24),
        )

        for chance, is_winner, expected in vectors:
            with self.subTest(chance=chance, is_winner=is_winner):
                self.assertEqual(
                    models.Team.change_elo_after_game(
                        SimpleNamespace(), chance, is_winner
                    ),
                    expected,
                )

    def test_squad_delta_changes_after_six_completed_games(self):
        vectors = (
            (5, True, 25, 1025),
            (5, False, -25, 975),
            (6, True, 16, 1016),
            (6, False, -16, 984),
        )

        for completed_games, is_winner, expected_delta, expected_elo in vectors:
            squad = SimpleNamespace(id=501, elo=1000, saved=0)
            squad.completed_game_count = mock.Mock(
                return_value=completed_games
            )
            squad.save = lambda: setattr(squad, 'saved', squad.saved + 1)
            with self.subTest(
                completed_games=completed_games, is_winner=is_winner
            ):
                self.assertEqual(
                    models.Squad.change_elo_after_game(
                        squad, 0.500, is_winner
                    ),
                    expected_delta,
                )
                self.assertEqual(squad.elo, expected_elo)
                self.assertEqual(squad.saved, 1)

    def test_player_provisional_factor_and_low_rating_boost_are_stable(self):
        vectors = (
            # completed, elo, winner, expected delta
            (0, 1200, True, 38),
            (0, 1200, False, -38),
            (6, 1200, True, 25),
            (11, 1200, True, 16),
            (11, 900, True, 25),
            (11, 900, False, -7),
        )

        for completed, elo, is_winner, expected_delta in vectors:
            lineup = _lineup(elo=elo, completed_games=completed)
            with self.subTest(
                completed=completed, elo=elo, is_winner=is_winner
            ), mock.patch.object(
                models.db, 'atomic', return_value=nullcontext()
            ), mock.patch.object(
                models.settings,
                'servers_included_in_global_lb',
                return_value=[lineup.game.guild_id],
            ):
                models.Lineup.change_elo_after_game(
                    lineup,
                    chance_of_winning=0.500,
                    is_winner=is_winner,
                    moonrise=True,
                )

            self.assertEqual(lineup.elo_change_player_moonrise, expected_delta)
            self.assertEqual(
                lineup.player.elo_moonrise, elo + expected_delta
            )
            self.assertEqual(
                lineup.elo_after_game_moonrise, elo + expected_delta
            )

    def test_player_field_selection_distinguishes_all_three_eras(self):
        vectors = (
            (
                {'alltime': False, 'moonrise': False},
                'elo',
                'elo_change_player',
                'elo_after_game',
            ),
            (
                {'alltime': True, 'moonrise': False},
                'elo_alltime',
                'elo_change_player_alltime',
                'elo_after_game_alltime',
            ),
            (
                {'alltime': False, 'moonrise': True},
                'elo_moonrise',
                'elo_change_player_moonrise',
                'elo_after_game_moonrise',
            ),
        )

        for kwargs, elo_field, change_field, after_field in vectors:
            lineup = _lineup(elo=1200, completed_games=11)
            with self.subTest(elo_field=elo_field), mock.patch.object(
                models.db, 'atomic', return_value=nullcontext()
            ):
                models.Lineup.change_elo_after_game(
                    lineup,
                    chance_of_winning=0.500,
                    is_winner=True,
                    **kwargs,
                )

            self.assertEqual(getattr(lineup.player, elo_field), 1216)
            self.assertEqual(getattr(lineup, change_field), 16)
            self.assertEqual(getattr(lineup, after_field), 1216)

    def test_global_rating_is_skipped_for_excluded_guild(self):
        lineup = _lineup(elo=1000, completed_games=0)
        with mock.patch.object(
            models.settings,
            'servers_included_in_global_lb',
            return_value=[],
        ), mock.patch.object(models.db, 'atomic') as atomic:
            models.Lineup.change_elo_after_game(
                lineup,
                chance_of_winning=0.500,
                is_winner=True,
                by_discord_member=True,
            )

        self.assertEqual(lineup.player.discord_member.elo_moonrise, 1000)
        self.assertEqual(lineup.elo_change_discordmember_moonrise, 0)
        atomic.assert_not_called()

    def test_bot_account_rating_is_never_changed(self):
        lineup = _lineup(elo=1000, completed_games=0, discord_id=91_999)
        with mock.patch.object(
            models.settings,
            'bot_id',
            lineup.player.discord_member.discord_id,
        ), mock.patch.object(models.db, 'atomic') as atomic:
            models.Lineup.change_elo_after_game(
                lineup,
                chance_of_winning=0.500,
                is_winner=True,
            )

        self.assertEqual(lineup.player.elo_moonrise, 1000)
        self.assertEqual(lineup.elo_change_player_moonrise, 0)
        atomic.assert_not_called()


if __name__ == '__main__':
    unittest.main()
