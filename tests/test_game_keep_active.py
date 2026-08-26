"""Focused offline coverage for P5.17 game keep-active."""

import datetime
from contextlib import AbstractContextManager
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime

workers = import_offline_runtime('modules.game_keep_active_workers')
purge = import_offline_runtime('modules.incomplete_game_purge_workers')
views = import_offline_runtime('modules.game_keep_active_views')
migration = import_offline_runtime('modules.game_keep_active_migration')

TODAY = datetime.date(2026, 8, 9)


def game(*, count=2, date=TODAY - datetime.timedelta(days=60), deferred=None,
         pending=False, completed=False, confirmed=False, season=False):
    member = SimpleNamespace(discord_id=42)
    player = SimpleNamespace(discord_member=member)
    return SimpleNamespace(
        id=77, guild_id=10, date=date, cleanup_deferred_until=deferred,
        is_ranked=False, is_pending=pending, is_completed=completed,
        is_confirmed=confirmed, league_season=3 if season else None,
        lineup=tuple(SimpleNamespace(player=player) for _ in range(count)),
        is_season_game=lambda: (3, 1, False) if season else (),
        gamesides=(), game_chan=None,
    )


class Database:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        db = self
        class C(AbstractContextManager):
            def __enter__(self): return self
            def __exit__(self, *_): return False
        return C()

    def atomic(self):
        db = self
        class A(AbstractContextManager):
            def __enter__(self): return self
            def __exit__(self, exc_type, *_):
                if exc_type: db.rollbacks += 1
                else: db.commits += 1
                return False
        return A()


class KeepActivePolicyTests(unittest.TestCase):
    def test_effective_deadline_and_strict_boundaries(self):
        loaded = game()
        self.assertEqual(purge.effective_protected_through(loaded), TODAY)
        self.assertEqual(purge.classify_game(loaded, as_of=TODAY, player_count=2), 'warning')
        self.assertEqual(purge.classify_game(loaded, as_of=TODAY + datetime.timedelta(days=1), player_count=2), purge.PURGED)
        loaded.cleanup_deferred_until = TODAY + datetime.timedelta(days=30)
        self.assertIsNone(purge.classify_game(loaded, as_of=TODAY, player_count=2))

    def test_early_renewal_rejected(self):
        loaded = game(date=TODAY - datetime.timedelta(days=1))
        request = workers.KeepActiveRequest(77, 42, '<@42>', 10, as_of=TODAY)
        database = Database()
        models = SimpleNamespace(
            db=database,
            Game=SimpleNamespace(select=lambda: mock.Mock()),
        )
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers, '_load_locked_game', return_value=loaded,
        ):
            with self.assertRaises(workers.KeepActiveValidationError):
                workers.keep_game_active(request)

    def test_commit_writes_deferred_date_and_owner_audit(self):
        loaded = game()
        loaded.save = mock.Mock()
        logs = []
        database = Database()
        models = SimpleNamespace(
            db=database,
            Game=SimpleNamespace(
                select=lambda: mock.Mock(),
                cleanup_deferred_until=object(),
            ),
            GameLog=SimpleNamespace(write=lambda **value: logs.append(value)),
        )
        request = workers.KeepActiveRequest(77, 42, '<@42>', 11, as_of=TODAY)
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers, '_load_locked_game', return_value=loaded,
        ):
            result = workers.keep_game_active(request)
        self.assertEqual(result.new_protected_through, TODAY + datetime.timedelta(days=30))
        loaded.save.assert_called_once()
        self.assertEqual(logs[0]['guild_id'], 10)
        self.assertEqual(database.commits, 1)

    def test_participant_and_staff_owning_guild_rules(self):
        loaded = game()
        with mock.patch.object(workers, '_load_locked_game', return_value=loaded):
            with self.assertRaises(workers.KeepActivePermissionError):
                workers._authorize(workers.KeepActiveRequest(77, 999, 'x', 10), loaded)
            with self.assertRaises(workers.KeepActivePermissionError):
                workers._authorize(workers.KeepActiveRequest(77, 9, 'x', 11, actor_is_staff=True), loaded)


class KeepActiveSurfaceTests(unittest.TestCase):
    def test_dynamic_button_and_warning_view_are_persistent(self):
        view = views.KeepActiveView(77, TODAY)
        self.assertEqual(view.children[0].custom_id, 'keep-active:77:2026-08-09')
        self.assertTrue(view.children[0].is_persistent())
        self.assertIn('deadline', views.CUSTOM_ID_TEMPLATE)

    def test_migration_plan_is_nullable_date_and_connection_free(self):
        plan = migration.plan_migration(None)
        self.assertIn('DATE NULL', plan.statements[0])
        self.assertTrue(migration.column_matches_contract(
            migration.ColumnState('date', 'date', 'YES', None)
        ))


if __name__ == '__main__':
    unittest.main()
