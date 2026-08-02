"""Focused offline coverage for the unified game-deletion boundary."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_deletion_workers')
service = import_offline_runtime('modules.game_deletion')
games = import_offline_runtime('modules.games')


def request(*, requester_id=101, staff=False, mod=False):
    return workers.DeletionRequest(
        game_id=77,
        guild_id=10,
        requester_id=requester_id,
        requester_name='Requester',
        requester_description='**Requester** (`101`)',
        requester_is_staff=staff,
        requester_is_mod=mod,
        prefix='!',
        invoked_with='delete',
    )


def plan(state=workers.PENDING, *, game_id=77):
    return workers.DeletionEffectPlan(
        game_id=game_id,
        guild_id=10,
        state=state,
        mentions=('<@202>',),
        public_message=(
            'Deleting unfilled open game 77\nNotifying players: <@202>'
            if state == workers.PENDING
            else 'Game with ID 77 has been deleted and team/player ELO '
                 'changes have been reverted, if applicable.\n'
                 'Notifying players: <@202>'
        ),
    )


def result(state=workers.PENDING):
    return service.DeletionResult(
        game_id=77,
        state=state,
        recalculated=state != workers.PENDING,
        effect_plan=plan(state),
    )


def classification(state, *, host_id=101, registered=True):
    return workers.DeletionClassification(
        game_id=77,
        guild_id=10,
        state=state,
        host_id=host_id,
        host_name='Host',
        registered=registered,
    )


class PendingGame:
    def __init__(self, *, capacity=2, players=1, expiration=None):
        self.id = 77
        self.guild_id = 10
        self.is_pending = True
        self.is_completed = False
        self.expiration = expiration or datetime.datetime.now() + datetime.timedelta(days=1)
        self.size = [capacity]
        self.deleted = False
        self.lineups = [SimpleNamespace(deleted=False)] * players
        self.gamesides = [SimpleNamespace(deleted=False)]
        host_member = SimpleNamespace(discord_id=101)
        self.host = SimpleNamespace(
            discord_member=host_member,
            name='Host',
        )

    @property
    def lineup(self):
        return tuple(self.lineups)

    def capacity(self):
        return len(self.lineups), self.size[0]

    def mentions(self):
        return ['<@202>']

    def is_hosted_by(self, discord_id):
        return discord_id == 101, self.host


class DeletionClassificationTests(unittest.TestCase):
    def test_request_is_frozen_and_captures_only_primitive_values(self):
        member = SimpleNamespace(
            id=101,
            name='requester',
            display_name='Requester',
            guild=SimpleNamespace(id=10),
        )
        with mock.patch.object(service.settings, 'is_staff', return_value=True), mock.patch.object(
            service.settings,
            'is_mod',
            return_value=False,
        ), mock.patch.object(
            service.models.GameLog,
            'member_string',
            return_value='**Requester** (`101`)',
        ):
            captured = service.build_request(
                game_id=77,
                member=member,
                guild_id=10,
                prefix='!',
                invoked_with='delgame',
            )

        self.assertEqual(captured.requester_id, 101)
        self.assertTrue(captured.requester_is_staff)
        self.assertEqual(captured.invoked_with, 'delgame')
        self.assertTrue(all(
            isinstance(value, (int, str, bool))
            for value in captured.__dict__.values()
        ))
        with self.assertRaises(FrozenInstanceError):
            captured.game_id = 88

    def test_classification_preserves_pending_open_full_and_expired_state(self):
        class Database:
            def connection_context(self):
                return mock.MagicMock()

        class GameTable:
            game = None

            @staticmethod
            def get_by_id(_game_id):
                return GameTable.game

        class DiscordMember:
            @staticmethod
            def get_or_none(**_kwargs):
                return object()

        models = SimpleNamespace(
            db=Database(),
            Game=GameTable,
            DiscordMember=DiscordMember,
        )
        states = []
        with mock.patch.object(workers, 'models', models):
            for game in (
                PendingGame(capacity=2, players=1),
                PendingGame(capacity=2, players=2),
                PendingGame(
                    capacity=2,
                    players=1,
                    expiration=datetime.datetime.now() - datetime.timedelta(hours=1),
                ),
            ):
                GameTable.game = game
                states.append(
                    workers.classify_game_deletion(request())
                )

        self.assertEqual([item.state for item in states], [
            workers.PENDING,
            workers.PENDING,
            workers.PENDING,
        ])
        self.assertEqual([item.host_id for item in states], [101, 101, 101])

    def test_effect_plan_distinguishes_pending_full_and_unfilled(self):
        with mock.patch.object(workers, '_snapshot_for_game', return_value=None):
            open_plan = workers.build_effect_plan(
                PendingGame(capacity=2, players=1),
                guild_id=10,
                state=workers.PENDING,
            )
            full_plan = workers.build_effect_plan(
                PendingGame(capacity=2, players=2),
                guild_id=10,
                state=workers.PENDING,
            )
        self.assertEqual(open_plan.pending_filled, 'unfilled')
        self.assertEqual(full_plan.pending_filled, 'full')
        self.assertIn('Deleting full open game', full_plan.public_message)

    def test_shared_permission_policy_has_host_staff_and_mod_parity(self):
        service._authorize(request(), classification(workers.PENDING, host_id=101))
        service._authorize(
            request(requester_id=999, staff=True),
            classification(workers.PENDING, host_id=101),
        )
        with self.assertRaisesRegex(
            workers.GameDeletionValidationError,
            'Only the game host',
        ):
            service._authorize(
                request(requester_id=999),
                classification(workers.PENDING, host_id=101),
            )
        service._authorize(
            request(requester_id=999, mod=True),
            classification(workers.IN_PROGRESS, host_id=101),
        )
        with self.assertRaisesRegex(
            workers.GameDeletionValidationError,
            'Only server mods',
        ):
            service._authorize(
                request(requester_id=999),
                classification(workers.COMPLETED, host_id=101),
            )
        with self.assertRaisesRegex(
            workers.GameDeletionValidationError,
            'requires bot registration',
        ):
            service._authorize(
                request(),
                classification(workers.PENDING, registered=False),
            )


class PendingDeletionTransactionTests(unittest.IsolatedAsyncioTestCase):
    def _models(self, game, database, logs):
        class GameTable:
            @staticmethod
            def get_by_id(game_id):
                if game_id != game.id:
                    raise peewee.DoesNotExist()
                return game

        class GameLog:
            @staticmethod
            def write(**kwargs):
                logs.append(kwargs)

        class DiscordMember:
            @staticmethod
            def get_or_none(**_kwargs):
                return object()

        return SimpleNamespace(
            db=database,
            Game=GameTable,
            GameLog=GameLog,
            DiscordMember=DiscordMember,
        )

    def _database(self, game, records, logs):
        database = self
        database.connection_opened = 0
        database.connection_closed = 0
        database.commits = 0
        database.rollbacks = 0

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                return database

            def __exit__(self, *_args):
                database.connection_closed += 1

        class Atomic(AbstractContextManager):
            def __enter__(self):
                self.snapshot = (
                    game.deleted,
                    [record.deleted for record in records],
                    list(logs),
                )
                return self

            def __exit__(self, exc_type, _value, _traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                game.deleted = self.snapshot[0]
                for record, deleted in zip(records, self.snapshot[1]):
                    record.deleted = deleted
                logs[:] = self.snapshot[2]
                return False

        database.connection_context = lambda: Connection()
        database.atomic = lambda: Atomic()
        return database

    def _wire_records(self, game, *, failing=False):
        records = []

        class Record:
            def __init__(self, should_fail=False):
                self.deleted = False
                self.should_fail = should_fail
                records.append(self)

            def delete_instance(self):
                self.deleted = True
                if self.should_fail:
                    raise peewee.OperationalError('simulated pending delete failure')

        game.lineups = [Record()]
        game.gamesides = [Record(should_fail=failing)]

        def delete_game():
            game.deleted = True

        game.delete_instance = delete_game
        return records

    def test_pending_worker_uses_primitive_request_and_one_atomic_audit_transaction(self):
        game = PendingGame()
        logs = []
        records = self._wire_records(game)
        database = self._database(game, records, logs)
        models = self._models(game, database, logs)
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            'build_effect_plan',
            return_value=plan(),
        ):
            output = workers.delete_pending_game(request())

        self.assertEqual(output.game_id, 77)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertTrue(game.deleted)
        self.assertTrue(all(record.deleted for record in records))
        self.assertEqual(len(logs), 1)
        self.assertEqual(inspect.signature(workers.delete_pending_game).parameters.keys(), {'request'})
        self.assertFalse(inspect.iscoroutinefunction(workers.delete_pending_game))
        self.assertNotIn('discord', workers.__dict__)

    def test_pending_worker_rolls_back_records_and_audit_on_failure(self):
        game = PendingGame()
        logs = []
        records = self._wire_records(game, failing=True)
        database = self._database(game, records, logs)
        models = self._models(game, database, logs)
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            'build_effect_plan',
            return_value=plan(),
        ), self.assertRaises(peewee.OperationalError):
            workers.delete_pending_game(request())

        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertFalse(game.deleted)
        self.assertTrue(all(not record.deleted for record in records))
        self.assertEqual(logs, [])

    async def _wait_for_thread_start(self, started):
        for _ in range(500):
            if started.is_set():
                return
            await asyncio.sleep(0.001)
        self.fail('pending deletion worker did not start')

    async def test_pending_coordinator_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()
        coordinator = workers.game_open_workers.PendingGameCoordinator()

        def slow_delete(_request):
            started.set()
            release.wait(timeout=2)
            return workers.PendingDeletionResult(
                game_id=77,
                recalculated=False,
                effect_plan=plan(),
            )

        try:
            with mock.patch.object(
                workers,
                'delete_pending_game',
                side_effect=slow_delete,
            ), mock.patch.object(
                workers.game_open_workers,
                'pending_game_coordinator',
                coordinator,
            ):
                task = asyncio.create_task(
                    workers.run_pending_game_deletion(request())
                )
                await self._wait_for_thread_start(started)
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
                release.set()
                output = await task
        finally:
            release.set()
            coordinator.executor.shutdown(wait=True)

        self.assertEqual(output.game_id, 77)


class DeletionServiceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_and_started_completed_routes_share_one_service(self):
        pending_worker = mock.AsyncMock(
            return_value=workers.PendingDeletionResult(
                game_id=77,
                recalculated=False,
                effect_plan=plan(),
            )
        )
        elo_runner = mock.AsyncMock(return_value=result(workers.COMPLETED))
        with mock.patch.object(
            service,
            'authorize_delete',
            new=mock.AsyncMock(return_value=classification(workers.PENDING)),
        ) as authorize, mock.patch.object(
            service.game_deletion_workers,
            'run_pending_game_deletion',
            pending_worker,
        ), mock.patch.object(
            service,
            '_run_elo_deletion',
            elo_runner,
        ):
            pending_result = await service.delete_game(request())

        self.assertEqual(pending_result.state, workers.PENDING)
        pending_worker.assert_awaited_once_with(mock.ANY)
        elo_runner.assert_not_awaited()
        authorize.assert_awaited_once()

        for state in (workers.IN_PROGRESS, workers.COMPLETED):
            with self.subTest(state=state), mock.patch.object(
                service,
                'authorize_delete',
                new=mock.AsyncMock(return_value=classification(state)),
            ), mock.patch.object(
                service,
                '_run_elo_deletion',
                new=mock.AsyncMock(return_value=result(state)),
            ) as run_elo:
                routed = await service.delete_game(request(mod=True))
                self.assertEqual(routed.state, state)
                run_elo.assert_awaited_once()

    async def test_pending_state_change_reclassifies_before_elo_handoff(self):
        authorize = mock.AsyncMock(
            side_effect=[
                classification(workers.PENDING),
                classification(workers.IN_PROGRESS),
            ]
        )
        pending_worker = mock.AsyncMock(
            side_effect=workers.PendingGameDeletionStateChanged('started')
        )
        elo_runner = mock.AsyncMock(return_value=result(workers.IN_PROGRESS))
        with mock.patch.object(service, 'authorize_delete', authorize), mock.patch.object(
            service.game_deletion_workers,
            'run_pending_game_deletion',
            pending_worker,
        ), mock.patch.object(service, '_run_elo_deletion', elo_runner):
            output = await service.delete_game(request(mod=True))

        self.assertEqual(output.state, workers.IN_PROGRESS)
        self.assertEqual(authorize.await_count, 2)
        elo_runner.assert_awaited_once()

    async def test_elo_worker_pending_rejection_rechecks_and_uses_pending_coordinator(self):
        authorize = mock.AsyncMock(
            side_effect=[
                classification(workers.IN_PROGRESS),
                classification(workers.PENDING),
            ]
        )
        pending_result = workers.PendingDeletionResult(
            game_id=77,
            recalculated=False,
            effect_plan=plan(),
        )
        pending_worker = mock.AsyncMock(return_value=pending_result)
        elo_runner = mock.AsyncMock(
            side_effect=service.elo_workers.DeleteValidationError('pending')
        )
        with mock.patch.object(service, 'authorize_delete', authorize), mock.patch.object(
            service.game_deletion_workers,
            'run_pending_game_deletion',
            pending_worker,
        ), mock.patch.object(service, '_run_elo_deletion', elo_runner):
            output = await service.delete_game(request(staff=True))

        self.assertEqual(output.state, workers.PENDING)
        self.assertEqual(authorize.await_count, 2)
        pending_worker.assert_awaited_once()

    async def test_coordinator_conflict_is_not_hidden_or_run_in_parallel(self):
        active = SimpleNamespace(operation='delete_game', game_id=88)
        conflict = service.EloJobConflict(active)
        with mock.patch.object(
            service,
            'authorize_delete',
            new=mock.AsyncMock(return_value=classification(workers.COMPLETED)),
        ), mock.patch.object(
            service,
            '_run_elo_deletion',
            new=mock.AsyncMock(side_effect=conflict),
        ):
            with self.assertRaises(service.EloJobConflict):
                await service.delete_game(request(mod=True))

class DeletionAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self):
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            guilds=[],
            get_guild=lambda _guild_id: None,
            get_channel=lambda _channel_id: None,
        )
        return cog

    async def test_prefix_alias_calls_service_and_publishes_only_after_success(self):
        cog = self._cog()
        context = SimpleNamespace(
            interaction=None,
            author=SimpleNamespace(id=101),
            guild=SimpleNamespace(id=10),
            prefix='!',
            invoked_with='delgame',
            send=mock.AsyncMock(),
        )
        shared_request = request()
        committed = result(workers.PENDING)
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'delete'
        )
        with mock.patch.object(
            games.game_deletion,
            'build_request',
            return_value=shared_request,
        ) as build_request, mock.patch.object(
            games.game_deletion,
            'delete_game',
            new=mock.AsyncMock(return_value=committed),
        ) as delete_game, mock.patch.object(
            games.game_deletion,
            'publish_result',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, context, 77)

        self.assertEqual(build_request.call_args.kwargs['invoked_with'], 'delgame')
        delete_game.assert_awaited_once_with(shared_request)
        publish.assert_awaited_once()

    async def test_native_delete_adapter_uses_same_service_boundary(self):
        cog = self._cog()
        prefix_command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'delete'
        )
        cog.delete = SimpleNamespace(
            can_run=mock.AsyncMock(return_value=True),
            callback=prefix_command.callback,
        )
        context = SimpleNamespace(
            interaction=None,
            author=SimpleNamespace(id=101),
            guild=SimpleNamespace(id=10),
            prefix='!',
            invoked_with='delete',
            send=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild=context.guild,
            user=context.author,
        )
        shared_request = request()
        committed = result(workers.COMPLETED)
        slash = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('delete')
        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='!',
        ), mock.patch.object(
            games.game_deletion,
            'build_request',
            return_value=shared_request,
        ) as build_request, mock.patch.object(
            games.game_deletion,
            'delete_game',
            new=mock.AsyncMock(return_value=committed),
        ) as delete_game, mock.patch.object(
            games.game_deletion,
            'publish_result',
            new=mock.AsyncMock(),
        ) as publish:
            await slash.callback(cog, interaction, 77)

        self.assertEqual(build_request.call_args.kwargs['invoked_with'], 'delete')
        delete_game.assert_awaited_once_with(shared_request)
        publish.assert_awaited_once()

    async def test_database_failure_skips_all_post_commit_discord_effects(self):
        cog = self._cog()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            user=SimpleNamespace(id=101),
            channel_id=500,
            response=SimpleNamespace(is_done=lambda: True),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog._native_pending_game_channel_allowed = mock.AsyncMock(return_value=True)
        with mock.patch.object(
            games.game_deletion,
            'build_request',
            return_value=request(),
        ), mock.patch.object(
            games.game_deletion,
            'delete_game',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('db failed')),
        ), mock.patch.object(
            games.game_deletion,
            'publish_result',
            new=mock.AsyncMock(),
        ) as publish:
            self.assertFalse(await cog._pending_card_delete(
                interaction,
                game_id=77,
                prefix='!',
            ))

        publish.assert_not_awaited()
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])

    async def test_pending_card_prepare_rejects_unauthorized_host_without_confirmation(self):
        cog = self._cog()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            user=SimpleNamespace(id=999),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog._native_pending_game_channel_allowed = mock.AsyncMock(return_value=True)
        with mock.patch.object(
            games.game_deletion,
            'build_request',
            return_value=request(requester_id=999),
        ), mock.patch.object(
            games.game_deletion,
            'authorize_delete',
            new=mock.AsyncMock(
                side_effect=workers.GameDeletionValidationError(
                    'Only the game host or server staff can do this.'
                )
            ),
        ) as authorize:
            self.assertFalse(await cog._pending_card_delete_prepare(
                interaction,
                game_id=77,
                prefix='!',
            ))

        authorize.assert_awaited_once()
        self.assertIn('Only the game host', interaction.followup.send.await_args.args[0])
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])


class PostCommitEffectTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_effects_run_after_commit_in_broadcast_then_output_order(self):
        events = []

        class BroadcastMessage:
            content = 'join game'

            async def edit(self, **_kwargs):
                events.append('broadcast-edit')

            async def clear_reactions(self):
                events.append('clear-reactions')

        class BroadcastChannel:
            async def fetch_message(self, _message_id):
                return BroadcastMessage()

        message_plan = workers.DeletionEffectPlan(
            game_id=77,
            guild_id=10,
            state=workers.PENDING,
            mentions=('<@202>',),
            public_message='Deleting full open game 77\nNotifying players: <@202>',
            broadcast_targets=(workers.DeletionBroadcastTarget(20, 30),),
        )
        output = service.DeletionResult(
            game_id=77,
            state=workers.PENDING,
            recalculated=False,
            effect_plan=message_plan,
        )

        async def send(content):
            events.append(('send', content))

        bot = SimpleNamespace(get_channel=lambda _channel_id: BroadcastChannel())
        await service.publish_result(
            output,
            send=send,
            guild=SimpleNamespace(id=10),
            bot=bot,
            prefix='!',
        )
        self.assertEqual(events, [
            'broadcast-edit',
            'clear-reactions',
            ('send', message_plan.public_message),
        ])

    async def test_started_effects_keep_announcement_output_channel_order_after_commit(self):
        events = []
        started_plan = workers.DeletionEffectPlan(
            game_id=77,
            guild_id=10,
            state=workers.COMPLETED,
            mentions=(),
            public_message='deleted',
            channel_targets=(workers.DeletionChannelTarget(10, 55),),
        )
        output = service.DeletionResult(
            game_id=77,
            state=workers.COMPLETED,
            recalculated=True,
            effect_plan=started_plan,
        )

        async def announcement(*_args, **_kwargs):
            events.append('announcement')

        async def channels(*_args, **_kwargs):
            events.append('channel')

        async def send(content):
            events.append(('send', content))

        with mock.patch.object(service, '_publish_announcement', announcement), mock.patch.object(
            service,
            '_publish_channels',
            channels,
        ):
            await service.publish_result(
                output,
                send=send,
                guild=SimpleNamespace(id=10),
                bot=SimpleNamespace(),
                prefix='!',
            )

        self.assertEqual(events, [
            'announcement',
            ('send', 'deleted'),
            'channel',
        ])


if __name__ == '__main__':
    unittest.main()
