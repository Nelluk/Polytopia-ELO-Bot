"""Focused offline coverage for the P7.10 team leaderboard workspace."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import time
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.team_leaderboard_workers')
worker_impl = import_offline_runtime('modules.leaderboard_workers')
service = import_offline_runtime('modules.team_leaderboard')
views = import_offline_runtime('modules.team_leaderboard_views')
games = import_offline_runtime('modules.games')


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1
                return False

        return ConnectionContext()


class FakeQuery:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def count(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


def row(
    index,
    *,
    archived=False,
    tier=1,
    elo=None,
    history=None,
):
    return workers.TeamLeaderboardRow(
        rank=index,
        team_id=10_000 + index,
        team_name=f'Team {index:02d}',
        team_emoji='🏹',
        tier_number=tier,
        tier_name='Platinum' if tier == 1 else 'Gold',
        is_archived=archived,
        member_count=index % 4,
        role_color='#123456',
        elo=elo if elo is not None else 1600 - index,
        wins=index,
        losses=index // 2,
        history=history or (),
    )


def result(count=31):
    return workers.TeamLeaderboardResult(
        total_teams=count,
        rows=tuple(
            row(
                index,
                archived=index % 7 == 0,
                tier=1 if index % 2 else 2,
            )
            for index in range(1, count + 1)
        ),
        graph_attachment_name='team-elo-request.png',
        loaded_all_filters=True,
    )


class FakeResponse:
    def __init__(self):
        self.done = False
        self.defer = mock.AsyncMock(side_effect=self._mark_done)
        self.send_message = mock.AsyncMock(side_effect=self._mark_done)
        self.send_modal = mock.AsyncMock(side_effect=self._mark_done)

    async def _mark_done(self, *args, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


def interaction(user_id=777):
    response = FakeResponse()
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=response,
        followup=SimpleNamespace(send=mock.AsyncMock()),
        edit_original_response=mock.AsyncMock(),
        response_object=response,
    )


class PrefixMatrixTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_filter_matrix_and_invalid_tier(self):
        self.assertEqual(
            service.parse_prefix_filters(None),
            (None, None, False),
        )
        self.assertEqual(
            service.parse_prefix_filters('silver'),
            (3, 'Silver', False),
        )
        self.assertEqual(
            service.parse_prefix_filters('old'),
            (None, None, True),
        )
        self.assertEqual(
            service.parse_prefix_filters('old 3'),
            (3, 'Silver', True),
        )
        with self.assertRaises(Exception):
            service.parse_prefix_filters('not-a-tier')

    def test_prefix_aliases_keep_junior_as_prefix_only_alias(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'lbteam'
        )
        self.assertEqual(set(command.aliases), {'teamlb', 'lbteamjr'})
        leaderboard = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'leaderboard'
        )
        self.assertNotIn('lbteamjr', {command.name for command in leaderboard.commands})
        self.assertNotIn('junior', {command.name for command in leaderboard.commands})

    async def test_prefix_callback_preserves_old_tier_and_teamlb_requests(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'lbteam'
        )
        guild_id = 478571892832206869
        strict_channel = 479292913080336397
        guild = SimpleNamespace(id=guild_id, roles=())
        author = SimpleNamespace(
            id=777,
            roles=(),
            guild=guild,
        )

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        cog = object.__new__(games.polygames)
        for invoked_with, arg, expected_tier, expected_archived in (
            ('lbteam', None, None, False),
            ('teamlb', 'silver', 3, False),
            ('lbteam', 'old', None, True),
            ('teamlb', 'old 3', 3, True),
        ):
            ctx = SimpleNamespace(
                author=author,
                guild=guild,
                message=SimpleNamespace(
                    channel=SimpleNamespace(id=strict_channel),
                ),
                prefix='!',
                invoked_with=invoked_with,
                typing=lambda: Typing(),
                send=mock.AsyncMock(),
            )
            with mock.patch.object(
                games.team_leaderboard_workers,
                'run_team_leaderboard',
                new=mock.AsyncMock(return_value=result(1)),
            ) as run, mock.patch.object(
                games.team_leaderboard_service,
                'publish_prefix',
                new=mock.AsyncMock(),
            ) as publish:
                await command.callback(cog, ctx, arg=arg)

            request = run.await_args.args[0]
            self.assertEqual(request.tier_number, expected_tier)
            self.assertEqual(request.include_archived, expected_archived)
            publish.assert_awaited_once()

    async def test_prefix_invalid_tier_preserves_error_guidance(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'lbteam'
        )
        guild = SimpleNamespace(id=478571892832206869, roles=())
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=777, roles=(), guild=guild),
            guild=guild,
            message=SimpleNamespace(channel=SimpleNamespace(id=479292913080336397)),
            prefix='!',
            invoked_with='teamlb',
            send=mock.AsyncMock(),
        )
        cog = object.__new__(games.polygames)
        await command.callback(cog, ctx, arg='not-a-tier')
        self.assertIn('**not-a-tier**', ctx.send.await_args.args[0])
        self.assertIn('!help teamlb', ctx.send.await_args.args[0])


class ChannelParityTests(unittest.TestCase):
    def _allowed(self, values, channel_id, *, is_mod=False):
        def setting(_guild_id, name, default=None):
            return values.get(name, default)

        with mock.patch.object(
            service,
            '_setting',
            side_effect=setting,
        ), mock.patch.object(
            service,
            '_is_mod',
            return_value=is_mod,
        ):
            return service._channel_allowed(
                SimpleNamespace(id=777),
                300,
                channel_id,
            )

    def test_strict_only_configuration_takes_precedence_and_keeps_private(self):
        values = {
            'bot_channels': None,
            'bot_channels_strict': [200],
            'bot_channels_private': [300],
        }
        self.assertTrue(self._allowed(values, 200))
        self.assertTrue(self._allowed(values, 300))
        self.assertFalse(self._allowed(values, 100))

    def test_ordinary_channels_are_used_when_strict_is_unset(self):
        values = {
            'bot_channels': [100],
            'bot_channels_strict': None,
            'bot_channels_private': [300],
        }
        self.assertTrue(self._allowed(values, 100))
        self.assertTrue(self._allowed(values, 300))
        self.assertFalse(self._allowed(values, 200))

    def test_both_channel_lists_unset_allow_everywhere_and_mods_bypass(self):
        self.assertTrue(self._allowed(
            {'bot_channels': None, 'bot_channels_strict': None},
            999,
        ))
        self.assertTrue(self._allowed(
            {
                'bot_channels': None,
                'bot_channels_strict': [200],
            },
            999,
            is_mod=True,
        ))


class TeamLeaderboardWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_uses_local_connection_and_current_record(self):
        database = FakeDatabase()
        observed = {}
        team = SimpleNamespace(
            id=42,
            name='Ronin',
            emoji='⚔️',
            league_tier=2,
            is_archived=False,
            elo=1234,
            elo_alltime=9999,
            get_record=lambda **kwargs: observed.update(kwargs) or (12, 8),
        )
        request_value = workers.TeamLeaderboardRequest(
            guild_id=300,
            database_guild_id=300,
            role_snapshots=(
                workers.TeamLeaderboardRoleSnapshot('Ronin', '#abcdef', 4),
            ),
            graph_attachment_name='request.png',
        )
        with mock.patch.object(worker_impl.models, 'db', database), mock.patch.object(
            worker_impl.models.Team,
            'select',
            return_value=FakeQuery((team,)),
        ), mock.patch.object(
            worker_impl,
            '_team_history_rows',
            return_value=((datetime.datetime(2026, 1, 1), 1234),),
        ), mock.patch.object(
            worker_impl.settings,
            'tier_lookup',
            return_value=(2, 'Gold'),
        ):
            loaded = workers.load_team_leaderboard(request_value)

        self.assertEqual((database.opened, database.closed), (1, 1))
        self.assertEqual(observed, {'alltime': False})
        self.assertEqual(loaded.rows[0].elo, 1234)
        self.assertEqual(loaded.rows[0].member_count, 4)
        self.assertEqual(loaded.rows[0].role_color, '#abcdef')
        self.assertEqual(loaded.rows[0].history[0][1], 1234)
        self.assertIsInstance(loaded.rows, tuple)
        with self.assertRaises(FrozenInstanceError):
            request_value.guild_id = 999
        with self.assertRaises(FrozenInstanceError):
            loaded.rows[0].elo = 1

    def test_worker_loads_all_matching_teams_without_25_row_cap(self):
        teams = tuple(
            SimpleNamespace(
                id=index,
                name=f'Team {index}',
                emoji='',
                league_tier=1,
                is_archived=False,
                elo=2000 - index,
                get_record=lambda **kwargs: (1, 2),
            )
            for index in range(1, 32)
        )
        database = FakeDatabase()
        request_value = workers.TeamLeaderboardRequest(
            guild_id=300,
            database_guild_id=300,
            graph_attachment_name='request.png',
        )
        with mock.patch.object(worker_impl.models, 'db', database), mock.patch.object(
            worker_impl.models.Team,
            'select',
            return_value=FakeQuery(teams),
        ), mock.patch.object(worker_impl, '_team_history_rows', return_value=()), mock.patch.object(
            worker_impl.settings,
            'tier_lookup',
            return_value=(1, 'Platinum'),
        ):
            loaded = workers.load_team_leaderboard(request_value)
        self.assertEqual(loaded.total_teams, 31)
        self.assertEqual(len(loaded.rows), 31)

    def test_discord_adapters_require_exact_roles_but_zero_member_roles_match(self):
        guild_id = 478571892832206869
        channel_id = 479292913080336397
        team = SimpleNamespace(
            id=42,
            name='Ronin',
            emoji='⚔️',
            league_tier=2,
            is_archived=False,
            elo=1234,
            get_record=lambda **kwargs: (12, 8),
        )
        database = FakeDatabase()

        def make_requests(roles):
            guild = SimpleNamespace(id=guild_id, roles=roles)
            member = SimpleNamespace(id=777, roles=(), guild=guild)
            ctx = SimpleNamespace(
                author=member,
                guild=guild,
                message=SimpleNamespace(
                    channel=SimpleNamespace(id=channel_id),
                ),
            )
            native = SimpleNamespace(
                user=member,
                guild=guild,
                channel_id=channel_id,
            )
            return (
                service.team_leaderboard_request_for_prefix(
                    ctx=ctx,
                    tier_number=None,
                    include_archived=False,
                ),
                service.team_leaderboard_request_for_native(native),
            )

        def load(request_value):
            with mock.patch.object(worker_impl.models, 'db', database), mock.patch.object(
                worker_impl.models.Team,
                'select',
                return_value=FakeQuery((team,)),
            ), mock.patch.object(
                worker_impl,
                '_team_history_rows',
                return_value=(),
            ), mock.patch.object(
                worker_impl.settings,
                'tier_lookup',
                return_value=(2, 'Gold'),
            ):
                return workers.load_team_leaderboard(request_value)

        missing_prefix, missing_native = make_requests(())
        self.assertTrue(missing_prefix.require_role_match)
        self.assertTrue(missing_native.require_role_match)
        with mock.patch.object(worker_impl.logger, 'warning') as warning:
            missing_results = tuple(
                load(request_value)
                for request_value in (missing_prefix, missing_native)
            )
        self.assertEqual(
            [result.rows for result in missing_results],
            [(), ()],
        )
        self.assertEqual(warning.call_count, 2)

        zero_member_role = SimpleNamespace(
            name='Ronin',
            color=SimpleNamespace(value=0xABCDEF),
            members=[],
        )
        present_prefix, present_native = make_requests((zero_member_role,))
        present_results = tuple(
            load(request_value)
            for request_value in (present_prefix, present_native)
        )
        self.assertEqual(
            [len(result.rows) for result in present_results],
            [1, 1],
        )
        self.assertEqual(
            [result.rows[0].member_count for result in present_results],
            [0, 0],
        )

        db_only = workers.TeamLeaderboardRequest(
            guild_id=guild_id,
            database_guild_id=guild_id,
            graph_attachment_name='db-only.png',
        )
        self.assertEqual(len(load(db_only).rows), 1)

    def test_filter_page_recomputes_deterministic_ranks_and_pages(self):
        loaded = result(31)
        active = workers.team_leaderboard_page(loaded, page_index=2)
        self.assertEqual(active.page_count, 3)
        self.assertEqual([item.rank for item in active.rows], [21, 22, 23, 24, 25, 26, 27])
        archived = workers.team_leaderboard_page(
            loaded,
            include_archived=True,
            page_index=2,
        )
        self.assertEqual(archived.page_count, 4)
        self.assertEqual(archived.rows[0].rank, 21)
        tier = workers.team_leaderboard_page(
            loaded,
            tier_number=2,
            page_index=0,
        )
        self.assertEqual([item.rank for item in tier.rows], list(range(1, 11)))

    async def test_slow_read_and_graph_keep_event_loop_responsive(self):
        original_load = worker_impl.load_team_leaderboard
        original_render = worker_impl.render_team_leaderboard_graph

        def slow_load(request):
            time.sleep(0.06)
            return result(1)

        def slow_render(page, filename):
            time.sleep(0.06)
            return workers.TeamLeaderboardGraph(filename, b'graph')

        worker_impl.load_team_leaderboard = slow_load
        worker_impl.render_team_leaderboard_graph = slow_render
        try:
            heartbeat = asyncio.create_task(asyncio.sleep(0.01))
            read_task = asyncio.create_task(
                workers.run_team_leaderboard(
                    workers.TeamLeaderboardRequest(guild_id=300),
                )
            )
            await asyncio.wait_for(heartbeat, timeout=0.04)
            self.assertFalse(read_task.done())
            loaded = await read_task
            graph_task = asyncio.create_task(
                workers.run_team_leaderboard_graph(
                    workers.team_leaderboard_page(loaded),
                    'graph.png',
                )
            )
            heartbeat = asyncio.create_task(asyncio.sleep(0.01))
            await asyncio.wait_for(heartbeat, timeout=0.04)
            self.assertFalse(graph_task.done())
            self.assertEqual((await graph_task).png_bytes, b'graph')
        finally:
            worker_impl.load_team_leaderboard = original_load
            worker_impl.render_team_leaderboard_graph = original_render


class TeamLeaderboardBoundaryAndGraphTests(unittest.TestCase):
    def test_role_snapshot_freezes_exact_role_counts_and_colors(self):
        active = SimpleNamespace(id=101)
        inactive = SimpleNamespace(id=202)
        inactive_role = SimpleNamespace(
            name='MIA',
            color=SimpleNamespace(value=0x111111),
            members=[inactive],
        )
        team_role = SimpleNamespace(
            name='Ronin',
            color=SimpleNamespace(value=0xABCDEF),
            members=[active, inactive],
        )
        guild = SimpleNamespace(id=300, roles=[inactive_role, team_role])
        snapshots = service.capture_role_snapshots(
            guild,
            inactive_role_name='MIA',
        )
        by_name = {snapshot.role_name: snapshot for snapshot in snapshots}
        self.assertEqual(by_name['Ronin'].active_member_count, 1)
        self.assertEqual(by_name['Ronin'].role_color, '#abcdef')
        self.assertIsInstance(snapshots, tuple)
        with self.assertRaises(FrozenInstanceError):
            snapshots[0].role_name = 'changed'

    def test_attachment_names_and_graph_renderer_are_request_owned(self):
        first = service._attachment_name()
        second = service._attachment_name()
        self.assertNotEqual(first, second)
        source = inspect.getsource(workers.render_team_leaderboard_graph)
        self.assertIn('FigureCanvasAgg', source)
        self.assertIn('Figure', source)
        self.assertNotIn('pyplot', source)
        self.assertNotIn('graph.png', source)
        graph = workers.render_team_leaderboard_graph(
            workers.team_leaderboard_page(
                workers.TeamLeaderboardResult(
                    1,
                    (row(
                        1,
                        history=(
                            (datetime.datetime(2026, 1, 1), 1200),
                            (datetime.datetime(2026, 2, 1), 1250),
                        ),
                    ),),
                    first,
                    True,
                ),
            ),
            first,
        )
        self.assertEqual(graph.filename, first)
        self.assertTrue(graph.png_bytes.startswith(b'\x89PNG\r\n\x1a\n'))


class TeamLeaderboardViewTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, *, graph_bytes=b'graph'):
        graph = workers.TeamLeaderboardGraph(
            'team-elo-request.png',
            graph_bytes,
        )
        calls = []

        async def graph_loader(page, filename):
            calls.append((page, filename))
            return workers.TeamLeaderboardGraph(filename, graph_bytes)

        view = views.TeamLeaderboardWorkspace(
            requester_id=777,
            result=result(31),
            tier_choices=((1, 'Platinum'), (2, 'Gold')),
            graph=graph,
            graph_loader=graph_loader,
        )
        return view, calls

    def test_native_workspace_has_one_common_filter_and_page_controls(self):
        view, _calls = self.make_view()
        self.assertIsInstance(view, discord.ui.LayoutView)
        selects = [
            item for item in view.walk_children()
            if isinstance(item, discord.ui.Select)
        ]
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0].placeholder, 'Common filters')
        self.assertEqual(
            [option.value for option in selects[0].options if option.default],
            ['active:all'],
        )
        self.assertEqual(view.page_count, 3)
        buttons = [
            item for item in view.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        self.assertTrue(any(item.label.startswith('Jump to page') for item in buttons))
        self.assertTrue(any(item.label == 'Next' for item in buttons))
        self.assertTrue(any('Team 01' in item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)))

    async def test_requester_controls_refine_reset_and_page_without_new_db_load(self):
        view, calls = self.make_view()
        denied = interaction(888)
        self.assertFalse(await view.interaction_check(denied))
        denied.response.send_message.assert_awaited_once_with(
            view.unauthorized_message,
            ephemeral=True,
        )

        allowed = interaction(777)
        await view._apply_filter(allowed, 'archived:2')
        self.assertEqual((view.tier_number, view.include_archived), (2, True))
        self.assertEqual(view.page_index, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].total_teams, 15)
        allowed.edit_original_response.assert_awaited_once()

        reset = interaction(777)
        await view._reset_filters(reset)
        self.assertEqual((view.tier_number, view.include_archived), (None, False))
        self.assertEqual(len(calls), 2)

        next_page = interaction(777)
        await view._next_page(next_page)
        self.assertEqual(view.page_index, 1)
        self.assertEqual(len(calls), 3)

    async def test_expired_controls_are_private_and_point_to_rerun(self):
        view, _calls = self.make_view()
        view.stop()
        expired = interaction(777)
        await view._next_page(expired)
        expired.response.send_message.assert_awaited_once_with(
            view.expired_message,
            ephemeral=True,
        )
        self.assertIn('Run the command again', view.expired_message)

    async def test_page_jump_modal_reuses_snapshot_and_refreshes_graph(self):
        view, calls = self.make_view()
        modal = views.TeamLeaderboardPageJumpModal(view)
        modal.page_number._value = '3'
        jump = interaction(777)
        await modal.on_submit(jump)
        self.assertEqual(view.page_index, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].page_index, 2)
        jump.edit_original_response.assert_awaited_once()

    async def test_native_command_defers_private_then_publishes_public(self):
        command = next(
            command
            for command in next(
                command
                for command in games.polygames.__cog_app_commands__
                if command.name == 'leaderboard'
            ).commands
            if command.name == 'teams'
        )
        response = FakeResponse()
        public_message = SimpleNamespace(id=99)
        channel = SimpleNamespace(send=mock.AsyncMock(return_value=public_message))
        native_interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            guild=SimpleNamespace(id=300),
            channel_id=400,
            channel=channel,
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )
        page = workers.team_leaderboard_page(result(1))
        graph = workers.TeamLeaderboardGraph('native.png', b'graph')
        request_value = workers.TeamLeaderboardRequest(guild_id=300)
        cog = object.__new__(games.polygames)
        with mock.patch.object(
            games.team_leaderboard_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            games.team_leaderboard_service,
            'team_leaderboard_request_for_native',
            return_value=request_value,
        ), mock.patch.object(
            games.team_leaderboard_workers,
            'run_team_leaderboard',
            new=mock.AsyncMock(return_value=result(1)),
        ), mock.patch.object(
            games.team_leaderboard_service,
            'render_page_graph',
            new=mock.AsyncMock(return_value=(page, graph)),
        ):
            await command.callback(cog, native_interaction)

        response.defer.assert_awaited_once_with(ephemeral=True)
        native_interaction.delete_original_response.assert_awaited_once()
        channel.send.assert_awaited_once()
        self.assertIs(channel.send.await_args.kwargs['view'].message, public_message)
        native_interaction.followup.send.assert_not_awaited()

    async def test_native_command_fetches_channel_and_never_uses_public_followup(self):
        command = next(
            command
            for command in next(
                command
                for command in games.polygames.__cog_app_commands__
                if command.name == 'leaderboard'
            ).commands
            if command.name == 'teams'
        )
        response = FakeResponse()
        public_channel = SimpleNamespace(
            send=mock.AsyncMock(return_value=SimpleNamespace(id=99))
        )
        client = SimpleNamespace(
            get_channel=mock.Mock(return_value=None),
            fetch_channel=mock.AsyncMock(return_value=public_channel),
        )
        native_interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            guild=SimpleNamespace(id=300),
            channel_id=400,
            channel=None,
            client=client,
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )
        page = workers.team_leaderboard_page(result(1))
        graph = workers.TeamLeaderboardGraph('native.png', b'graph')
        cog = object.__new__(games.polygames)
        with mock.patch.object(
            games.team_leaderboard_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            games.team_leaderboard_service,
            'team_leaderboard_request_for_native',
            return_value=workers.TeamLeaderboardRequest(guild_id=300),
        ), mock.patch.object(
            games.team_leaderboard_workers,
            'run_team_leaderboard',
            new=mock.AsyncMock(return_value=result(1)),
        ), mock.patch.object(
            games.team_leaderboard_service,
            'render_page_graph',
            new=mock.AsyncMock(return_value=(page, graph)),
        ):
            await command.callback(cog, native_interaction)

        client.fetch_channel.assert_awaited_once_with(400)
        public_channel.send.assert_awaited_once()
        native_interaction.followup.send.assert_not_awaited()

    async def test_native_command_reports_private_failure_without_destination(self):
        command = next(
            command
            for command in next(
                command
                for command in games.polygames.__cog_app_commands__
                if command.name == 'leaderboard'
            ).commands
            if command.name == 'teams'
        )
        response = FakeResponse()
        native_interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            guild=SimpleNamespace(id=300),
            channel_id=400,
            channel=None,
            client=SimpleNamespace(
                get_channel=mock.Mock(return_value=None),
                fetch_channel=mock.AsyncMock(return_value=None),
            ),
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )
        page = workers.team_leaderboard_page(result(1))
        graph = workers.TeamLeaderboardGraph('native.png', b'graph')
        cog = object.__new__(games.polygames)
        with mock.patch.object(
            games.team_leaderboard_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            games.team_leaderboard_service,
            'team_leaderboard_request_for_native',
            return_value=workers.TeamLeaderboardRequest(guild_id=300),
        ), mock.patch.object(
            games.team_leaderboard_workers,
            'run_team_leaderboard',
            new=mock.AsyncMock(return_value=result(1)),
        ), mock.patch.object(
            games.team_leaderboard_service,
            'render_page_graph',
            new=mock.AsyncMock(return_value=(page, graph)),
        ), mock.patch.object(games.logger, 'exception'):
            await command.callback(cog, native_interaction)

        native_interaction.delete_original_response.assert_not_awaited()
        message = native_interaction.followup.send.await_args.args[0]
        self.assertIn('not published publicly', message)
        self.assertTrue(
            native_interaction.followup.send.await_args.kwargs['ephemeral']
        )

    async def test_native_access_failure_stays_private_after_immediate_defer(self):
        command = next(
            command
            for command in next(
                command
                for command in games.polygames.__cog_app_commands__
                if command.name == 'leaderboard'
            ).commands
            if command.name == 'teams'
        )
        response = FakeResponse()
        native_interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            guild=SimpleNamespace(id=300),
            channel_id=400,
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog = object.__new__(games.polygames)
        with mock.patch.object(
            games.team_leaderboard_service,
            'native_access_error',
            return_value='This command can only be used in a designated bot spam channel.',
        ), mock.patch.object(
            games.team_leaderboard_workers,
            'run_team_leaderboard',
            new=mock.AsyncMock(),
        ) as run:
            await command.callback(cog, native_interaction)

        response.defer.assert_awaited_once_with(ephemeral=True)
        native_interaction.followup.send.assert_awaited_once_with(
            'This command can only be used in a designated bot spam channel.',
            ephemeral=True,
        )
        run.assert_not_awaited()
