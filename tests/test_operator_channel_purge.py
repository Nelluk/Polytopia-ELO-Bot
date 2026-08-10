"""Focused offline coverage for P9.9 manual channel cleanup."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.operator_channel_purge_workers')
service = import_offline_runtime('modules.operator_channel_purge')
views = import_offline_runtime('modules.operator_channel_purge_views')
administration = import_offline_runtime('modules.administration')
NOW = datetime.datetime(2026, 8, 10, 16, 0, tzinfo=datetime.UTC)


class Predicate:
    def __and__(self, _other):
        return self


class Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, _other):
        return Predicate()

    def is_null(self, _value):
        return Predicate()


class Query:
    def __init__(self, row=None):
        self.row = row

    def select(self, *_args):
        return self

    def join(self, *_args):
        return self

    def where(self, *_args):
        return self

    def for_update(self):
        return self

    def first(self):
        return self.row


class Database:
    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.opens += 1

            def __exit__(self, *_args):
                database.closes += 1

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, *_args):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1

        return Atomic()


def snapshot(channel_id=900, **overrides):
    values = dict(
        channel_id=channel_id,
        name=f'game-{channel_id}',
        category_id=50,
        category_name='Games',
        last_message_id=100,
        last_activity_at=NOW - datetime.timedelta(days=31),
        manageable=True,
        archive_protected=False,
    )
    values.update(overrides)
    return workers.ChannelSnapshot(**values)


def reference(channel_id=900, **overrides):
    values = dict(
        kind=workers.GAME_TARGET,
        record_id=77,
        game_id=77,
        source_guild_id=10,
        channel_id=channel_id,
        game_name='Test Game',
        is_completed=False,
        is_pending=False,
        league_season=None,
        recent_nova=False,
        external=False,
        notice_targets=((10, 901),),
    )
    values.update(overrides)
    return workers.ChannelReference(**values)


def request(mode=workers.STALE, **overrides):
    values = dict(
        guild_id=10,
        requester_id=1,
        mode=mode,
        as_of=NOW,
        guild_channel_count=430,
        configured_category_ids=(50,),
        channels=(snapshot(),),
    )
    values.update(overrides)
    return workers.ManualPurgePreviewRequest(**values)


def preview(mode=workers.STALE, **overrides):
    with mock.patch.object(workers.settings, 'owner_id', 1):
        result = workers.build_manual_purge_preview(
            request(mode), (reference(),)
        )
    values = result.__dict__ | overrides
    return workers.ManualPurgePreview(**values)


class ManualPurgeClassificationTests(unittest.TestCase):
    def build(self, mode, channels, references, **request_overrides):
        with mock.patch.object(workers.settings, 'owner_id', 1):
            return workers.build_manual_purge_preview(
                request(mode, channels=tuple(channels), **request_overrides),
                tuple(references),
            )

    def test_stale_requires_old_or_empty_tracked_channel(self):
        recent = snapshot(901, last_activity_at=NOW - datetime.timedelta(days=1))
        empty = snapshot(902, last_message_id=None, last_activity_at=None)
        result = self.build(
            workers.STALE,
            (snapshot(), recent, empty),
            (reference(), reference(901, record_id=78, game_id=78),
             reference(902, record_id=79, game_id=79)),
        )
        self.assertEqual(
            [row.channel_id for row in result.candidates], [900, 902]
        )

    def test_capacity_requires_threshold_and_central_reference(self):
        result = self.build(
            workers.CAPACITY,
            (snapshot(), snapshot(901)),
            (reference(), reference(
                901, kind=workers.SIDE_TARGET, record_id=88,
            )),
        )
        self.assertEqual([row.channel_id for row in result.candidates], [900])
        self.assertEqual(result.candidates[0].notice_targets, ((10, 901),))
        below = self.build(
            workers.CAPACITY,
            (snapshot(),),
            (reference(),),
            guild_channel_count=workers.CAPACITY_THRESHOLD,
        )
        self.assertEqual(below.candidates, ())

    def test_orphans_are_only_unreferenced_configured_category_channels(self):
        outside = snapshot(901, category_id=60)
        result = self.build(
            workers.ORPHAN,
            (snapshot(), outside, snapshot(902)),
            (reference(902),),
        )
        self.assertEqual([row.channel_id for row in result.candidates], [900])
        self.assertEqual(result.candidates[0].kind, workers.ORPHAN_TARGET)

    def test_missing_mode_returns_only_absent_local_references(self):
        result = self.build(
            workers.MISSING,
            (),
            (reference(), reference(901, external=True)),
        )
        self.assertEqual([row.channel_id for row in result.candidates], [900])
        self.assertTrue(result.candidates[0].missing)

    def test_protections_and_ambiguous_references_fail_closed(self):
        result = self.build(
            workers.STALE,
            (snapshot(), snapshot(901), snapshot(902), snapshot(903)),
            (
                reference(is_completed=True),
                reference(901, league_season=4),
                reference(902, recent_nova=True),
                reference(903, record_id=80, game_id=80),
                reference(903, kind=workers.SIDE_TARGET, record_id=81, game_id=81),
            ),
        )
        self.assertEqual(result.candidates, ())
        self.assertTrue(any('automatic' in row for row in result.exclusions))
        self.assertTrue(any('ambiguous' in row for row in result.exclusions))

    def test_preview_is_frozen_bounded_and_activity_changes_fingerprint(self):
        first = self.build(workers.STALE, (snapshot(),), (reference(),))
        second = self.build(
            workers.STALE,
            (snapshot(last_message_id=101),),
            (reference(),),
        )
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        with self.assertRaises(FrozenInstanceError):
            first.mode = workers.ORPHAN

    def test_worker_revalidates_owner(self):
        with mock.patch.object(workers.settings, 'owner_id', 99):
            with self.assertRaises(workers.ManualChannelPurgeError):
                workers.build_manual_purge_preview(request(), (reference(),))


class ManualPurgeWorkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_reference_clear_and_protected_audit_are_atomic(self):
        database = Database()
        row = SimpleNamespace(game_chan=900, save=mock.Mock())
        game = SimpleNamespace(
            id=Field('id'), guild_id=Field('guild_id'),
            game_chan=Field('game_chan'), select=lambda *_args: Query(row=row),
        )
        logs = SimpleNamespace(write=mock.Mock())
        candidate = preview().candidates[0]
        reconcile_request = workers.ManualPurgeReconcileRequest(
            10, 1, 'Owner (`1`)', candidate,
        )
        with mock.patch.object(workers.settings, 'owner_id', 1), mock.patch.object(
            workers, 'models',
            SimpleNamespace(
                db=database, Game=game, GameSide=object(), GameLog=logs,
            ),
        ):
            result = await workers.run_reconcile_manual_purge(reconcile_request)
        self.assertEqual(result.status, workers.RECONCILED)
        self.assertIsNone(row.game_chan)
        row.save.assert_called_once_with(only=(game.game_chan,))
        self.assertTrue(logs.write.call_args.kwargs['is_protected'])
        self.assertEqual((database.commits, database.rollbacks), (1, 0))
        self.assertEqual((database.opens, database.closes), (1, 1))

    async def test_audit_failure_rolls_back_and_closes_connection(self):
        database = Database()
        row = SimpleNamespace(game_chan=900, save=mock.Mock())
        game = SimpleNamespace(
            id=Field('id'), guild_id=Field('guild_id'),
            game_chan=Field('game_chan'), select=lambda *_args: Query(row=row),
        )
        logs = SimpleNamespace(
            write=mock.Mock(side_effect=peewee.OperationalError('audit failed'))
        )
        with mock.patch.object(workers.settings, 'owner_id', 1), mock.patch.object(
            workers, 'models',
            SimpleNamespace(
                db=database, Game=game, GameSide=object(), GameLog=logs,
            ),
        ):
            with self.assertRaisesRegex(peewee.OperationalError, 'audit failed'):
                await workers.run_reconcile_manual_purge(
                    workers.ManualPurgeReconcileRequest(
                        10, 1, 'Owner (`1`)', preview().candidates[0],
                    )
                )
        self.assertEqual((database.commits, database.rollbacks), (0, 1))
        self.assertEqual((database.opens, database.closes), (1, 1))

    async def test_slow_preview_keeps_loop_responsive_and_cancellation_drains(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return preview()

        with mock.patch.object(
            workers, 'load_manual_purge_preview', side_effect=slow,
        ):
            task = asyncio.create_task(
                workers.run_load_manual_purge_preview(request())
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.002)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_later(0.01, heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.2)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())


class FakeChannel:
    def __init__(self, channel_id=900):
        self.id = channel_id
        self.name = f'game-{channel_id}'
        self.category_id = 50
        self.category = SimpleNamespace(name='Games')
        self.last_message_id = 100
        self.guild = SimpleNamespace(id=10)
        self.delete = mock.AsyncMock()

    def permissions_for(self, _member):
        return SimpleNamespace(manage_channels=True)


def interaction_for(channel):
    guild = SimpleNamespace(
        id=10, me=object(), channels=(channel,) * 430,
        text_channels=(channel,), get_channel=lambda cid: channel if cid == 900 else None,
    )
    bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == 10 else None,
        fetch_channel=mock.AsyncMock(return_value=channel),
    )
    return SimpleNamespace(
        guild=guild,
        guild_id=10,
        user=SimpleNamespace(id=1, display_name='Owner'),
        client=bot,
    )


class ManualPurgeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reauthorization_failure_skips_without_deletion(self):
        channel = FakeChannel()
        interaction = interaction_for(channel)
        accepted = preview()
        with mock.patch.object(service.settings, 'owner_id', 1), mock.patch.object(
            service, 'load_preview', new=mock.AsyncMock(return_value=accepted),
        ), mock.patch.object(
            service.workers, 'run_authorize_manual_purge_candidate',
            new=mock.AsyncMock(return_value=False),
        ):
            outcome = await service.confirm_purge(
                interaction,
                accepted,
                (accepted.candidates[0].key,),
                'PURGE 1',
            )
        self.assertEqual(outcome.skipped_count, 1)
        channel.delete.assert_not_awaited()

    async def test_delete_precedes_exact_reconciliation(self):
        channel = FakeChannel()
        interaction = interaction_for(channel)
        accepted = preview()
        events = []

        async def delete(**_kwargs):
            events.append('delete')
        channel.delete.side_effect = delete

        async def reconcile(_request):
            events.append('reconcile')
            return workers.ManualPurgeReconcileResult(900, workers.RECONCILED)

        with mock.patch.object(service.settings, 'owner_id', 1), mock.patch.object(
            service, 'load_preview', new=mock.AsyncMock(return_value=accepted),
        ), mock.patch.object(
            service.workers, 'run_authorize_manual_purge_candidate',
            new=mock.AsyncMock(return_value=True),
        ), mock.patch.object(
            service.workers, 'run_reconcile_manual_purge', side_effect=reconcile,
        ):
            outcome = await service.confirm_purge(
                interaction,
                accepted,
                (accepted.candidates[0].key,),
                'PURGE 1',
            )
        self.assertEqual(events, ['delete', 'reconcile'])
        self.assertEqual(outcome.state, 'complete')
        self.assertEqual(outcome.reconciled_count, 1)

    async def test_changed_refresh_deletes_nothing(self):
        channel = FakeChannel()
        interaction = interaction_for(channel)
        accepted = preview()
        changed_candidate = accepted.candidates[0]
        changed_candidate = workers.ManualPurgeCandidate(
            **(changed_candidate.__dict__ | {'eligibility_token': 'changed'})
        )
        fresh = workers.ManualPurgePreview(
            **(accepted.__dict__ | {'candidates': (changed_candidate,)})
        )
        with mock.patch.object(service.settings, 'owner_id', 1), mock.patch.object(
            service, 'load_preview', new=mock.AsyncMock(return_value=fresh),
        ):
            outcome = await service.confirm_purge(
                interaction,
                accepted,
                (accepted.candidates[0].key,),
                'PURGE 1',
            )
        self.assertEqual(outcome.state, 'refreshed')
        channel.delete.assert_not_awaited()

    async def test_cancellation_drains_delete_and_reconcile_pair(self):
        channel = FakeChannel()
        interaction = interaction_for(channel)
        accepted = preview()
        started = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def delete(**_kwargs):
            started.set()
            await release.wait()
            events.append('delete')
        channel.delete.side_effect = delete

        async def reconcile(_request):
            events.append('reconcile')
            return workers.ManualPurgeReconcileResult(900, workers.RECONCILED)

        with mock.patch.object(service.settings, 'owner_id', 1), mock.patch.object(
            service, 'load_preview', new=mock.AsyncMock(return_value=accepted),
        ), mock.patch.object(
            service.workers, 'run_authorize_manual_purge_candidate',
            new=mock.AsyncMock(return_value=True),
        ), mock.patch.object(
            service.workers, 'run_reconcile_manual_purge', side_effect=reconcile,
        ):
            task = asyncio.create_task(service.confirm_purge(
                interaction, accepted, (accepted.candidates[0].key,), 'PURGE 1',
            ))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(events, ['delete', 'reconcile'])

    async def test_unexpected_target_failure_does_not_stop_later_target(self):
        channel = FakeChannel()
        interaction = interaction_for(channel)
        accepted = preview()
        first = accepted.candidates[0]
        second = workers.ManualPurgeCandidate(**(
            first.__dict__ | {
                'key': 'game:78:901',
                'channel_id': 901,
                'record_id': 78,
                'game_id': 78,
            }
        ))
        accepted = workers.ManualPurgePreview(**(
            accepted.__dict__ | {'candidates': (first, second,)}
        ))
        processed = []

        async def process(_interaction, candidate):
            processed.append(candidate.channel_id)
            if candidate.channel_id == 900:
                raise RuntimeError('first target failed')
            return 'reconciled', 'done'

        with mock.patch.object(service.settings, 'owner_id', 1), mock.patch.object(
            service, 'load_preview', new=mock.AsyncMock(return_value=accepted),
        ), mock.patch.object(service, '_process_candidate', side_effect=process):
            outcome = await service.confirm_purge(
                interaction,
                accepted,
                (first.key, second.key),
                'PURGE 2',
            )
        self.assertEqual(processed, [900, 901])
        self.assertEqual(outcome.failed_count, 1)
        self.assertEqual(outcome.reconciled_count, 1)

    async def test_same_guild_overlap_is_rejected(self):
        coordinator = service.ManualPurgeCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def first():
            started.set()
            await release.wait()

        task = asyncio.create_task(coordinator.run(10, first))
        await started.wait()
        with self.assertRaisesRegex(
            workers.ManualChannelPurgeError, 'already active'
        ):
            await coordinator.run(10, mock.AsyncMock())
        release.set()
        await task

    def test_discord_service_is_model_free(self):
        source = inspect.getsource(service)
        self.assertNotIn('models.', source)
        self.assertNotIn('Game.select', source)


class ManualPurgeViewAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_workspace_starts_with_no_selected_candidate(self):
        workspace = views.ManualChannelPurgeWorkspace(
            requester_id=1,
            preview=preview(),
            refresher=mock.AsyncMock(),
            confirmer=mock.AsyncMock(),
        )
        self.assertEqual(workspace.selected_keys, set())
        buttons = [
            item for item in workspace.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        review = next(item for item in buttons if item.label == 'Review deletion')
        self.assertTrue(review.disabled)

    def test_exact_nested_shape_and_prefix_retirement(self):
        operator = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        command = operator.get_command('channels').get_command('purge')
        self.assertEqual([item.name for item in command.parameters], ['mode'])
        self.assertEqual(
            {choice.value for choice in command.parameters[0].choices},
            workers.MODES,
        )
        prefix_names = {
            name
            for command in administration.administration.__cog_commands__
            for name in (command.name, *command.aliases)
        }
        self.assertNotIn('purge_game_channels', prefix_names)

    async def test_non_owner_denies_before_defer(self):
        command = (
            next(
                item for item in administration.administration.__cog_app_commands__
                if item.name == 'operator'
            ).get_command('channels').get_command('purge')
        )
        interaction = SimpleNamespace(
            guild_id=10,
            user=SimpleNamespace(id=2),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(), defer=mock.AsyncMock(),
            ),
        )
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(administration.settings, 'owner_id', 1):
            await command.callback(
                cog, interaction,
                discord.app_commands.Choice(name='Stale', value='stale'),
            )
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
