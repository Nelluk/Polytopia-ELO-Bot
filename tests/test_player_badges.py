"""Focused offline coverage for P12.1 PolyChampions player badges."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
from playhouse.postgres_ext import ArrayField

from tests.test_newgame_worker import import_offline_runtime


models = import_offline_runtime('modules.models')
workers = import_offline_runtime('modules.league_badges_workers')
service = import_offline_runtime('modules.league_badges')
views = import_offline_runtime('modules.league_badges_views')
league = import_offline_runtime('modules.league')
player_views = import_offline_runtime('modules.player_views')


class FakeDatabase:
    def __init__(self, players=()):
        self.players = tuple(players)
        self.logs = []
        self.atomic_entries = 0
        self.rollbacks = 0

    def connection_context(self):
        return _Context()

    def atomic(self):
        database = self
        before = [list(player.badges) for player in self.players]

        class Atomic(AbstractContextManager):
            def __enter__(self):
                database.atomic_entries += 1

            def __exit__(self, exc_type, *_args):
                if exc_type is not None:
                    database.rollbacks += 1
                    for player, badges in zip(database.players, before, strict=True):
                        player.badges = list(badges)
                    database.logs.clear()
                return False

        return Atomic()


class _Context(AbstractContextManager):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def player(player_id, discord_id, badges=(), *, fail_save=False):
    value = SimpleNamespace(
        id=player_id,
        name=f'Player {player_id}',
        badges=list(badges),
        discord_member=SimpleNamespace(
            discord_id=discord_id,
            name=f'Member {discord_id}',
        ),
    )

    def save(*, only):
        if fail_save:
            raise RuntimeError('injected save failure')
        value.saved_only = tuple(only)

    value.save = save
    return value


def request(*, operation='add', targets=(10,), badge='🏆 Champion'):
    return workers.BadgeMutationRequest(
        operation=operation,
        guild_id=300,
        actor_discord_id=9,
        actor_display_label='Mod',
        recipient_discord_ids=tuple(targets),
        badge=badge,
    )


class ModelAndRegistrationTests(unittest.TestCase):
    def test_player_badges_is_reviewed_non_null_text_array(self):
        field = models.Player.badges
        self.assertIsInstance(field, ArrayField)
        self.assertFalse(field.null)
        self.assertIs(field.default, list)
        self.assertEqual(field.field_type, 'TEXT')
        self.assertEqual(len(field.constraints), 1)

    def test_add_and_remove_are_under_existing_league_root_only(self):
        root = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )
        badge = root.get_command('badge')
        self.assertIsInstance(badge, discord.app_commands.Group)
        self.assertEqual({command.name for command in badge.commands}, {'add', 'remove'})
        self.assertIsNone(next((
            command for command in league.league.__cog_app_commands__
            if command.name == 'badge'
        ), None))
        prefix_names = {command.name for command in league.league.__cog_commands__}
        self.assertFalse(prefix_names.intersection({'badge', 'badges', 'ptrophies'}))

    def test_worker_requests_and_results_are_immutable_primitives(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.guild_id = 2


class ValidationAndAutocompleteTests(unittest.TestCase):
    def test_add_normalizes_whitespace_and_builds_exact_display(self):
        draft = service.normalize_add('  Season\t12   Champion ', ' 🏆 ')
        self.assertEqual(draft.badge, '🏆 Season 12 Champion')
        self.assertEqual(draft.label, 'Season 12 Champion')
        self.assertEqual(
            service.safe_badge('<:gold_cup:123456789> **Champion** @everyone'),
            '<:gold_cup:123456789> \\*\\*Champion\\*\\* '
            '@\u200beveryone',
        )

    def test_validation_rejects_empty_control_and_length_boundaries(self):
        for label in ('', '   ', 'bad\nlabel', 'x' * 101):
            with self.subTest(label=label):
                with self.assertRaises(workers.BadgeValidationError):
                    service.normalize_add(label, None)
        with self.assertRaises(workers.BadgeValidationError):
            service.normalize_add('ok', 'x' * 101)
        with self.assertRaises(workers.BadgeValidationError):
            service.normalize_remove('bad\nvalue')

    def test_emoji_autocomplete_is_fixed_then_cached_and_bounded(self):
        emojis = tuple(
            SimpleNamespace(name=f'cup{index}', __str__=lambda self: '<:cup:1>')
            for index in range(30)
        )
        # SimpleNamespace special methods are class-resolved, so use tiny
        # concrete values for deterministic render strings.
        class Emoji:
            def __init__(self, index):
                self.name = f'cup{index}'

            def __str__(self):
                return f'<:cup{self.name[3:]}:{100 + int(self.name[3:])}>'

        values = service.emoji_autocomplete(
            SimpleNamespace(emojis=tuple(Emoji(i) for i in range(30))), ''
        )
        self.assertEqual(tuple(value for _name, value in values[:6]), service.UNICODE_EMOJI_CHOICES)
        self.assertEqual(len(values), 25)
        filtered = service.emoji_autocomplete(
            SimpleNamespace(emojis=(Emoji(1), Emoji(2))), 'cup2'
        )
        self.assertEqual(
            filtered,
            tuple((value, value) for value in service.UNICODE_EMOJI_CHOICES)
            + ((':cup2:', '<:cup2:102>'),),
        )

    def test_access_is_mod_only_and_league_scoped(self):
        member = SimpleNamespace(id=9)
        with mock.patch.object(
            service.league_user_commands, 'league_scope', return_value=False
        ), mock.patch.object(service.settings, 'is_mod', return_value=True):
            self.assertIn('league server', service.access_error(member, 300))
        with mock.patch.object(
            service.league_user_commands, 'league_scope', return_value=True
        ), mock.patch.object(service.settings, 'is_mod', return_value=False):
            self.assertIn('Mod', service.access_error(member, 300))


class WorkerTransactionTests(unittest.TestCase):
    def run_worker(self, players, value, *, audit_error=None):
        database = FakeDatabase(players)

        def write(**kwargs):
            if audit_error:
                raise audit_error
            database.logs.append(kwargs)

        with mock.patch.object(workers.models, 'db', database), \
                mock.patch.object(workers, '_allowed_guild_id', return_value=300), \
                mock.patch.object(workers, '_target_rows', return_value=tuple(players)), \
                mock.patch.object(workers.models.GameLog, 'write', side_effect=write):
            result = workers.mutate_badges(value)
        return database, result

    def test_mixed_add_is_atomic_idempotent_and_writes_one_audit(self):
        first = player(1, 10)
        second = player(2, 11, ('🏆 Champion',))
        database, result = self.run_worker(
            (first, second), request(targets=(10, 11))
        )
        self.assertEqual(first.badges, ['🏆 Champion'])
        self.assertEqual(second.badges, ['🏆 Champion'])
        self.assertEqual((result.changed_count, result.unchanged_count), (1, 1))
        self.assertEqual(len(database.logs), 1)
        database, repeated = self.run_worker(
            (first, second), request(targets=(10, 11))
        )
        self.assertEqual(repeated.changed_count, 0)
        self.assertEqual(database.logs, [])

    def test_remove_is_casefold_idempotent_and_preserves_order(self):
        target = player(1, 10, ('First', '🏆 Champion', 'Last'))
        database, result = self.run_worker(
            (target,), request(operation='remove', badge='🏆 champion')
        )
        self.assertEqual(target.badges, ['First', 'Last'])
        self.assertEqual(result.changed_count, 1)
        self.assertEqual(len(database.logs), 1)

    def test_duplicate_targets_and_badge_cap_fail_before_write(self):
        target = player(1, 10, tuple(f'Badge {i}' for i in range(100)))
        with mock.patch.object(workers, '_allowed_guild_id', return_value=300):
            with self.assertRaises(workers.BadgeValidationError):
                workers._validate_request(request(targets=(10, 10)))
        with self.assertRaises(workers.BadgeValidationError):
            self.run_worker((target,), request())
        self.assertEqual(len(target.badges), 100)

    def test_save_and_audit_failures_roll_back_every_array(self):
        for failure in ('save', 'audit'):
            first = player(1, 10)
            second = player(2, 11, fail_save=failure == 'save')
            database = FakeDatabase((first, second))
            with mock.patch.object(workers.models, 'db', database), \
                    mock.patch.object(workers, '_allowed_guild_id', return_value=300), \
                    mock.patch.object(workers, '_target_rows', return_value=(first, second)), \
                    mock.patch.object(
                        workers.models.GameLog,
                        'write',
                        side_effect=(RuntimeError('audit') if failure == 'audit' else None),
                    ):
                with self.assertRaises(RuntimeError):
                    workers.mutate_badges(request(targets=(10, 11)))
            self.assertEqual(first.badges, [])
            self.assertEqual(second.badges, [])
            self.assertEqual(database.rollbacks, 1)

    def test_source_enforces_deterministic_lock_and_explicit_save(self):
        source = (Path(__file__).resolve().parents[1] / 'modules/league_badges_workers.py').read_text()
        self.assertIn('.order_by(models.Player.id)', source)
        self.assertIn('query = query.for_update()', source)
        self.assertIn('save(only=[models.Player.badges])', source)
        self.assertIn('with models.db.atomic():', source)


class AsyncAndPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_worker_keeps_unrelated_coroutine_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(timeout=2)
            return 'done'

        task = asyncio.create_task(workers._run(slow))
        while not started.is_set():
            await asyncio.sleep(0.001)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_later(0.01, heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 0.2)
        release.set()
        self.assertEqual(await task, 'done')

    async def test_cancellation_drains_submitted_worker(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow():
            started.set()
            release.wait(timeout=2)
            finished.set()

        task = asyncio.create_task(workers._run(slow))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_publication_failure_is_explicitly_post_commit(self):
        result = workers.BadgeMutationResult(
            operation='add', guild_id=300, actor_discord_id=9,
            actor_display_label='Mod', badge='🏆 Champion',
            recipients=(workers.BadgeRecipientResult(10, 'P1', True),),
            audit_written=True,
        )
        interaction = SimpleNamespace(
            channel=SimpleNamespace(send=mock.AsyncMock(side_effect=RuntimeError('discord')))
        )
        with self.assertRaises(service.BadgePublicationError):
            await service.publish_result(interaction, result)
        self.assertIn('committed', service.BadgePublicationError.__doc__.lower())


class ProfileAndOperatorContractTests(unittest.TestCase):
    def test_profile_source_has_six_overview_ten_page_and_other_guild_gate(self):
        root = Path(__file__).resolve().parents[1]
        view_source = (root / 'modules/player_views.py').read_text()
        worker_source = (root / 'modules/player_workers.py').read_text()
        self.assertIn('snapshot.badges[:6]', view_source)
        self.assertIn('BADGE_PAGE_SIZE = 10', view_source)
        self.assertIn("settings.server_ids['polychampions']", worker_source)
        self.assertIn('_profile_badges(player, request.guild_id)', worker_source)

    def test_operator_fingerprints_and_preservation_include_badges(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / 'modules/operator_player_migration_workers.py').read_text()
        deletion = (root / 'modules/operator_player_deletion_workers.py').read_text()
        self.assertIn("'trophies', 'badges', 'is_banned'", migration)
        self.assertIn('_merged_badges(', migration)
        self.assertIn("'trophies', 'badges', 'is_banned'", deletion)
        self.assertIn('badge_count=', deletion)


class DraftViewTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self):
        return views.BadgeDraftWorkspace(
            requester_id=9,
            guild_id=300,
            draft=service.BadgeDraft('add', '🏆 Champion', 'Champion', '🏆'),
            runner=mock.AsyncMock(),
        )

    def test_selector_is_one_to_twenty_five_and_payload_is_bounded(self):
        view = self.make_view()
        selector = next(
            value for value in view.walk_children()
            if isinstance(value, discord.ui.UserSelect)
        )
        self.assertEqual((selector.min_values, selector.max_values), (1, 25))
        self.assertLessEqual(view.total_children_count, 40)
        self.assertEqual(view.to_components()[0]['type'], 17)

    async def test_non_requester_cannot_control_draft(self):
        view = self.make_view()
        response = SimpleNamespace(send_message=mock.AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10), response=response, guild_id=300
        )
        self.assertFalse(await view.authorize(interaction))
        response.send_message.assert_awaited_once()

    async def test_cancel_and_timeout_are_terminal_with_rerun_path(self):
        view = self.make_view()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=9),
            guild_id=300,
            response=SimpleNamespace(
                is_done=lambda: False,
                edit_message=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
        )
        with mock.patch.object(view, '_ready', return_value=True):
            await view._cancel(interaction)
        self.assertTrue(view.terminal)
        self.assertIn('No badge was changed', view.status)

        expired = self.make_view()
        expired.message = SimpleNamespace(edit=mock.AsyncMock())
        await expired.on_timeout()
        self.assertTrue(expired.terminal)
        self.assertIn('/league badge add', expired.status)


if __name__ == '__main__':
    unittest.main()
