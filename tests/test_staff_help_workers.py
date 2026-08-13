"""Focused bounded game-context coverage for ``/staffhelp``."""

from types import SimpleNamespace
import unittest
from unittest import mock

import peewee

from modules import staff_help_workers


class StaffHelpWorkerTests(unittest.TestCase):
    def test_channel_first_lookup_returns_only_bounded_routing_context(self):
        game = SimpleNamespace(
            id=42500,
            guild_id=201,
            name='A useful game',
            is_pending=False,
            is_completed=True,
            is_confirmed=False,
        )
        with mock.patch.object(
                staff_help_workers.models.db,
                'connection_context') as connection, mock.patch.object(
                    staff_help_workers.models.Game,
                    'by_channel_or_arg',
                    return_value=game,
                ) as lookup:
            result = staff_help_workers.find_related_game(
                channel_id=300,
                game_id=42500,
            )

        connection.assert_called_once_with()
        lookup.assert_called_once_with(chan_id=300, arg='42500')
        self.assertEqual(result, staff_help_workers.RelatedGame(
            game_id=42500,
            guild_id=201,
            name='A useful game',
            status='Unconfirmed',
        ))

    def test_missing_or_unavailable_database_context_does_not_disable_help(self):
        with mock.patch.object(
                staff_help_workers.models.db,
                'connection_context'), mock.patch.object(
                    staff_help_workers.models.Game,
                    'by_channel_or_arg',
                    side_effect=staff_help_workers.exceptions.NoMatches('missing'),
                ):
            self.assertIsNone(staff_help_workers.find_related_game(
                channel_id=300,
                game_id=None,
            ))

        with mock.patch.object(
                staff_help_workers.models.db,
                'connection_context',
                side_effect=peewee.OperationalError('unavailable'),
                ):
            self.assertIsNone(staff_help_workers.find_related_game(
                channel_id=300,
                game_id=None,
            ))


if __name__ == '__main__':
    unittest.main()
