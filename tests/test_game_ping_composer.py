"""Offline coverage for the P4.3 interactive game-ping composer."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, replace
import inspect
from types import SimpleNamespace
import time
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_ping_workers')
service = import_offline_runtime('modules.game_ping')
views = import_offline_runtime('modules.game_ping_views')


class FakeDatabase:
    def __init__(self):
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1
                return False

        return AtomicContext()


def snapshot(
    discord_id=10,
    *,
    level=5,
    is_staff=True,
    is_mod=False,
    guild_id=1,
):
    return workers.MemberSnapshot(
        guild_id=guild_id,
        discord_id=discord_id,
        display_name=f'User {discord_id}',
        name=f'user-{discord_id}',
        role_ids=(1000 + discord_id,),
        role_names=('ELO-Helper',),
        level=level,
        is_staff=is_staff,
        is_mod=is_mod,
        description=f'**User {discord_id}** (`{discord_id}`)',
    )


def game_snapshot(
    game_id=42,
    *,
    participants=(10, 20),
    destinations=None,
    all_side_channels=True,
    guild_id=1,
    is_completed=False,
    is_confirmed=False,
):
    if destinations is None:
        destinations = (
            workers.GamePingDestination(
                game_id,
                guild_id,
                100,
                tuple(participants),
                'central',
            ),
        )
    participant_rows = tuple(
        workers.GamePingParticipant(
            discord_id,
            f'User {discord_id}',
            1,
        )
        for discord_id in participants
    )
    return workers.GamePingGame(
        game_id=game_id,
        guild_id=guild_id,
        name=f'Game {game_id}',
        is_pending=False,
        is_completed=is_completed,
        is_confirmed=is_confirmed,
        participants=participant_rows,
        destinations=tuple(destinations),
        all_side_channels=all_side_channels,
    )


_AUTO_INFERRED = object()


def load_result(
    *,
    target_id=10,
    games=None,
    all_scope_allowed=True,
    truncated=False,
    inferred_game_id=_AUTO_INFERRED,
):
    games = tuple(games or (game_snapshot(),))
    if inferred_game_id is _AUTO_INFERRED:
        inferred_game_id = games[0].game_id if len(games) == 1 else None
    return workers.GamePingLoadResult(
        guild_id=1,
        target_id=target_id,
        target_name=f'User {target_id}',
        games=games,
        total_games=len(games),
        truncated=truncated,
        inferred_game_id=inferred_game_id,
        all_scope_allowed=all_scope_allowed,
    )


def channel_facts(
    *,
    channel_id=100,
    bot_channels=(100,),
    readable=(10, 20),
    guild_id=1,
    game_id=42,
):
    return workers.ChannelFacts(
        guild_id=guild_id,
        channel_id=channel_id,
        bot_channel_ids=tuple(bot_channels),
        private_bot_channel_ids=(),
        participant_permissions=tuple(
            workers.ParticipantPermission(game_id, user_id, user_id in readable)
            for user_id in (10, 20)
        ),
    )


def commit_request(*, scope='single', game_id=42, text='hello', facts=None):
    requester = snapshot()
    return workers.GamePingCommitRequest(
        guild_id=1,
        requester=requester,
        target_id=10,
        target_description=requester.description,
        scope=scope,
        game_ids=(game_id,),
        channel_facts=facts or channel_facts(),
        text=text,
        attachments=(),
        invoked_with='/game ping',
    )


class GamePingRegistrationTests(unittest.TestCase):
    def test_native_and_prefix_shapes(self):
        from modules import games, misc

        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        command = game_group.get_command('ping')
        self.assertEqual(
            [(parameter.name, parameter.type, parameter.required)
             for parameter in command.parameters],
            [
                ('message', discord.AppCommandOptionType.string, True),
                ('attachment', discord.AppCommandOptionType.attachment, False),
            ],
        )
        self.assertEqual(command._params['message'].to_dict()['max_length'], 4000)
        prefix_commands = {
            command.name: command for command in misc.misc.__cog_commands__
        }
        self.assertEqual(prefix_commands['ping'].aliases, [])
        self.assertEqual(prefix_commands['pingall'].aliases, [])
        self.assertNotIn('pingmobile', prefix_commands)
        self.assertNotIn('pingsteam', prefix_commands)


class GamePingDraftTests(unittest.TestCase):
    def test_modals_have_one_bounded_message_and_upload_field(self):
        for modal in (views.GamePingStartModal, views.GamePingComposeModal):
            self.assertEqual(
                modal.message_input.component.max_length,
                workers.MAX_TEXT_SECTION_LENGTH,
            )
            self.assertEqual(
                modal.attachments.component.max_values,
                workers.MAX_ATTACHMENTS,
            )

    def test_text_ceiling_preserves_newlines_and_rejects_extra_sections(self):
        sections = ('a\nline',)
        self.assertEqual(service.combine_sections(sections), 'a\nline')
        full = service.build_draft(('a' * 4000,))
        self.assertEqual(sum(map(len, full.sections)), workers.MAX_TEXT_LENGTH)
        self.assertEqual(len(full.text), 4_000)
        self.assertLessEqual(len(full.text), workers.MAX_FORMATTED_TEXT_LENGTH)

        with self.assertRaises(workers.GamePingValidationError):
            service.combine_sections(('a', 'b'))

    def test_empty_text_is_rejected_but_attachments_only_is_valid(self):
        with self.assertRaises(workers.GamePingValidationError):
            service.build_draft(('',), ())
        attachment = workers.AttachmentMetadata(
            filename='map.png',
            url='https://cdn.discordapp.com/attachments/1/2/map.png',
            content_type='image/png',
            size=10,
        )
        draft = service.build_draft(('',), (attachment,))
        self.assertEqual(draft.text, '')
        self.assertEqual(draft.attachments, (attachment,))
        with self.assertRaises(workers.GamePingValidationError):
            service.build_draft(('text',), (SimpleNamespace(url='bad'),))

    def test_native_input_uses_compose_sentinel_and_leading_game_id(self):
        self.assertEqual(
            service.parse_native_input('extension'),
            service.NativePingInput(False, None, 'extension'),
        )
        self.assertEqual(
            service.parse_native_input('144386 extension'),
            service.NativePingInput(False, 144386, 'extension'),
        )
        self.assertEqual(
            service.parse_native_input('compose'),
            service.NativePingInput(True, None, ''),
        )
        self.assertEqual(
            service.parse_native_input('compose 144386'),
            service.NativePingInput(True, 144386, ''),
        )
        with self.assertRaisesRegex(
            workers.GamePingValidationError,
            'must be a game ID',
        ):
            service.parse_native_input('compose later')

    def test_all_scope_excludes_a_cross_guild_channel_selection(self):
        result = load_result(games=(
            game_snapshot(42, guild_id=2),
            game_snapshot(50, guild_id=1),
            game_snapshot(51, guild_id=1, is_confirmed=True),
        ))
        self.assertEqual(
            service._game_ids_for_scope(
                result,
                scope='all',
                selected_game_id=42,
            ),
            (50,),
        )

    def test_attachment_capture_freezes_primitive_metadata_only(self):
        attachment = SimpleNamespace(
            filename='../map.png',
            url='https://cdn.discordapp.com/attachments/1/2/map.png',
            content_type='image/png; charset=utf-8',
            size=123,
            read=mock.Mock(),
        )
        captured = service.capture_attachments((attachment,))
        self.assertIsInstance(captured[0], workers.AttachmentMetadata)
        self.assertEqual(captured[0].filename, '_map.png')
        self.assertFalse(hasattr(captured[0], 'read'))
        attachment.read.assert_not_called()


class GamePingWorkerTests(unittest.TestCase):
    def test_dtos_are_frozen_and_candidate_load_closes_worker_connection(self):
        requester = snapshot()
        request = workers.GamePingLoadRequest(
            guild_id=1,
            requester=requester,
            target_id=10,
            explicit_game_id=42,
            channel_id=None,
            discover_all=False,
        )
        database = FakeDatabase()
        player = SimpleNamespace(
            name='Target',
            discord_member=SimpleNamespace(discord_id=10),
        )
        game = game_snapshot()
        with mock.patch.object(workers.models, 'db', database), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=player), \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(game,)) as load:
            result = workers.prepare_candidates(request)

        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(result.games, (game,))
        load.assert_called_once_with((42,), incomplete_only=False)
        with self.assertRaises(FrozenInstanceError):
            result.games = ()

    def test_completed_single_game_load_and_commit_are_allowed(self):
        requester = snapshot()
        completed_game = game_snapshot(
            is_completed=True,
            is_confirmed=True,
        )
        load_request = workers.GamePingLoadRequest(
            guild_id=1,
            requester=requester,
            target_id=10,
            channel_id=100,
            discover_all=False,
        )
        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_game_ids_for_channel', return_value=(42,)), \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(completed_game,)) as load:
            result = workers.prepare_candidates(load_request)
        self.assertEqual(result.games, (completed_game,))
        load.assert_called_once_with((42,), incomplete_only=False)

        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(completed_game,)) as load, \
                mock.patch.object(workers, '_destinations_for_game', return_value=completed_game.destinations), \
                mock.patch.object(workers.models.GameLog, 'write') as write:
            committed = workers.commit_notification(commit_request())
        self.assertEqual(committed.game_ids, (42,))
        load.assert_called_once_with(
            (42,),
            guild_id=None,
            incomplete_only=False,
        )
        write.assert_called_once()

    def test_cross_guild_single_does_not_require_invoking_guild_player(self):
        requester = snapshot(guild_id=1)
        foreign_game = game_snapshot(42, guild_id=2)
        load_request = workers.GamePingLoadRequest(
            guild_id=1,
            requester=requester,
            target_id=10,
            channel_id=100,
            discover_all=False,
        )
        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild') as player_lookup, \
                mock.patch.object(workers, '_game_ids_for_channel', return_value=(42,)), \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(foreign_game,)):
            result = workers.prepare_candidates(load_request)

        player_lookup.assert_not_called()
        self.assertEqual(result.games, (foreign_game,))
        self.assertEqual(result.target_name, 'User 10')

        request = commit_request()
        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild') as player_lookup, \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(foreign_game,)), \
                mock.patch.object(workers, '_destinations_for_game', return_value=foreign_game.destinations), \
                mock.patch.object(workers.models.GameLog, 'write'):
            committed = workers.commit_notification(request)

        player_lookup.assert_not_called()
        self.assertEqual(committed.game_ids, (42,))

    def test_all_scope_retains_invoking_guild_player_boundary(self):
        request = commit_request(scope='all')
        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=None), \
                mock.patch.object(workers, '_all_target_game_ids') as load_ids:
            with self.assertRaisesRegex(
                workers.GamePingLookupError,
                'not a registered ELO player in this server',
            ):
                workers.commit_notification(request)

        load_ids.assert_not_called()

    def test_native_explicit_game_can_override_channel_inference(self):
        requester = snapshot()
        explicit_game = game_snapshot(99)
        request = workers.GamePingLoadRequest(
            guild_id=1,
            requester=requester,
            target_id=10,
            explicit_game_id=99,
            channel_id=100,
            discover_all=False,
            prefer_explicit_game_id=True,
        )
        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_game_ids_for_channel', return_value=(42,)), \
                mock.patch.object(
                    workers,
                    '_load_games_by_ids',
                    return_value=(explicit_game,),
                ) as load:
            result = workers.prepare_candidates(request)

        self.assertEqual(result.games, (explicit_game,))
        self.assertEqual(result.inferred_game_id, 42)
        load.assert_called_once_with((99,), incomplete_only=False)

    def test_all_scope_still_rejects_completed_games(self):
        completed_game = game_snapshot(
            is_completed=True,
            is_confirmed=True,
        )
        request = commit_request(scope='all')
        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_all_target_game_ids', return_value=((42,), 1, False)), \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(completed_game,)) as load:
            with self.assertRaisesRegex(
                workers.GamePingValidationError,
                'no longer incomplete',
            ):
                workers.commit_notification(request)
        load.assert_called_once_with(
            (42,),
            guild_id=1,
            incomplete_only=True,
        )

    def test_channel_inference_does_not_exclude_confirmed_games(self):
        source = inspect.getsource(workers._game_ids_for_channel)
        self.assertNotIn('is_confirmed', source)

    def test_cross_guild_channel_inference_wins_and_all_discovery_stays_local(self):
        requester = snapshot()
        request = workers.GamePingLoadRequest(
            guild_id=1,
            requester=requester,
            target_id=10,
            explicit_game_id=99,
            channel_id=100,
            discover_all=True,
        )
        foreign_game = game_snapshot(42, guild_id=2)
        local_game = game_snapshot(50, guild_id=1)

        def load_games(game_ids, *, guild_id=None, incomplete_only=True):
            if (
                tuple(game_ids) == (42,)
                and guild_id is None
                and not incomplete_only
            ):
                return (foreign_game,)
            if (
                tuple(game_ids) == (50,)
                and guild_id == 1
                and incomplete_only
            ):
                return (local_game,)
            self.fail(
                f'unexpected game load: {game_ids}, guild_id={guild_id}, '
                f'incomplete_only={incomplete_only}'
            )

        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_game_ids_for_channel', return_value=(42,)) as infer, \
                mock.patch.object(workers, '_all_target_game_ids', return_value=((50,), 1, False)), \
                mock.patch.object(workers, '_load_games_by_ids', side_effect=load_games):
            result = workers.prepare_candidates(request)

        infer.assert_called_once_with(100)
        self.assertEqual(result.inferred_game_id, 42)
        self.assertEqual(tuple(game.game_id for game in result.games), (42, 50))
        self.assertEqual(result.total_games, 1)
        self.assertFalse(result.truncated)

    def test_atomic_audit_success_and_rollback(self):
        database = FakeDatabase()
        request = commit_request()
        game = game_snapshot(guild_id=2)
        with mock.patch.object(workers.models, 'db', database), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(game,)) as load, \
                mock.patch.object(workers, '_destinations_for_game', return_value=game.destinations), \
                mock.patch.object(workers.models.GameLog, 'write') as write:
            result = workers.commit_notification(request)
            self.assertEqual(result.game_ids, (42,))
            self.assertEqual(result.requester_description, request.requester.description)
            self.assertEqual(result.target_description, request.target_description)
            self.assertEqual(write.call_count, 1)
            audit_message = write.call_args.kwargs['message']
            self.assertIn('committed a game ping notification request', audit_message)
            self.assertNotIn(' sent a game ping', audit_message)
            self.assertIn(request.requester.description, audit_message)
            self.assertEqual(write.call_args.kwargs['guild_id'], 2)
            load.assert_called_once_with(
                (42,),
                guild_id=None,
                incomplete_only=False,
            )
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_closed, 1)

        database = FakeDatabase()
        with mock.patch.object(workers.models, 'db', database), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_load_games_by_ids', return_value=(game,)), \
                mock.patch.object(workers, '_destinations_for_game', return_value=game.destinations), \
                mock.patch.object(workers.models.GameLog, 'write', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                workers.commit_notification(request)
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_all_scope_rechecks_the_exact_current_game_set(self):
        request = commit_request(scope='all')
        with mock.patch.object(workers.models, 'db', FakeDatabase()), \
                mock.patch.object(workers, '_registered_member', return_value=object()), \
                mock.patch.object(workers, '_player_for_guild', return_value=object()), \
                mock.patch.object(workers, '_all_target_game_ids', return_value=((99,), 1, False)):
            with self.assertRaises(workers.GamePingConflictError):
                workers.commit_notification(request)

    def test_slow_commit_does_not_block_the_event_loop_and_cancel_drains_worker(self):
        request = commit_request()
        result = workers.GamePingCommitResult(
            guild_id=1,
            requester_id=10,
            target_id=10,
            scope='single',
            game_ids=(42,),
            total_games=1,
            truncated=False,
            recipient_ids=(10, 20),
            recipient_names=('User 10', 'User 20'),
            destinations=(),
            text='hello',
            attachments=(),
        )

        def slow_commit(_request):
            time.sleep(0.05)
            return result

        async def exercise():
            with mock.patch.object(workers, 'commit_notification', side_effect=slow_commit):
                task = asyncio.create_task(workers.run_ping_commit(request))
                await asyncio.sleep(0)
                started = time.monotonic()
                await asyncio.sleep(0.005)
                self.assertLess(time.monotonic() - started, 0.04)
                task.cancel()
                with self.assertRaises(workers.GamePingCancelled) as caught:
                    await asyncio.wait_for(task, 1.0)
                self.assertTrue(caught.exception.committed)

        asyncio.run(exercise())


class GamePingPermissionAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_destination_policy_allows_central_and_rejects_wrong_all_scope_channel(self):
        game = game_snapshot(
            destinations=(
                workers.GamePingDestination(42, 1, 100, (10, 20), 'central'),
                workers.GamePingDestination(42, 1, 101, (10,), 'side'),
            ),
            all_side_channels=False,
        )
        central_request = commit_request(
            facts=channel_facts(channel_id=100, bot_channels=()),
        )
        self.assertEqual(
            workers._destinations_for_game(game, central_request),
            game.destinations,
        )
        wrong_all = commit_request(
            scope='all',
            facts=channel_facts(channel_id=999, bot_channels=()),
        )
        with self.assertRaises(workers.GamePingPermissionError):
            workers._destinations_for_game(game, wrong_all)

    async def test_delivery_chunks_exact_text_and_uses_explicit_user_mentions_only(self):
        class Channel:
            def __init__(self, channel_id, fail=False):
                self.id = channel_id
                self.fail = fail
                self.sent = []

            async def send(self, content, **kwargs):
                if self.fail:
                    raise RuntimeError('delivery failed')
                self.sent.append((content, kwargs))

        class Guild:
            def __init__(self, channels):
                self.id = 1
                self.channels = channels

            def get_channel(self, channel_id):
                return self.channels.get(channel_id)

        good = Channel(100)
        bad = Channel(101, fail=True)
        completion = Channel(999)
        attachment = workers.AttachmentMetadata(
            'report.txt',
            'https://cdn.discordapp.com/attachments/1/2/report.txt',
            'text/plain',
            1,
        )
        result = workers.GamePingCommitResult(
            guild_id=1,
            requester_id=10,
            target_id=10,
            scope='single',
            game_ids=(42,),
            total_games=1,
            truncated=False,
            recipient_ids=(10, 20),
            recipient_names=('User 10', 'User 20'),
            destinations=(
                workers.GamePingDestination(42, 1, 100, (10, 20), 'central'),
                workers.GamePingDestination(42, 1, 101, (20,), 'side'),
            ),
            text='line one\n@everyone\n' + ('x' * 2500),
            attachments=(attachment,),
            requester_description='**User 10** (`10`)',
            target_description='**User 10** (`10`)',
        )
        with mock.patch.object(service.logger, 'exception'):
            delivered = await service.deliver_committed(
                result,
                guilds=(Guild({100: good, 101: bad}),),
                completion_destination=completion,
                completion_on_success=False,
            )
        self.assertEqual(len(delivered.delivered_destinations), 1)
        self.assertEqual(
            [(failure.game_id, failure.channel_id) for failure in delivered.failures],
            [(42, 101)],
        )
        combined = ''.join(message[0] for message in good.sent)
        self.assertIn('line one\n@\u200beveryone\n', combined)
        self.assertIn(attachment.url, combined)
        self.assertIn('<@10>', combined)
        self.assertIn('<@20>', combined)
        self.assertEqual(combined.count('@\u200beveryone'), 1)
        self.assertEqual(len(good.sent) > 1, True)
        allowed = good.sent[0][1]['allowed_mentions']
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.everyone)
        self.assertEqual([user.id for user in allowed.users], [10, 20])
        self.assertEqual(len(completion.sent), 1)
        self.assertFalse(completion.sent[0][1]['allowed_mentions'].everyone)

    async def test_legacy_prefix_success_needs_no_extra_reconciliation(self):
        destination_channel = SimpleNamespace(id=100, send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=1,
            get_channel=lambda channel_id: (
                destination_channel if channel_id == 100 else None
            ),
        )
        completion = SimpleNamespace(
            id=999,
            guild=guild,
            send=mock.AsyncMock(),
        )
        result = workers.GamePingCommitResult(
            guild_id=1,
            requester_id=10,
            target_id=10,
            scope='single',
            game_ids=(42,),
            total_games=1,
            truncated=False,
            recipient_ids=(10, 20),
            recipient_names=('User 10', 'User 20'),
            destinations=(
                workers.GamePingDestination(
                    42,
                    1,
                    100,
                    (10, 20),
                    'side',
                ),
            ),
            text='hello',
            attachments=(),
        )

        delivered = await service.deliver_committed(
            result,
            guilds=(guild,),
            completion_destination=completion,
            completion_on_success=False,
        )

        self.assertEqual(delivered.failures, ())
        destination_channel.send.assert_awaited_once()
        completion.send.assert_not_awaited()

    def test_delivery_content_attributes_self_and_staff_on_behalf(self):
        destination = workers.GamePingDestination(42, 1, 100, (10, 20), 'central')
        self_result = workers.GamePingCommitResult(
            guild_id=1,
            requester_id=10,
            target_id=10,
            scope='single',
            game_ids=(42,),
            total_games=1,
            truncated=False,
            recipient_ids=(10, 20),
            recipient_names=('User 10', 'User 20'),
            destinations=(destination,),
            text='hello',
            attachments=(),
            requester_description='**Actor** (`10`)',
            target_description='**Actor** (`10`)',
        )
        self_content = service.delivery_content(self_result, destination)
        self.assertIn('Actor: **Actor** (`10`)', self_content)
        self.assertNotIn('On behalf of:', self_content)

        staff_result = replace(
            self_result,
            requester_id=99,
            target_id=20,
            requester_description='**Staff** (`99`)',
            target_description='**Target** (`20`)',
        )
        staff_content = service.delivery_content(staff_result, destination)
        self.assertIn('Actor: **Staff** (`99`)', staff_content)
        self.assertIn('On behalf of: **Target** (`20`)', staff_content)
        self.assertNotIn('@everyone', staff_content)
        spoofed = replace(
            staff_result,
            requester_description='**Someone else** (`10`)',
        )
        spoofed_content = service.delivery_content(spoofed, destination)
        self.assertIn('Actor: Actor (`99`)', spoofed_content)
        self.assertNotIn('Someone else', spoofed_content)
        completion = service._completion_message(
            staff_result,
            (),
            requester_description=None,
            delivered_count=1,
        )
        self.assertIn('Actor:', completion)
        self.assertIn('on behalf of:', completion)


class GamePingViewLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requester = snapshot()
        self.result = load_result()
        self.facts = channel_facts()
        self.draft = service.build_draft(('hello',), ())

    def test_ordinary_single_game_hides_irrelevant_controls_and_jargon(self):
        requester = snapshot(level=2, is_staff=False)
        result = load_result(all_scope_allowed=False)
        view = views.GamePingComposerView(
            requester=requester,
            target=requester,
            result=result,
            channel_facts=self.facts,
            selected_game_id=42,
            target_loader=None,
            confirmer=mock.AsyncMock(),
        )
        view.draft = self.draft
        view.rebuild()

        self.assertIsNone(view.scope_select)
        self.assertIsNone(view.game_select)
        self.assertIsNone(view.target_select)
        preview = service.preview_message(
            result,
            requester=requester,
            target=requester,
            scope='single',
            selected_game_id=42,
            draft=self.draft,
            channel_facts=self.facts,
        )
        self.assertIn('Game: `42`', preview)
        self.assertNotIn('Target:', preview)
        self.assertNotIn('Resolved', preview)
        self.assertNotIn('(guild ', preview)

    def test_ambiguous_games_require_a_choice_instead_of_defaulting(self):
        requester = snapshot(level=2, is_staff=False)
        result = load_result(
            games=(game_snapshot(42), game_snapshot(50)),
            inferred_game_id=None,
            all_scope_allowed=False,
        )
        view = views.GamePingComposerView(
            requester=requester,
            target=requester,
            result=result,
            channel_facts=self.facts,
            selected_game_id=None,
            target_loader=None,
            confirmer=mock.AsyncMock(),
        )
        view.draft = self.draft
        view.rebuild()

        self.assertIsNotNone(view.game_select)
        self.assertFalse(any(option.default for option in view.game_select.options))
        self.assertTrue(view.confirm_button.disabled)

    def test_unusable_all_scope_is_hidden(self):
        requester = snapshot(level=3, is_staff=False, is_mod=False)
        result = load_result(
            games=(game_snapshot(42), game_snapshot(50)),
            inferred_game_id=None,
            all_scope_allowed=True,
        )
        blocked_facts = channel_facts(
            channel_id=999,
            bot_channels=(),
            readable=(),
        )
        view = views.GamePingComposerView(
            requester=requester,
            target=requester,
            result=result,
            channel_facts=blocked_facts,
            selected_game_id=42,
            target_loader=None,
            confirmer=mock.AsyncMock(),
        )
        self.assertIsNone(view.scope_select)
        self.assertEqual(view.scope, 'single')

    async def test_remove_files_clears_existing_attachments(self):
        attachment = workers.AttachmentMetadata(
            'map.png',
            'https://cdn.discordapp.com/attachments/1/2/map.png',
            'image/png',
            10,
        )
        view = self._view()
        view.draft = service.build_draft(('hello',), (attachment,))
        view.rebuild()
        interaction = self._component_interaction()

        await view._remove_files_clicked(interaction)

        self.assertEqual(view.draft.attachments, ())
        self.assertIsNone(view.remove_files_button)
        interaction.response.edit_message.assert_awaited_once_with(view=view)

    async def test_precommit_failure_restores_exact_draft_and_confirm_is_retryable(self):
        async def fail(_interaction, _view):
            raise workers.GamePingConflictError('stale game')

        view = views.GamePingComposerView(
            requester=self.requester,
            target=self.requester,
            result=self.result,
            channel_facts=self.facts,
            selected_game_id=42,
            target_loader=None,
            confirmer=fail,
        )
        view.draft = self.draft
        old_draft = view.draft
        view.rebuild()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=1,
            channel_id=100,
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._confirm_clicked(interaction)
        self.assertIs(view.draft, old_draft)
        self.assertFalse(view.committed)
        self.assertFalse(view._busy)
        self.assertFalse(view.confirm_button.disabled)
        interaction.response.defer.assert_awaited_once()

    async def test_successful_confirm_is_terminal_even_with_postcommit_failure(self):
        committed = workers.GamePingCommitResult(
            guild_id=1,
            requester_id=10,
            target_id=10,
            scope='single',
            game_ids=(42,),
            total_games=1,
            truncated=False,
            recipient_ids=(10, 20),
            recipient_names=('User 10', 'User 20'),
            destinations=(),
            text='hello',
            attachments=(),
        )
        outcome = service.DeliveryResult(committed, (), (
            service.DeliveryFailure(42, 1, 100, 'failed after commit'),
        ))

        async def succeed(_interaction, _view):
            return outcome

        view = views.GamePingComposerView(
            requester=self.requester,
            target=self.requester,
            result=self.result,
            channel_facts=self.facts,
            selected_game_id=42,
            target_loader=None,
            confirmer=succeed,
        )
        view.draft = self.draft
        view.rebuild()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=1,
            channel_id=100,
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._confirm_clicked(interaction)
        self.assertTrue(view.committed)
        self.assertTrue(view.is_finished())
        self.assertIn('Do not send it again', view.status)
        self.assertEqual(view._confirmations, 1)

    def _component_interaction(self, *, send_modal=None):
        response = SimpleNamespace(
            send_modal=send_modal or mock.AsyncMock(),
            send_message=mock.AsyncMock(),
            defer=mock.AsyncMock(),
            edit_message=mock.AsyncMock(),
            is_done=mock.Mock(return_value=False),
        )
        return SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=1,
            channel_id=100,
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

    def _view(self):
        view = views.GamePingComposerView(
            requester=self.requester,
            target=self.requester,
            result=self.result,
            channel_facts=self.facts,
            selected_game_id=42,
            target_loader=None,
            confirmer=mock.AsyncMock(),
        )
        return view

    async def test_dismissed_modal_without_callback_allows_new_generation(self):
        view = self._view()
        first_interaction = self._component_interaction()
        second_interaction = self._component_interaction()

        await view._compose_clicked(first_interaction)
        first_modal = first_interaction.response.send_modal.call_args.args[0]
        first_generation = first_modal.generation

        # Discord supplies no callback when the user closes the modal.  The
        # next component interaction must still be able to open a fresh one.
        await view._compose_clicked(second_interaction)
        second_modal = second_interaction.response.send_modal.call_args.args[0]
        self.assertGreater(second_modal.generation, first_generation)
        self.assertEqual(view.current_modal_generation, second_modal.generation)

    async def test_newest_modal_submits_successfully(self):
        view = self._view()
        interaction = self._component_interaction()
        await view._compose_clicked(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        modal.message_input.component._value = 'newest draft'

        await modal.on_submit(self._component_interaction())
        self.assertIsNotNone(view.draft)
        self.assertEqual(view.draft.text, 'newest draft')
        self.assertEqual(view.current_modal_generation, modal.generation)

    async def test_older_modal_submission_is_private_and_cannot_overwrite(self):
        view = self._view()
        first_interaction = self._component_interaction()
        second_interaction = self._component_interaction()
        await view._compose_clicked(first_interaction)
        await view._compose_clicked(second_interaction)
        older_modal = first_interaction.response.send_modal.call_args.args[0]
        newest_modal = second_interaction.response.send_modal.call_args.args[0]

        newest_modal.message_input.component._value = 'newest draft'
        await newest_modal.on_submit(self._component_interaction())
        original_draft = view.draft

        older_modal.message_input.component._value = 'stale overwrite'
        stale_submit = self._component_interaction()
        await older_modal.on_submit(stale_submit)
        self.assertIs(view.draft, original_draft)
        self.assertEqual(view.draft.text, 'newest draft')
        stale_submit.response.send_message.assert_awaited_once()
        self.assertIn('stale', stale_submit.response.send_message.call_args.args[0])

    async def test_modal_dispatch_failure_does_not_block_another_click(self):
        view = self._view()
        failed_send = mock.AsyncMock(side_effect=RuntimeError('send failed'))
        failed_interaction = self._component_interaction(send_modal=failed_send)
        await view._compose_clicked(failed_interaction)
        self.assertTrue(failed_interaction.response.send_message.await_count)

        successful_interaction = self._component_interaction()
        await view._compose_clicked(successful_interaction)
        successful_interaction.response.send_modal.assert_awaited_once()
        self.assertEqual(
            view.current_modal_generation,
            successful_interaction.response.send_modal.call_args.args[0].generation,
        )

    async def test_modal_timeout_invalidates_only_its_current_generation(self):
        view = self._view()
        interaction = self._component_interaction()
        await view._compose_clicked(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        await modal.on_timeout()
        self.assertFalse(view.is_current_modal(modal.generation))


class GamePingPrefixAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _context(self, author, *, target=None):
        channel = SimpleNamespace(
            id=100,
            permissions_for=lambda _member: SimpleNamespace(read_messages=True),
        )
        guild = SimpleNamespace(
            id=1,
            get_member=lambda member_id: target if target and member_id == target.id else author,
        )
        return SimpleNamespace(
            author=author,
            guild=guild,
            channel=channel,
            prefix='$',
            invoked_with='ping',
            message=SimpleNamespace(attachments=()),
            bot=SimpleNamespace(guilds=()),
            send=mock.AsyncMock(),
        )

    async def test_prefix_single_uses_shared_primitive_request_and_delivery(self):
        author = SimpleNamespace(id=10, display_name='Author', name='author', roles=(), guild=SimpleNamespace(id=1))
        context = self._context(author)
        attachment = SimpleNamespace(
            filename='note.txt',
            url='https://cdn.discordapp.com/attachments/1/2/note.txt',
            content_type='text/plain',
            size=2,
        )
        requester = snapshot(level=5, is_staff=True)
        loaded = load_result(inferred_game_id=None)
        facts = channel_facts()
        captured = {}

        async def candidates(request):
            captured['candidate'] = request
            return loaded

        async def confirm(request, **kwargs):
            captured['commit'] = request
            captured['confirm_kwargs'] = kwargs
            return 'delivered'

        with mock.patch.object(service, 'capture_member', return_value=requester), \
                mock.patch.object(service, 'capture_channel_facts', return_value=facts), \
                mock.patch.object(workers, 'run_ping_candidates', side_effect=candidates), \
                mock.patch.object(service, 'confirm_and_deliver', side_effect=confirm):
            outcome = await service.run_prefix_single(
                context,
                '42 hello world',
                attachments=(attachment,),
            )
        self.assertEqual(outcome, 'delivered')
        self.assertEqual(captured['candidate'].explicit_game_id, 42)
        self.assertEqual(captured['commit'].scope, 'single')
        self.assertEqual(captured['commit'].text, 'hello world')
        self.assertEqual(captured['commit'].attachments[0].filename, 'note.txt')
        self.assertFalse(captured['confirm_kwargs']['completion_on_success'])

    async def test_native_quick_ping_infers_one_game_and_delivers_immediately(self):
        author = SimpleNamespace(
            id=10,
            display_name='Author',
            name='author',
            roles=(),
        )
        channel = SimpleNamespace(id=100)
        interaction = SimpleNamespace(
            user=author,
            guild=SimpleNamespace(id=1),
            channel=channel,
            channel_id=100,
        )
        requester = snapshot(level=5, is_staff=True)
        loaded = load_result(inferred_game_id=42)
        facts = channel_facts()
        captured = {}

        async def candidates(request):
            captured['candidate'] = request
            return loaded

        async def confirm(request, **kwargs):
            captured['commit'] = request
            captured['confirm_kwargs'] = kwargs
            return 'delivered'

        with mock.patch.object(service, 'capture_member', return_value=requester), \
                mock.patch.object(service, 'capture_channel_facts', return_value=facts), \
                mock.patch.object(workers, 'run_ping_candidates', side_effect=candidates), \
                mock.patch.object(service, 'confirm_and_deliver', side_effect=confirm):
            outcome = await service.run_native_single(
                interaction,
                message='extension',
                game_id=None,
                guilds=('guild-cache',),
            )

        self.assertEqual(outcome, 'delivered')
        self.assertIsNone(captured['candidate'].explicit_game_id)
        self.assertFalse(captured['candidate'].discover_all)
        self.assertEqual(captured['commit'].game_ids, (42,))
        self.assertEqual(captured['commit'].text, 'extension')
        self.assertEqual(captured['commit'].invoked_with, '/game ping')
        self.assertEqual(captured['confirm_kwargs']['guilds'], ('guild-cache',))
        self.assertIs(captured['confirm_kwargs']['completion_destination'], channel)
        self.assertFalse(captured['confirm_kwargs']['completion_on_success'])

    async def test_native_command_uses_quick_path_when_message_is_supplied(self):
        from modules import games

        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        command = game_group.get_command('ping')
        committed = workers.GamePingCommitResult(
            guild_id=1,
            requester_id=10,
            target_id=10,
            scope='single',
            game_ids=(42,),
            total_games=1,
            truncated=False,
            recipient_ids=(10, 20),
            recipient_names=('Author', 'Other'),
            destinations=(),
            text='extension',
            attachments=(),
            requester_description='**Author** (`10`)',
            target_description='**Author** (`10`)',
        )
        delivered = service.DeliveryResult(committed, (), ())
        requester = snapshot(level=5, is_staff=True)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=100),
            channel_id=100,
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            edit_original_response=mock.AsyncMock(),
        )
        cog = SimpleNamespace(
            bot=SimpleNamespace(guilds=('guild-cache',)),
            _claim_game_ping_send=mock.Mock(return_value=123.0),
            _release_game_ping_send=mock.Mock(),
        )

        with mock.patch.object(
                games.game_ping,
                'capture_member',
                return_value=requester,
        ), mock.patch.object(
                games.game_ping,
                'run_native_single',
                return_value=delivered,
        ) as quick_ping:
            await command.callback(
                cog,
                interaction,
                message='42 extension',
                attachment=None,
            )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        quick_ping.assert_awaited_once_with(
            interaction,
            message='extension',
            game_id=42,
            attachment=None,
            guilds=('guild-cache',),
        )
        cog._claim_game_ping_send.assert_called_once_with(10)
        cog._release_game_ping_send.assert_not_called()
        interaction.edit_original_response.assert_awaited_once_with(
            content='Ping sent for game `42`.',
        )

    async def test_native_compose_opens_modal_and_does_not_choose_ambiguous_game(self):
        from modules import games

        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        command = game_group.get_command('ping')
        requester = snapshot(level=2, is_staff=False)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=1),
            guild_id=1,
            channel=SimpleNamespace(id=100),
            channel_id=100,
            response=SimpleNamespace(
                send_modal=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
        )
        cog = SimpleNamespace(
            bot=SimpleNamespace(guilds=('guild-cache',)),
            _claim_game_ping_send=mock.Mock(return_value=123.0),
            _release_game_ping_send=mock.Mock(),
        )

        with mock.patch.object(
            games.game_ping,
            'capture_member',
            return_value=requester,
        ):
            await command.callback(
                cog,
                interaction,
                message='compose',
                attachment=None,
            )

        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, views.GamePingStartModal)
        modal.message_input.component._value = 'longer update'
        loaded = load_result(
            games=(game_snapshot(50), game_snapshot(42)),
            inferred_game_id=None,
            all_scope_allowed=False,
        )
        submit_interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=1),
            guild_id=1,
            channel=SimpleNamespace(id=100),
            channel_id=100,
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                return_value=SimpleNamespace(edit=mock.AsyncMock()),
            ),
        )
        with mock.patch.object(
                workers,
                'run_ping_candidates',
                return_value=loaded,
        ), mock.patch.object(
                games.game_ping,
                'capture_channel_facts',
                return_value=channel_facts(),
        ):
            await modal.on_submit(submit_interaction)

        submit_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        workspace = submit_interaction.edit_original_response.call_args.kwargs['view']
        self.assertIsNone(workspace.selected_game_id)
        self.assertIsNotNone(workspace.game_select)
        self.assertTrue(workspace.confirm_button.disabled)
        self.assertEqual(workspace.draft.text, 'longer update')

        workspace.selected_game_id = 50
        workspace.rebuild()
        with mock.patch.object(
            games.game_ping,
            'confirm_and_deliver',
            return_value='sent',
        ) as confirm:
            outcome = await workspace.confirmer(submit_interaction, workspace)

        self.assertEqual(outcome, 'sent')
        commit = confirm.call_args.args[0]
        self.assertEqual(commit.game_ids, (50,))
        self.assertEqual(commit.invoked_with, '/game ping compose')
        self.assertFalse(confirm.call_args.kwargs['completion_on_success'])
        cog._claim_game_ping_send.assert_called_once_with(10)

    async def test_prefix_single_keeps_leading_number_when_channel_inference_wins(self):
        author = SimpleNamespace(id=10, display_name='Author', name='author', roles=(), guild=SimpleNamespace(id=1))
        context = self._context(author)
        requester = snapshot(level=5, is_staff=True)
        loaded = load_result(games=(game_snapshot(42, guild_id=2),))
        captured = {}

        async def confirm(request, **_kwargs):
            captured['commit'] = request
            return 'delivered'

        with mock.patch.object(service, 'capture_member', return_value=requester), \
                mock.patch.object(service, 'capture_channel_facts', return_value=channel_facts()), \
                mock.patch.object(workers, 'run_ping_candidates', return_value=loaded), \
                mock.patch.object(service, 'confirm_and_deliver', side_effect=confirm):
            outcome = await service.run_prefix_single(
                context,
                '99 city island please',
            )

        self.assertEqual(outcome, 'delivered')
        self.assertEqual(captured['commit'].game_ids, (42,))
        self.assertEqual(captured['commit'].text, '99 city island please')

    async def test_prefix_all_preserves_staff_target_grammar_without_platform_filter(self):
        author = SimpleNamespace(id=10, display_name='Author', name='author', roles=(), guild=SimpleNamespace(id=1))
        target_member = SimpleNamespace(id=20, display_name='Target', name='target', roles=(), guild=SimpleNamespace(id=1))
        context = self._context(author, target=target_member)
        requester = snapshot(10, level=5, is_staff=True)
        target = snapshot(20, level=0, is_staff=False)
        loaded = load_result(
            target_id=20,
            games=(game_snapshot(participants=(20, 10)),),
            all_scope_allowed=True,
        )
        facts = channel_facts()
        captured = {}

        async def candidates(request):
            captured['candidate'] = request
            return loaded

        async def confirm(request, **_kwargs):
            captured['commit'] = request
            return 'delivered'

        with mock.patch.object(
            service,
            'capture_member',
            side_effect=lambda member, _guild_id: requester if member.id == 10 else target,
        ), mock.patch.object(service, 'capture_channel_facts', return_value=facts), \
                mock.patch.object(workers, 'run_ping_candidates', side_effect=candidates), \
                mock.patch.object(service, 'confirm_and_deliver', side_effect=confirm):
            outcome = await service.run_prefix_all(
                context,
                '<@20> notify all',
            )
        self.assertEqual(outcome, 'delivered')
        self.assertEqual(captured['candidate'].target_id, 20)
        self.assertTrue(captured['candidate'].discover_all)
        self.assertEqual(captured['commit'].scope, 'all')
        self.assertEqual(captured['commit'].invoked_with, 'pingall')

        committed = workers.GamePingCommitResult(
            guild_id=1,
            requester_id=captured['commit'].requester.discord_id,
            target_id=captured['commit'].target_id,
            scope=captured['commit'].scope,
            game_ids=captured['commit'].game_ids,
            total_games=1,
            truncated=False,
            recipient_ids=(20, 10),
            recipient_names=('Target', 'Author'),
            destinations=(),
            text=captured['commit'].text,
            attachments=(),
            requester_description=captured['commit'].requester.description,
            target_description=captured['commit'].target_description,
        )
        content = service.delivery_content(
            committed,
            workers.GamePingDestination(42, 1, 100, (20, 10), 'central'),
        )
        self.assertIn('Actor:', content)
        self.assertIn('On behalf of:', content)


if __name__ == '__main__':
    unittest.main()
