"""Focused offline coverage for P4.4 permission-aware game logs."""

import asyncio
from dataclasses import FrozenInstanceError
import datetime
from types import SimpleNamespace
import time
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


games = import_offline_runtime('modules.games')
service = import_offline_runtime('modules.game_logs')
views = import_offline_runtime('modules.game_logs_views')
workers = import_offline_runtime('modules.game_log_workers')


def key(scope='guild', game_id=None, include=(), exclude=''):
    return workers.GameLogKey(
        scope=scope,
        game_id=game_id,
        include_terms=tuple(include),
        exclude_term=exclude,
    )


def request(selected_key=None, *, staff=True, owner=False, requester_id=10):
    return workers.GameLogRequest(
        guild_id=300,
        requester_id=requester_id,
        requester_is_staff=staff,
        requester_is_owner=owner,
        key=selected_key or key(),
    )


def snapshot(selected_key=None, count=23):
    selected_key = selected_key or key()
    return workers.GameLogSnapshot(
        key=selected_key,
        title='Audit logs',
        rows=tuple(
            workers.GameLogRow(
                log_id=index + 1,
                guild_id=300 if index % 2 else 0,
                timestamp=f'2026-08-08 12:{index:02d}:00',
                message=f'__321__ - **Actor** changed entry {index + 1}',
                message_truncated=False,
            )
            for index in range(count)
        ),
        truncated=False,
    )


class GameLogRegistrationTests(unittest.TestCase):
    def test_native_shape_and_retained_prefix_aliases(self):
        game_group = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        command = game_group.get_command('logs')
        self.assertIsNotNone(command)
        self.assertEqual(
            [(parameter.name, parameter.required, parameter.type)
             for parameter in command.parameters],
            [('game_id', False, discord.AppCommandOptionType.integer)],
        )
        prefix = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'logs'
        )
        self.assertEqual(
            set(prefix.aliases),
            {'gamelog', 'gamelogs', 'global_logs', 'log'},
        )


class GameLogParsingTests(unittest.TestCase):
    def test_mentions_and_first_negative_term_are_normalized(self):
        include, exclude = service.parse_search_terms(
            '<@123456789012345678> joined -left another'
        )
        self.assertEqual(
            include,
            ('123456789012345678', 'joined', 'another'),
        )
        self.assertEqual(exclude, 'left')

    def test_native_nonstaff_requires_game_but_staff_defaults_to_guild(self):
        member = SimpleNamespace(id=10)
        with mock.patch.object(service.settings, 'is_staff', return_value=False):
            with self.assertRaises(workers.GameLogPermissionError):
                service.initial_key(member=member, game_id=None)
            self.assertEqual(
                service.initial_key(member=member, game_id=321),
                key(scope='game', game_id=321),
            )
        with mock.patch.object(service.settings, 'is_staff', return_value=True):
            self.assertEqual(service.initial_key(member=member, game_id=None), key())

    def test_owner_without_staff_role_can_default_to_guild(self):
        member = SimpleNamespace(id=10)
        with mock.patch.object(service.settings, 'is_staff', return_value=False), \
                mock.patch.object(service.settings, 'owner_id', 10):
            self.assertEqual(service.initial_key(member=member, game_id=None), key())

    def test_global_prefix_scope_is_owner_checked_in_worker(self):
        member = SimpleNamespace(id=10)
        selected = service.legacy_key(
            member=member,
            search_term='Nelluk -leave',
            invoked_with='global_logs',
        )
        self.assertEqual(selected.scope, 'global')
        self.assertEqual(selected.include_terms, ('Nelluk',))
        self.assertEqual(selected.exclude_term, 'leave')

    def test_legacy_mixed_game_id_search_keeps_exact_marker_semantics(self):
        member = SimpleNamespace(id=10)
        with mock.patch.object(service.settings, 'is_staff', return_value=True):
            selected = service.legacy_key(
                member=member,
                search_term='3210 joined',
                invoked_with='logs',
            )
        self.assertEqual(selected.include_terms, ('__3210__', 'joined'))


class GameLogWorkerTests(unittest.TestCase):
    def test_requests_are_frozen_primitive_snapshots(self):
        selected = request(key(scope='game', game_id=321))
        with self.assertRaises(FrozenInstanceError):
            selected.guild_id = 999
        self.assertIsInstance(selected.key.include_terms, tuple)

    def test_permission_matrix_rejects_broad_nonstaff_and_nonowner_global(self):
        with self.assertRaises(workers.GameLogPermissionError):
            workers._validate_scope(request(staff=False), key())
        with self.assertRaises(workers.GameLogPermissionError):
            workers._validate_scope(
                request(key(scope='global'), owner=False),
                key(scope='global'),
            )

    def test_exact_owner_can_read_guild_without_staff_role(self):
        selected = request(key(), staff=False, owner=True, requester_id=10)
        with mock.patch.object(workers.settings, 'owner_id', 10):
            workers._validate_scope(selected, key())

    def test_game_scope_requires_same_guild_participation_for_nonstaff(self):
        selected = key(scope='game', game_id=321)
        game = SimpleNamespace(id=321, guild_id=300)
        with mock.patch.object(workers.models.Game, 'get_or_none', return_value=game), \
                mock.patch.object(workers, '_requester_is_game_participant', return_value=False):
            with self.assertRaises(workers.GameLogPermissionError):
                workers._validate_scope(request(selected, staff=False), selected)
        with mock.patch.object(workers.models.Game, 'get_or_none', return_value=game), \
                mock.patch.object(workers, '_requester_is_game_participant', return_value=True):
            workers._validate_scope(request(selected, staff=False), selected)

    def test_worker_closes_connection_returns_bounded_ordered_dtos(self):
        database = FakeDatabase({})
        entries = tuple(
            SimpleNamespace(
                id=index,
                guild_id=300,
                message_ts=datetime.datetime(2026, 8, 8, 12, index),
                message='x' * (workers.MAX_ROW_MESSAGE_LENGTH + index),
            )
            for index in range(1, 4)
        )
        with mock.patch.object(workers.models, 'db', database), \
                mock.patch.object(workers, '_validate_scope'), \
                mock.patch.object(workers, '_query_logs', return_value=(entries, 'Recent')):
            result = workers.read_game_logs(request())
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual([row.log_id for row in result.rows], [1, 2, 3])
        self.assertTrue(all(row.message_truncated for row in result.rows))
        self.assertTrue(all(len(row.message) == workers.MAX_ROW_MESSAGE_LENGTH for row in result.rows))

    def test_slow_read_keeps_event_loop_responsive_and_cancellation_drains(self):
        result = snapshot(count=1)

        def slow_read(_request):
            time.sleep(0.05)
            return result

        async def exercise():
            with mock.patch.object(workers, 'read_game_logs', side_effect=slow_read):
                task = asyncio.create_task(workers.run_game_log_read(request()))
                await asyncio.sleep(0)
                started = time.monotonic()
                await asyncio.sleep(0.005)
                self.assertLess(time.monotonic() - started, 0.04)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1.0)

        asyncio.run(exercise())


class GameLogWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, *, initial=None, loader=None, staff=True, owner=False, game_id=321):
        return views.GameLogsWorkspace(
            requester_id=10,
            initial_result=initial or snapshot(key(scope='game', game_id=game_id)),
            loader=loader or mock.AsyncMock(),
            requester_is_staff=staff,
            requester_is_owner=owner,
            initial_game_id=game_id,
        )

    async def test_public_workspace_controls_are_requester_only(self):
        view = self.make_view()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=11),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once_with(
            view.unauthorized_message,
            ephemeral=True,
        )

    async def test_paging_is_snapshot_only(self):
        loader = mock.AsyncMock()
        view = self.make_view(loader=loader)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
        )
        await view.show_next(interaction)
        self.assertEqual(view.page_index, 1)
        loader.assert_not_awaited()

    async def test_staff_scope_change_loads_once_and_then_uses_cache(self):
        initial = snapshot(key(scope='game', game_id=321), count=1)
        guild_key = key(scope='guild')
        guild_result = snapshot(guild_key, count=2)
        loader = mock.AsyncMock(return_value=guild_result)
        view = self.make_view(initial=initial, loader=loader, staff=True)

        class Response:
            def __init__(self):
                self.defer = mock.AsyncMock()

            def is_done(self):
                return bool(self.defer.await_count)

        interaction = SimpleNamespace(
            response=Response(),
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        view.scope_select._values = ['guild']
        await view._change_scope(interaction)
        loader.assert_awaited_once_with(guild_key)
        self.assertEqual(view.result.key, guild_key)

    def test_owner_gets_global_scope_and_nonstaff_does_not(self):
        owner_view = self.make_view(owner=True)
        self.assertIn('global', [value for value, _ in owner_view._scope_options()])
        self.assertIn('guild', [value for value, _ in owner_view._scope_options()])
        participant_view = self.make_view(staff=False, owner=False)
        self.assertEqual(participant_view._scope_options(), [('game', 'Game 321')])

    def test_serialized_action_rows_match_discord_contract(self):
        def action_rows(components):
            for component in components:
                if component.get('type') == 1:
                    yield component
                yield from action_rows(component.get('components', ()))

        for view in (
            self.make_view(staff=False),
            self.make_view(staff=True),
            self.make_view(staff=True, owner=True),
        ):
            for action_row in action_rows(view.to_components()):
                children = tuple(action_row.get('components', ()))
                self.assertTrue(children)
                types = tuple(child.get('type') for child in children)
                if any(component_type != 2 for component_type in types):
                    self.assertEqual(len(children), 1)
                    self.assertIn(types[0], {3, 5, 6, 7, 8})


class GameLogSlashLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def command(self):
        game_group = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        return game_group.get_command('logs')

    def interaction(self):
        response = SimpleNamespace(defer=mock.AsyncMock())
        followup = SimpleNamespace(send=mock.AsyncMock())
        channel = SimpleNamespace(send=mock.AsyncMock(return_value=object()))
        return SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=300),
            channel_id=900,
            channel=channel,
            response=response,
            followup=followup,
            delete_original_response=mock.AsyncMock(),
        )

    async def test_success_defers_private_then_publishes_public_workspace(self):
        interaction = self.interaction()
        loaded = snapshot(key(scope='game', game_id=321), count=1)
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_logs,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            games.game_log_workers,
            'run_game_log_read',
            new=mock.AsyncMock(return_value=loaded),
        ), mock.patch.object(
            games.settings,
            'is_staff',
            return_value=False,
        ), mock.patch.object(games.settings, 'owner_id', 99):
            workspace = await self.command().callback(cog, interaction, 321)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertIs(interaction.channel.send.await_args.kwargs['view'], workspace)
        self.assertIsInstance(
            interaction.channel.send.await_args.kwargs['allowed_mentions'],
            discord.AllowedMentions,
        )
        interaction.followup.send.assert_not_awaited()

    async def test_permission_failure_remains_private_without_publication(self):
        interaction = self.interaction()
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_logs,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            games.game_logs,
            'initial_key',
            side_effect=workers.GameLogPermissionError('Choose a game.'),
        ):
            await self.command().callback(cog, interaction, None)

        interaction.followup.send.assert_awaited_once_with(
            'Choose a game.',
            ephemeral=True,
        )
        interaction.channel.send.assert_not_awaited()
        interaction.delete_original_response.assert_not_awaited()


class GameLogPrefixTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_adapter_uses_shared_worker_and_classic_pagination(self):
        context = SimpleNamespace(
            author=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=300),
            invoked_with='logs',
            bot=object(),
        )
        loaded = snapshot(key(scope='game', game_id=321), count=2)
        with mock.patch.object(service.settings, 'is_staff', return_value=False), \
                mock.patch.object(service.settings, 'owner_id', 99), \
                mock.patch.object(workers, 'run_game_log_read', return_value=loaded) as reader, \
                mock.patch.object(service.utilities, 'paginate', new=mock.AsyncMock()) as paginate:
            await service.run_prefix(context, '321')
        self.assertEqual(reader.await_args.args[0].key, key(scope='game', game_id=321))
        self.assertEqual(len(paginate.await_args.kwargs['message_list']), 2)


if __name__ == '__main__':
    unittest.main()
