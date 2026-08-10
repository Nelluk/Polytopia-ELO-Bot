"""Focused offline coverage for P9.5 owner-only player deletion."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import time
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.operator_player_deletion_workers')
service = import_offline_runtime('modules.operator_player_deletion')
views = import_offline_runtime('modules.operator_player_deletion_views')
administration = import_offline_runtime('modules.administration')


def preview(**overrides):
    values = dict(
        guild_id=300,
        target_id=200,
        target_name='Orphan Name',
        account_metadata=('canonical Polytopia name',),
        global_rating_summary=('elo=1100',),
        players=(workers.PlayerDeletionGuildPreview(
            player_id=1,
            guild_id=300,
            name='Orphan Name',
            nick='Orphan',
            team_id=10,
            rating_summary=('elo=1050',),
            trophies_present=True,
            is_banned=False,
            squad_memberships=1,
            house_preferences=1,
            lineups=0,
            hosted_games=0,
            bid_references=0,
        ),),
        player_count=1,
        squad_membership_count=1,
        house_preference_count=1,
        blockers=(),
        warnings=('Non-default metadata will be discarded.',),
        fingerprint='abc',
    )
    values.update(overrides)
    return workers.PlayerDeletionPreview(**values)


def commit_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_description='**Owner** (`10`)',
        target_id=200,
        expected_fingerprint='abc',
        confirmation_text='DELETE 200',
    )
    values.update(overrides)
    return workers.PlayerDeletionCommitRequest(**values)


class FakeField:
    def __eq__(self, _other):
        return self

    def in_(self, _values):
        return self


class FakeDeleteQuery:
    def __init__(self, database, key):
        self.database = database
        self.key = key

    def where(self, *_conditions):
        return self

    def execute(self):
        self.database.events.append(f'delete-{self.key}')
        value = self.database.rows[self.key]
        self.database.rows[self.key] = 0
        return value


def fake_table(key, *, field_names=()):
    attributes = {name: FakeField() for name in field_names}

    @classmethod
    def delete(cls):
        return FakeDeleteQuery(cls.database, key)

    attributes['delete'] = delete
    attributes['database'] = None
    return type(f'Fake{key.title()}', (), attributes)


class FakeDatabase:
    def __init__(self):
        self.rows = {
            'squads': 1,
            'preferences': 1,
            'players': 1,
            'member': 1,
        }
        self.events = []
        self.logs = []
        self.opened = 0
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_audit = False

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                database.events.append('connection-open')

            def __exit__(self, *_args):
                database.closed += 1
                database.events.append('connection-close')

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                self.rows = dict(database.rows)
                self.logs = list(database.logs)
                database.events.append('atomic-open')

            def __exit__(self, exc_type, *_args):
                if exc_type is None:
                    database.commits += 1
                    database.events.append('commit')
                else:
                    database.rows = self.rows
                    database.logs = self.logs
                    database.rollbacks += 1
                    database.events.append('rollback')
                return False

        return Atomic()


class FakeGameLog:
    database = None

    @classmethod
    def write(cls, **kwargs):
        cls.database.events.append('audit')
        if cls.database.fail_audit:
            raise peewee.OperationalError('forced audit rollback')
        cls.database.logs.append(kwargs)


class PlayerDeletionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.tables = {
            'SquadMember': fake_table('squads', field_names=('player',)),
            'PlayerHousePreference': fake_table(
                'preferences', field_names=('player',)
            ),
            'Player': fake_table('players', field_names=('id',)),
            'DiscordMember': fake_table('member', field_names=('id',)),
        }
        for table in self.tables.values():
            table.database = self.database
        FakeGameLog.database = self.database
        self.patches = ExitStack()
        self.patches.enter_context(
            mock.patch.object(workers.models, 'db', self.database)
        )
        for name, table in self.tables.items():
            self.patches.enter_context(
                mock.patch.object(workers.models, name, table)
            )
        self.patches.enter_context(
            mock.patch.object(workers.models, 'GameLog', FakeGameLog)
        )
        self.patches.enter_context(
            mock.patch.object(workers.settings, 'owner_id', 10)
        )
        self.patches.enter_context(
            mock.patch.object(workers.settings, 'superuser_ids', (10, 20, 30))
        )
        self.patches.enter_context(
            mock.patch.object(workers.settings, 'bot_id', 40)
        )
        self.patches.enter_context(
            mock.patch.object(workers.settings, 'bot_id_beta', 50)
        )
        self.patches.enter_context(
            mock.patch.object(
                workers.runtime_config,
                'LEGACY_PRODUCTION_BOT_ID',
                60,
            )
        )
        self.patches.enter_context(
            mock.patch.object(workers, '_lock_graph')
        )
        graph = workers._Graph(
            preview=preview(),
            target_member_id=99,
            player_ids=(1,),
        )
        self.build_graph = self.patches.enter_context(
            mock.patch.object(workers, '_build_graph', return_value=graph)
        )
        self.addCleanup(self.patches.close)

    def test_requests_and_results_are_frozen_primitives(self):
        request = commit_request()
        with self.assertRaises(FrozenInstanceError):
            request.target_id = 999
        self.assertNotIn('Member', repr(request))
        self.assertNotIn('Model', repr(request))

    def test_owner_authorization_and_protected_targets_are_authoritative(self):
        with self.assertRaises(workers.PlayerDeletionPermissionError):
            workers.delete_player(commit_request(requester_id=20))
        for protected in (10, 20, 30, 40, 50, 60):
            with self.subTest(protected=protected):
                with self.assertRaises(workers.PlayerDeletionValidationError):
                    workers.delete_player(commit_request(target_id=protected))
        self.build_graph.assert_not_called()

    def test_exact_typed_confirmation_is_revalidated_in_worker(self):
        with self.assertRaisesRegex(
            workers.PlayerDeletionValidationError,
            'Type exactly',
        ):
            workers.delete_player(commit_request(confirmation_text='delete 200'))
        self.build_graph.assert_not_called()

    def test_blockers_and_stale_graph_fail_before_any_delete(self):
        blocked = preview(blockers=('A Lineup remains.',))
        self.build_graph.return_value = workers._Graph(
            preview=blocked,
            target_member_id=99,
            player_ids=(1,),
        )
        with self.assertRaisesRegex(
            workers.PlayerDeletionValidationError,
            'Lineup',
        ):
            workers.delete_player(commit_request())
        self.assertNotIn('delete-squads', self.database.events)

        self.build_graph.return_value = workers._Graph(
            preview=preview(fingerprint='changed'),
            target_member_id=99,
            player_ids=(1,),
        )
        with self.assertRaises(workers.PlayerDeletionStaleError):
            workers.delete_player(commit_request())
        self.assertNotIn('delete-squads', self.database.events)

    def test_explicit_graph_deletes_and_audits_in_one_transaction(self):
        result = workers.delete_player(commit_request())
        self.assertEqual(
            (
                result.players_deleted,
                result.squad_memberships_deleted,
                result.house_preferences_deleted,
            ),
            (1, 1, 1),
        )
        self.assertEqual(
            self.database.events,
            [
                'connection-open',
                'atomic-open',
                'delete-squads',
                'delete-preferences',
                'delete-players',
                'delete-member',
                'audit',
                'commit',
                'connection-close',
            ],
        )
        self.assertEqual(self.database.logs[0]['guild_id'], 300)
        self.assertIn('**Owner**', self.database.logs[0]['message'])
        self.assertEqual((self.database.opened, self.database.closed), (1, 1))

    def test_count_mismatch_and_audit_failure_roll_back_everything(self):
        self.database.rows['squads'] = 0
        with self.assertRaises(workers.PlayerDeletionStaleError):
            workers.delete_player(commit_request())
        self.assertEqual(self.database.rows['players'], 1)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertNotIn('audit', self.database.events)

        self.database.rows['squads'] = 1
        self.database.fail_audit = True
        with self.assertRaisesRegex(peewee.OperationalError, 'forced audit'):
            workers.delete_player(commit_request())
        self.assertEqual(
            self.database.rows,
            {'squads': 1, 'preferences': 1, 'players': 1, 'member': 1},
        )
        self.assertEqual(self.database.logs, [])

    def test_slow_commit_keeps_loop_responsive_and_cancellation_drains(self):
        result = workers.PlayerDeletionResult(
            guild_id=300,
            target_id=200,
            target_name='Orphan',
            players_deleted=1,
            squad_memberships_deleted=1,
            house_preferences_deleted=1,
        )

        def slow_commit(_request):
            time.sleep(0.05)
            return result

        async def exercise():
            with mock.patch.object(
                workers,
                'delete_player',
                side_effect=slow_commit,
            ):
                task = asyncio.create_task(workers.run_commit(commit_request()))
                await asyncio.sleep(0)
                started = time.monotonic()
                await asyncio.sleep(0.005)
                self.assertLess(time.monotonic() - started, 0.04)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1.0)

        asyncio.run(exercise())


class PlayerDeletionViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_blockers_disable_delete_and_controls_are_requester_bound(self):
        view = views.PlayerDeletionPreviewView(
            requester_id=10,
            preview=preview(blockers=('A Lineup remains.',)),
            confirmer=mock.AsyncMock(),
        )
        buttons = [
            item for item in view.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        self.assertTrue(
            next(item for item in buttons if item.label == 'Delete identity').disabled
        )
        denial = mock.AsyncMock()
        allowed = await view.interaction_check(SimpleNamespace(
            user=SimpleNamespace(id=11),
            response=SimpleNamespace(send_message=denial),
        ))
        self.assertFalse(allowed)
        denial.assert_awaited_once()
        self.assertEqual(view.to_components()[0]['type'], 17)

        modal = views.PlayerDeletionConfirmationModal(view)
        components = modal.to_components()
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]['type'], 1)
        self.assertEqual(components[0]['components'][0]['type'], 4)

    async def test_confirmation_must_match_exactly_before_worker(self):
        confirmer = mock.AsyncMock()
        view = views.PlayerDeletionPreviewView(
            requester_id=10,
            preview=preview(),
            confirmer=confirmer,
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        await view.submit_confirmation(interaction, 'delete 200')
        confirmer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            'Type exactly `DELETE 200`. No database changes were made.',
            ephemeral=True,
        )

    async def test_precommit_failure_restores_delete_but_success_is_terminal(self):
        message = SimpleNamespace(edit=mock.AsyncMock())
        failed = views.PlayerDeletionPreviewView(
            requester_id=10,
            preview=preview(),
            confirmer=mock.AsyncMock(
                side_effect=workers.PlayerDeletionStaleError('Reload preview.')
            ),
        )
        failed.message = message
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await failed.submit_confirmation(interaction, 'DELETE 200')
        self.assertFalse(failed.finished)
        self.assertFalse(failed.busy)
        self.assertEqual(failed.status, 'Reload preview.')

        successful = views.PlayerDeletionPreviewView(
            requester_id=10,
            preview=preview(),
            confirmer=mock.AsyncMock(),
        )
        successful.message = SimpleNamespace(edit=mock.AsyncMock())
        await successful.submit_confirmation(interaction, 'DELETE 200')
        self.assertTrue(successful.finished)
        buttons = [
            item for item in successful.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        self.assertTrue(all(item.disabled for item in buttons))


class PlayerDeletionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_success_is_actor_attributed_after_private_cleanup(self):
        events = []

        async def clear():
            events.append('clear')

        async def send(content, **kwargs):
            events.append(('public', content, kwargs))

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10, display_name='Owner', name='Owner'),
            delete_original_response=clear,
            channel=SimpleNamespace(send=send),
        )
        result = workers.PlayerDeletionResult(
            guild_id=300,
            target_id=200,
            target_name='Orphan',
            players_deleted=2,
            squad_memberships_deleted=1,
            house_preferences_deleted=1,
        )
        await service.publish_result(interaction, result)
        self.assertEqual(events[0], 'clear')
        self.assertIn('Owner', events[1][1])
        self.assertIn('Orphan', events[1][1])
        self.assertIn('2 guild Player', events[1][1])


class PlayerDeletionAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(
            administration.administration
        )
        self.operator = next(
            command for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.command = self.operator.get_command('player').get_command('delete')

    def test_exact_nested_shape_and_prefix_retirement(self):
        self.assertEqual(
            [(parameter.name, parameter.required) for parameter in self.command.parameters],
            [('player_id', True)],
        )
        prefix_names = {
            name
            for command in administration.administration.__cog_commands__
            for name in (command.name, *command.aliases)
        }
        self.assertNotIn('delete_player', prefix_names)
        self.assertNotIn('delplayer', prefix_names)

    async def test_configured_non_owner_is_denied_privately_before_defer(self):
        response = SimpleNamespace(
            send_message=mock.AsyncMock(),
            defer=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=20),
            response=response,
        )
        with mock.patch.object(administration.settings, 'owner_id', 10):
            await self.command.callback(
                self.cog,
                interaction,
                '123456789012345678',
            )
        response.defer.assert_not_awaited()
        response.send_message.assert_awaited_once()

    async def test_valid_request_defers_before_preview_worker(self):
        events = []

        async def defer(**kwargs):
            events.append(('defer', kwargs))

        async def run_preview(_request):
            events.append('worker')
            return preview()

        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=10, display_name='Owner', name='Owner'),
            response=SimpleNamespace(defer=defer, send_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            original_response=mock.AsyncMock(return_value=SimpleNamespace()),
        )
        with mock.patch.object(administration.settings, 'owner_id', 10), \
                mock.patch.object(
                    administration.operator_player_deletion_workers,
                    'run_preview',
                    new=run_preview,
                ):
            await self.command.callback(
                self.cog,
                interaction,
                '123456789012345678',
            )
        self.assertEqual(events[:2], [('defer', {'ephemeral': True}), 'worker'])
        interaction.edit_original_response.assert_awaited_once()
