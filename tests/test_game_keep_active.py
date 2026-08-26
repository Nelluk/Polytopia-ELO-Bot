"""Focused offline coverage for P5.17 game keep-active."""

import datetime
from contextlib import AbstractContextManager
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime

workers = import_offline_runtime('modules.game_keep_active_workers')
purge = import_offline_runtime('modules.incomplete_game_purge_workers')
views = import_offline_runtime('modules.game_keep_active_views')
migration = import_offline_runtime('modules.game_keep_active_migration')
production = import_offline_runtime('modules.game_keep_active_production_migration')
service = import_offline_runtime('modules.game_keep_active')
warning_service = import_offline_runtime('modules.incomplete_game_purge')

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
    def test_discovery_filter_excludes_old_rows_with_future_deferrals(self):
        expression = purge.discovery_deadline_filter(
            purge.models.Game, TODAY,
        )
        sql, _params = purge.models.Game.select().where(expression).sql()
        self.assertIn('cleanup_deferred_until', sql)
        self.assertIn('IS NULL', sql.upper())
        self.assertIn('"cleanup_deferred_until" <=', sql)

    def test_original_cycle_accepts_legacy_channel_marker(self):
        current_query = mock.MagicMock()
        current_query.where.return_value.exists.return_value = True
        game_log = mock.MagicMock()
        game_log.select.return_value = current_query
        models = SimpleNamespace(GameLog=game_log)
        with mock.patch.object(purge, 'models', models):
            self.assertTrue(purge._warning_was_recorded(
                game_id=77,
                guild_id=10,
                channel_id=500,
                protected_through=TODAY,
                allow_legacy=True,
            ))
        # The legacy lookup is still a channel-scoped lookup; a new deadline
        # must be represented by the exact deadline-bearing marker instead.
        self.assertEqual(game_log.select.call_count, 1)

    def test_deferred_cycle_does_not_accept_old_channel_marker(self):
        current_query = mock.MagicMock()
        current_query.where.return_value.exists.return_value = False
        game_log = mock.MagicMock()
        game_log.select.return_value = current_query
        models = SimpleNamespace(GameLog=game_log)
        with mock.patch.object(purge, 'models', models):
            self.assertFalse(purge._warning_was_recorded(
                game_id=77,
                guild_id=10,
                channel_id=500,
                protected_through=TODAY + datetime.timedelta(days=30),
                allow_legacy=False,
            ))
        # Deferred cycles intentionally skip the second, generic legacy query.
        self.assertEqual(game_log.select.call_count, 1)

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

    def test_stale_warning_deadline_skips_without_audit(self):
        loaded = game()
        database = Database()
        game_log = SimpleNamespace(write=mock.Mock())
        models = SimpleNamespace(db=database, GameLog=game_log)
        request = purge.WarningDeliveryRequest(
            77, 10, 10, 900, TODAY, TODAY + datetime.timedelta(days=30),
        )
        with mock.patch.object(purge, 'models', models), mock.patch.object(
            purge, '_load_game', return_value=loaded,
        ):
            result = purge.record_warning_delivery(request)
        self.assertEqual(result.status, purge.SKIPPED_STATE_CHANGED)
        game_log.write.assert_not_called()


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

    def test_dynamic_callback_passes_game_and_frozen_deadline(self):
        button = views.KeepActiveButton(discord.ui.Button(
            label='Keep active for 30 days',
            custom_id='keep-active:77:2026-08-09',
        ))
        interaction = SimpleNamespace()
        with mock.patch.object(
            service, 'run_button', new=mock.AsyncMock(),
        ) as callback:
            import asyncio
            asyncio.run(button.callback(interaction))
        callback.assert_awaited_once()
        self.assertEqual(callback.await_args.kwargs['game_id'], 77)
        self.assertEqual(
            callback.await_args.kwargs['protected_through'], TODAY,
        )

    def test_public_success_follows_private_defer_and_private_ack(self):
        events = []
        class Response:
            def __init__(self): self.done = False
            def is_done(self): return self.done
            async def defer(self, **kwargs): events.append(('defer', kwargs)); self.done = True
        class Channel:
            async def send(self, content): events.append(('public', content))
        class Followup:
            async def send(self, content, **kwargs): events.append(('followup', content, kwargs))
        user = SimpleNamespace(id=42)
        interaction = SimpleNamespace(
            response=Response(), followup=Followup(), channel=Channel(),
            user=user, guild_id=10, channel_id=900,
        )
        result = workers.KeepActiveResult(77, 10, TODAY, TODAY + datetime.timedelta(days=30), 42)
        with mock.patch.object(service, 'run', new=mock.AsyncMock(return_value=result)), \
             mock.patch('settings.is_staff', return_value=False):
            import asyncio
            asyncio.run(service.run_button(interaction, game_id=77, protected_through=TODAY))
        self.assertEqual(events[0][0], 'defer')
        self.assertEqual(events[1][0], 'public')
        self.assertEqual(events[2][0], 'followup')
        self.assertTrue(events[2][2]['ephemeral'])

    def test_warning_publication_includes_persistent_view(self):
        warning = purge.WarningPlan(
            77, 60, 'deadline 2026-08-09',
            (purge.WarningTarget(10, 900, ()),), TODAY,
        )
        sent = []
        class Channel:
            async def send(self, content, **kwargs): sent.append((content, kwargs))
        guild = SimpleNamespace(id=10, get_channel=lambda _id: Channel())
        bot = SimpleNamespace(get_guild=lambda _id: guild)
        with mock.patch.object(
            purge, 'run_record_warning_delivery',
            new=mock.AsyncMock(return_value=purge.WarningDeliveryResult(
                77, 900, purge.WARNING_RECORDED,
            )),
        ):
            import asyncio
            asyncio.run(warning_service.publish_warning_plan(
                warning, bot=bot, source_guild_id=10, as_of=TODAY,
                staff_channel=None,
            ))
        self.assertIsInstance(sent[0][1]['view'], views.KeepActiveView)

    def test_committed_publication_failure_is_terminal(self):
        events = []
        class Response:
            def is_done(self): return True
        class Channel:
            async def send(self, _content): raise RuntimeError('gone')
        class Followup:
            async def send(self, content, **kwargs): events.append((content, kwargs))
        interaction = SimpleNamespace(
            response=Response(), followup=Followup(), channel=Channel(),
        )
        result = workers.KeepActiveResult(77, 10, TODAY, TODAY + datetime.timedelta(days=30), 42)
        import asyncio
        asyncio.run(service._publish_success(interaction, result))
        self.assertIn('committed', events[0][0])
        self.assertIn('do not retry', events[0][0])

    def test_production_policy_and_confirmation_are_fail_closed(self):
        target = production.MigrationTarget('development', 'polytopia_dev', 'role')
        with self.assertRaises(production.MigrationSafetyError):
            production.validate_target(target)
        with self.assertRaises(production.MigrationSafetyError):
            production.validate_apply_confirmation('wrong')
        self.assertEqual(
            production.plan_migration(None).statements,
            migration.plan_migration(None).statements,
        )


if __name__ == '__main__':
    unittest.main()
