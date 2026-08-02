"""Focused offline coverage for the P4.2a game-map attribute."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
import peewee

from modules import exceptions
from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


game_workers = import_offline_runtime('modules.game_workers')
game_map = import_offline_runtime('modules.game_map')
games = import_offline_runtime('modules.games')


def app_group(name):
    return next(
        command
        for command in games.polygames.__cog_app_commands__
        if command.name == name
    )


def map_request(
    *,
    game_id=42,
    guild_id=300,
    channel_id=900,
    requester_id=100,
    requester_level=3,
    map_type='Dryland',
    clear=False,
    legacy_tokens=(),
    allow_related_channel=False,
):
    return game_workers.GameMapMutationRequest(
        game_id=game_id,
        guild_id=guild_id,
        channel_id=channel_id,
        requester_id=requester_id,
        requester_level=requester_level,
        requester_description='**Player** (`100`)',
        map_type=map_type,
        clear=clear,
        legacy_tokens=tuple(legacy_tokens),
        allow_related_channel=allow_related_channel,
        invoked_with='setmap',
    )


class GameMapWorkerTests(unittest.TestCase):
    def make_game(self, state, *, guild_id=300, participant_ids=(100,)):
        game = SimpleNamespace(
            id=42,
            guild_id=guild_id,
            map_type=state.get('map_type', 'Lakes'),
            announcement_channel=None,
            announcement_message=None,
        )

        def save():
            state['map_type'] = game.map_type

        def player(*, discord_id):
            if discord_id in participant_ids:
                return SimpleNamespace(id=discord_id)
            return None

        game.save = save
        game.player = player
        game.uses_channel_id = lambda channel_id: channel_id == 900
        return game

    def patch_game(self, game, database, *, registered=True, log=None):
        stack = mock.patch.object(game_workers.models, 'db', database)
        stack.start()
        self.addCleanup(stack.stop)
        game_get = mock.patch.object(
            game_workers.models.Game,
            'get_by_id',
            return_value=game,
        )
        game_get.start()
        self.addCleanup(game_get.stop)
        member_get = mock.patch.object(
            game_workers.models.DiscordMember,
            'get_or_none',
            return_value=object() if registered else None,
        )
        member_get.start()
        self.addCleanup(member_get.stop)
        log_patch = mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=log,
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_worker_requests_are_immutable_primitive_snapshots(self):
        request = map_request()
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 99
        self.assertIsInstance(request.legacy_tokens, tuple)
        self.assertIsInstance(request.requester_description, str)

    def test_update_commits_map_and_audit_on_worker_connection(self):
        state = {'map_type': 'Lakes', 'logs': []}
        database = FakeDatabase(state)
        game = self.make_game(state)
        self.patch_game(
            game,
            database,
            log=lambda **kwargs: state['logs'].append(kwargs),
        )

        result = game_workers.set_game_map(map_request())

        self.assertEqual(result.old_map_type, 'Lakes')
        self.assertEqual(result.map_type, 'Dryland')
        self.assertEqual(state['map_type'], 'Dryland')
        self.assertEqual(len(state['logs']), 1)
        self.assertIn('set map type to "Dryland"', state['logs'][0]['message'])
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)

    def test_explicit_clear_commits_empty_map_value(self):
        state = {'map_type': 'Archipelago', 'logs': []}
        database = FakeDatabase(state)
        game = self.make_game(state)
        self.patch_game(
            game,
            database,
            log=lambda **kwargs: state['logs'].append(kwargs),
        )

        result = game_workers.set_game_map(
            map_request(map_type=None, clear=True)
        )

        self.assertEqual(result.map_type, '')
        self.assertEqual(state['map_type'], '')
        self.assertEqual(database.commits, 1)

    def test_audit_failure_rolls_back_map_and_closes_connection(self):
        state = {'map_type': 'Lakes', 'logs': []}
        database = FakeDatabase(state)
        game = self.make_game(state)
        self.patch_game(
            game,
            database,
            log=mock.Mock(
                side_effect=peewee.OperationalError('map log failure')
            ),
        )

        with self.assertRaisesRegex(peewee.OperationalError, 'map log failure'):
            game_workers.set_game_map(map_request())

        self.assertEqual(state['map_type'], 'Lakes')
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_legacy_normalization_none_aliases_and_channel_inference(self):
        database = FakeDatabase({})
        game = self.make_game({}, participant_ids=(100,))
        game_by_channel_or_arg = mock.patch.object(
            game_workers.models.Game,
            'by_channel_or_arg',
            return_value=game,
        )
        get_map_type = mock.patch.object(
            game_workers.utilities,
            'get_map_type',
            side_effect=lambda value: {
                'arch': 'Archipelago',
                'ww': 'Water World',
                'drylands': 'Dryland',
            }.get(value.lower()),
        )
        with mock.patch.object(game_workers.models, 'db', database), \
                game_by_channel_or_arg as lookup, get_map_type:
            arch = game_workers.prepare_legacy_game_map(
                map_request(
                    game_id=None,
                    legacy_tokens=('42', 'arch'),
                    allow_related_channel=True,
                )
            )
            inferred = game_workers.prepare_legacy_game_map(
                map_request(
                    game_id=None,
                    legacy_tokens=('drylands',),
                    allow_related_channel=True,
                )
            )
            clear = game_workers.prepare_legacy_game_map(
                map_request(
                    game_id=None,
                    legacy_tokens=('none',),
                    allow_related_channel=True,
                )
            )

        self.assertEqual(arch, game_workers.GameMapTarget(42, 'Archipelago', False))
        self.assertEqual(inferred, game_workers.GameMapTarget(42, 'Dryland', False))
        self.assertEqual(clear, game_workers.GameMapTarget(42, '', True))
        self.assertEqual(lookup.call_count, 3)

    def test_conflicting_options_are_rejected_before_database_mutation(self):
        request = map_request(map_type='Lakes', clear=True)
        with self.assertRaisesRegex(
            game_workers.GameMapValidationError,
            'either a map type or clear',
        ):
            game_workers.set_game_map(request)

    def test_permission_parity_for_participant_power_user_staff_and_denial(self):
        cases = (
            (100, 3, True),   # participant, level > 2
            (100, 2, False),  # participant, insufficient level
            (999, 4, True),  # power user, regardless of membership
            (999, 3, False), # ordinary nonparticipant
            (999, 5, True),  # staff level
        )
        for requester_id, level, allowed in cases:
            with self.subTest(requester_id=requester_id, level=level):
                state = {'map_type': 'Lakes'}
                database = FakeDatabase(state)
                game = self.make_game(state)
                self.patch_game(
                    game,
                    database,
                    log=lambda **kwargs: None,
                )
                request = map_request(
                    requester_id=requester_id,
                    requester_level=level,
                )
                if allowed:
                    game_workers.set_game_map(request)
                else:
                    with self.assertRaises(game_workers.GameMapPermissionError):
                        game_workers.set_game_map(request)

    def test_cross_guild_requires_related_channel_for_prefix_but_not_slash(self):
        state = {'map_type': 'Lakes'}
        database = FakeDatabase(state)
        game = self.make_game(state, guild_id=301)
        self.patch_game(game, database, log=lambda **kwargs: None)

        allowed = map_request(
            guild_id=300,
            channel_id=900,
            allow_related_channel=True,
        )
        game_workers.set_game_map(allowed)

        denied = map_request(
            guild_id=300,
            channel_id=900,
            allow_related_channel=False,
        )
        with self.assertRaisesRegex(
            game_workers.GameMapValidationError,
            'different discord server',
        ):
            game_workers.set_game_map(denied)

    def test_read_worker_is_separate_and_returns_current_value(self):
        database = FakeDatabase({})
        game = self.make_game({'map_type': 'Pangea'})
        with mock.patch.object(game_workers.models, 'db', database), \
                mock.patch.object(
                    game_workers.models.Game,
                    'get_by_id',
                    return_value=game,
                ), mock.patch.object(
                    game_workers.models.DiscordMember,
                    'get_or_none',
                    return_value=object(),
                ):
            result = game_workers.read_game_map(
                game_workers.GameMapReadRequest(
                    game_id=42,
                    guild_id=300,
                    channel_id=0,
                    requester_id=999,
                )
            )

        self.assertEqual(result.map_type, 'Pangea')
        self.assertEqual(result.game_id, 42)
        self.assertEqual(database.connection_closed, 1)

    def test_slow_mutation_worker_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            return game_workers.GameMapMutationResult(
                game_id=42,
                guild_id=300,
                old_map_type='Lakes',
                map_type='Dryland',
                announcement_channel_id=None,
                announcement_message_id=None,
            )

        async def run():
            with mock.patch.object(
                game_workers,
                'set_game_map',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    game_workers.run_game_map_mutation(map_request())
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
                release.set()
                await asyncio.sleep(0.05)
                return await task

        result = asyncio.run(run())
        self.assertEqual(result.map_type, 'Dryland')


class GameMapServiceTests(unittest.IsolatedAsyncioTestCase):
    def result(self, *, announcement=True):
        return game_workers.GameMapMutationResult(
            game_id=42,
            guild_id=300,
            old_map_type='Lakes',
            map_type='Dryland',
            announcement_channel_id=900 if announcement else None,
            announcement_message_id=901 if announcement else None,
        )

    async def test_same_game_claim_conflict_rejects_and_does_not_submit_worker(self):
        request = map_request()
        worker = mock.AsyncMock()
        with mock.patch.object(
            game_map.utilities,
            'lock_game',
            side_effect=exceptions.RecordLocked('already locked'),
        ), mock.patch.object(
            game_map.game_workers,
            'run_game_map_mutation',
            new=worker,
        ), mock.patch.object(
            game_map.utilities,
            'unlock_game',
        ) as unlock:
            with self.assertRaises(exceptions.RecordLocked):
                await game_map.run_map_mutation(request)

        worker.assert_not_awaited()
        unlock.assert_not_called()

    async def test_claim_cleanup_survives_post_commit_failure(self):
        request = map_request()
        events = []

        async def worker(_request):
            events.append('worker')
            return self.result(announcement=False)

        async def after(_result):
            events.append('after')
            raise RuntimeError('Discord failure')

        with mock.patch.object(
            game_map.utilities,
            'lock_game',
            side_effect=lambda game_id: events.append(('lock', game_id)),
        ), mock.patch.object(
            game_map.utilities,
            'unlock_game',
            side_effect=lambda game_id: events.append(('unlock', game_id)),
        ), mock.patch.object(
            game_map.game_workers,
            'run_game_map_mutation',
            side_effect=worker,
        ):
            with self.assertRaisesRegex(RuntimeError, 'Discord failure'):
                await game_map.run_map_mutation(request, after_commit=after)

        self.assertEqual(
            events,
            [('lock', 42), 'worker', ('unlock', 42), 'after'],
        )

    async def test_cancellation_keeps_claim_until_worker_finishes(self):
        request = map_request()
        started = threading.Event()
        release = threading.Event()
        active_games = set()
        events = []
        worker_calls = 0

        def lock(game_id):
            if game_id in active_games:
                raise exceptions.RecordLocked('already locked')
            active_games.add(game_id)
            events.append(('lock', game_id))

        def unlock(game_id):
            self.assertIn(game_id, active_games)
            active_games.remove(game_id)
            events.append(('unlock', game_id))

        def slow_worker(_request):
            nonlocal worker_calls
            worker_calls += 1
            started.set()
            if not release.wait(timeout=2):
                raise AssertionError('test worker was not released')
            events.append('worker-finished')
            return self.result(announcement=False)

        with mock.patch.object(
            game_map.utilities,
            'lock_game',
            side_effect=lock,
        ), mock.patch.object(
            game_map.utilities,
            'unlock_game',
            side_effect=unlock,
        ), mock.patch.object(
            game_map.game_workers,
            'set_game_map',
            side_effect=slow_worker,
        ):
            task = asyncio.create_task(game_map.run_map_mutation(request))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())

                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)

                self.assertFalse(task.done())
                self.assertNotIn(('unlock', 42), events)
                with self.assertRaises(exceptions.RecordLocked):
                    await game_map.run_map_mutation(request)
                self.assertEqual(worker_calls, 1)

                release.set()
                await asyncio.sleep(0.05)
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    with self.assertRaises(asyncio.CancelledError):
                        await task

        self.assertEqual(worker_calls, 1)
        self.assertEqual(
            events,
            [('lock', 42), 'worker-finished', ('unlock', 42)],
        )
        self.assertEqual(active_games, set())

    async def test_database_failure_has_no_post_commit_discord_callback(self):
        request = map_request()
        after = mock.AsyncMock()
        with mock.patch.object(
            game_map.game_workers,
            'run_game_map_mutation',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError('database down')
            ),
        ), mock.patch.object(game_map.utilities, 'lock_game'), mock.patch.object(
            game_map.utilities,
            'unlock_game',
        ):
            with self.assertRaises(peewee.OperationalError):
                await game_map.run_map_mutation(request, after_commit=after)
        after.assert_not_awaited()

    async def test_commit_precedes_card_refresh_and_failure_is_observable(self):
        events = []

        class Game:
            async def update_announcement(self, *, guild, prefix):
                events.append('refresh')
                return False

        async def send(content):
            events.append(('send', content))

        await game_map.publish_mutation_result(
            self.result(),
            send=send,
            guild=SimpleNamespace(),
            prefix='$',
            load_game=lambda *, game_id: Game(),
        )

        self.assertEqual(events[0][0], 'send')
        self.assertEqual(events[1], 'refresh')
        self.assertIn('announcement/card refresh failed', events[2][1])


class GameMapSlashTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        member = SimpleNamespace(id=100, display_name='Player')
        return SimpleNamespace(
            user=member,
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=901),
            channel_id=901,
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

    def map_command(self):
        return app_group('game').get_command('map')

    def test_registration_choices_and_optional_edit_options(self):
        command = self.map_command()
        parameters = {parameter.name: parameter for parameter in command.parameters}
        self.assertEqual(
            [(parameter.name, parameter.type, parameter.required) for parameter in command.parameters],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
                ('map_type', discord.AppCommandOptionType.string, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        self.assertEqual(
            [choice.value for choice in parameters['map_type'].choices],
            games.settings.map_types,
        )

    async def test_read_defers_then_returns_public_current_value(self):
        interaction = self.interaction()
        events = []

        async def defer(**kwargs):
            events.append(('defer', kwargs))

        async def read(_request):
            events.append('read')
            self.assertEqual(_request.channel_id, 901)
            return game_workers.GameMapReadResult(42, 300, 'Dryland')

        interaction.response.defer.side_effect = defer
        interaction.followup.send.side_effect = lambda *args, **kwargs: events.append(('send', args, kwargs))
        with mock.patch.object(game_map, '_requester_level', return_value=3), \
                mock.patch.object(games.game_map, 'run_map_read', side_effect=read):
            await self.map_command().callback(
                SimpleNamespace(),
                interaction,
                42,
                None,
                False,
            )

        self.assertEqual(
            events,
            [
                ('defer', {'ephemeral': True}),
                'read',
                ('send', mock.ANY, mock.ANY),
            ],
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertEqual(interaction.followup.send.call_args.args[0], 'Current map type for game 42: "Dryland".')
        self.assertFalse(interaction.followup.send.call_args.kwargs['ephemeral'])

    async def test_update_defers_and_publishes_public_success(self):
        interaction = self.interaction()
        events = []

        async def run(request, *, after_commit):
            events.append(('worker', request))
            await after_commit(
                game_workers.GameMapMutationResult(
                    42, 300, 'Lakes', 'Archipelago', None, None,
                )
            )

        async def publish(result, **kwargs):
            events.append(('publish', result))
            await kwargs['send'](game_map.mutation_message(result))

        interaction.response.defer.side_effect=lambda **kwargs: events.append(('defer', kwargs))
        with mock.patch.object(game_map, '_requester_level', return_value=3), \
                mock.patch.object(games.game_map, 'run_map_mutation', side_effect=run), \
                mock.patch.object(games.game_map, 'publish_mutation_result', side_effect=publish), \
                mock.patch.object(games.settings, 'guild_setting', return_value='$'):
            await self.map_command().callback(
                SimpleNamespace(),
                interaction,
                42,
                'Archipelago',
                False,
            )

        self.assertEqual(events[0], ('defer', {'ephemeral': True}))
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertEqual(events[1][0], 'worker')
        self.assertEqual(events[2][0], 'publish')
        interaction.followup.send.assert_awaited_once_with(
            'Map type for game 42 set to "Archipelago".',
            ephemeral=False,
        )
        self.assertEqual(events[1][1].clear, False)

    async def test_explicit_clear_and_conflicting_options(self):
        interaction = self.interaction()
        run = mock.AsyncMock()
        with mock.patch.object(games.game_map, 'run_map_mutation', new=run):
            await self.map_command().callback(
                SimpleNamespace(),
                interaction,
                42,
                None,
                True,
            )
            run.assert_awaited_once()
            self.assertTrue(run.call_args.args[0].clear)
            interaction.response.defer.assert_awaited_once_with(ephemeral=True)

        interaction = self.interaction()
        with mock.patch.object(games.game_map, 'run_map_mutation', new=run):
            await self.map_command().callback(
                SimpleNamespace(),
                interaction,
                42,
                'Lakes',
                True,
            )
        interaction.response.send_message.assert_awaited_once_with(
            'Choose either a map type or clear, not both.',
            ephemeral=True,
        )
        self.assertEqual(run.await_count, 1)
        interaction.response.defer.assert_not_awaited()

    async def test_denial_and_database_error_are_private_after_defer(self):
        for error in (
            game_workers.GameMapPermissionError('not authorized'),
            game_workers.GameMapValidationError(
                'Game 42 is associated with a different discord server.'
            ),
            peewee.OperationalError('database down'),
        ):
            with self.subTest(error=type(error).__name__):
                interaction = self.interaction()
                with mock.patch.object(game_map, '_requester_level', return_value=2), \
                        mock.patch.object(
                            games.game_map,
                            'run_map_mutation',
                            new=mock.AsyncMock(side_effect=error),
                        ):
                    await self.map_command().callback(
                        SimpleNamespace(),
                        interaction,
                        42,
                        'Lakes',
                        False,
                    )
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])


class GameMapPrefixTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_worker_registration_recheck_keeps_prefix_guidance(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'setmap'
        )
        author = SimpleNamespace(
            id=100,
            display_name='Player',
            guild=SimpleNamespace(id=300),
        )
        ctx = SimpleNamespace(
            author=author,
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=901),
            prefix='!',
            invoked_with='setmap',
            send=mock.AsyncMock(),
        )

        with mock.patch.object(
            game_map,
            'run_map_mutation',
            new=mock.AsyncMock(
                side_effect=game_workers.GameMapPermissionError(
                    'This command requires bot registration first. Type '
                    '__`setname Your Mobile Name`__ or  '
                    '__`steamname Your Steam Username`__ to get started.'
                )
            ),
        ):
            await command.callback(SimpleNamespace(), ctx, args='42 dry')

        ctx.send.assert_awaited_once_with(
            'This command requires bot registration first. Type '
            '__`!setname Your Mobile Name`__ or  '
            '__`!steamname Your Steam Username`__ to get started.'
        )

    async def test_prefix_wrong_argument_usage_keeps_configured_prefix(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'setmap'
        )
        author = SimpleNamespace(
            id=100,
            display_name='Player',
            guild=SimpleNamespace(id=300),
        )
        ctx = SimpleNamespace(
            author=author,
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=901),
            prefix='!',
            invoked_with='setmap',
            send=mock.AsyncMock(),
        )

        with mock.patch.object(
            game_map,
            'run_map_mutation',
            new=mock.AsyncMock(
                side_effect=game_workers.GameMapValidationError(
                    'Wrong number of arguments. See `help setmaptype` for '
                    'usage examples.'
                )
            ),
        ):
            await command.callback(SimpleNamespace(), ctx, args='42 dry extra')

        ctx.send.assert_awaited_once_with(
            'Wrong number of arguments. See `!help setmaptype` for usage '
            'examples.'
        )

    async def test_prefix_lookup_failure_keeps_alias_usage_guidance(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'setmap'
        )
        author = SimpleNamespace(
            id=100,
            display_name='Player',
            guild=SimpleNamespace(id=300),
        )
        ctx = SimpleNamespace(
            author=author,
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=901),
            prefix='$',
            invoked_with='setmaptype',
            send=mock.AsyncMock(),
        )

        with mock.patch.object(
            game_map,
            'run_map_mutation',
            new=mock.AsyncMock(
                side_effect=game_workers.GameMapLookupError(
                    'Non-numeric game ID *arch* is invalid.'
                )
            ),
        ):
            await command.callback(SimpleNamespace(), ctx, args='arch')

        ctx.send.assert_awaited_once_with(
            'Non-numeric game ID *arch* is invalid.\n'
            '**Example usage:** `$setmaptype 1234 dry`\n'
            'You can also omit the game ID if you use the command from a '
            'game-specific channel.'
        )

    async def test_prefix_alias_and_channel_inference_request_shared_service(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'setmap'
        )
        author = SimpleNamespace(
            id=100,
            display_name='Player',
            guild=SimpleNamespace(id=300),
        )
        ctx = SimpleNamespace(
            author=author,
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=901),
            prefix='$',
            invoked_with='setmaptype',
            send=mock.AsyncMock(),
        )
        request_seen = {}

        async def run(request, *, after_commit):
            request_seen['request'] = request
            return game_workers.GameMapMutationResult(
                42, 300, 'Lakes', 'Dryland', None, None,
            )

        with mock.patch.object(game_map, '_requester_level', return_value=3), \
                mock.patch.object(games.game_map, 'run_map_mutation', side_effect=run):
            await command.callback(SimpleNamespace(), ctx, args='dry')

        self.assertEqual(request_seen['request'].legacy_tokens, ('dry',))
        self.assertTrue(request_seen['request'].allow_related_channel)
        self.assertEqual(command.aliases, ['setmaptype'])
