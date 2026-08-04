"""Focused offline coverage for the P8.6 asynchronous team card."""

import asyncio
from contextlib import AbstractAsyncContextManager, AbstractContextManager, ExitStack
from dataclasses import FrozenInstanceError, replace
import inspect
from types import SimpleNamespace
import time
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.team_show_workers')
service = import_offline_runtime('modules.team_show')
games = import_offline_runtime('modules.games')
administration = import_offline_runtime('modules.administration')


class FakeDatabase:
    def __init__(self):
        self.events = []
        self.connection_opened = 0
        self.connection_closed = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                database.events.append('connection-open')
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1
                database.events.append('connection-close')
                return False

        return ConnectionContext()


class FakeQuery:
    def __init__(self, records):
        self.records = tuple(records)

    def join(self, *args, **kwargs):
        return self

    def where(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def __iter__(self):
        return iter(self.records)


class FakeHouse:
    def __init__(self, name='Ninjas', emoji='🥷'):
        self.name = name
        self.emoji = emoji


class FakeTeam:
    def __init__(self, *, name='Ronin', team_id=42):
        self.id = team_id
        self.name = name
        self.guild_id = 300
        self.emoji = '⚔️'
        self.image_url = 'https://example.test/ronin.png'
        self.elo = 1234
        self.league_tier = 2
        self.external_server = 987654
        self.house = FakeHouse()

    def get_record(self, *, alltime):
        self.record_alltime_argument = alltime
        return (12, 8)


class FakeDiscordMember:
    def __init__(self, discord_id, name):
        self.discord_id = discord_id
        self.name = name


class FakePlayer:
    def __init__(self, player_id, discord_id, name, elo):
        self.id = player_id
        self.discord_member = FakeDiscordMember(discord_id, name)
        self.elo_moonrise = elo


def member(discord_id, name):
    return SimpleNamespace(
        id=discord_id,
        name=name,
        display_name=f'{name} Display',
        mention=f'<@{discord_id}>',
    )


def role(role_id, name, members=()):
    return SimpleNamespace(id=role_id, name=name, members=list(members))


def guild(*, missing_team_role=False):
    alpha = member(101, 'Alpha')
    beta = member(202, 'Beta')
    inactive = member(303, 'Inactive')
    roles = [
        role(2, 'Inactive', [inactive]),
        role(3, 'Ninjas', [alpha, beta]),
        role(4, 'House Leader', [alpha]),
        role(5, 'House Co-Leader', [beta]),
        role(6, 'House Recruiter', [beta]),
        role(7, 'Team Captain', [beta]),
    ]
    if not missing_team_role:
        roles.insert(0, role(1, 'Ronin', [alpha, beta, inactive]))
    return SimpleNamespace(
        id=300,
        roles=roles,
        members=[alpha, beta, inactive],
    )


def request(
    *,
    activity_mode=workers.TEAM_ACTIVITY_RECENT,
    missing=False,
    team_lookup='Ronin',
):
    return workers.TeamShowRequest(
        guild_id=300,
        requester_id=101,
        team_lookup=team_lookup,
        activity_mode=activity_mode,
        team_enabled=True,
        channel_allowed=True,
        leadership_enabled=True,
        inactive_role_name='Inactive',
        guild_snapshot=service.capture_guild_snapshot(
            guild(missing_team_role=missing)
        ),
        team_elo_reset_label='01/01/2020',
        requester_description='**Alpha** (`101`)',
        native=True,
        invoked_with='/team show',
        prefix='$',
    )


def result(*, activity_mode=workers.TEAM_ACTIVITY_RECENT, missing=False):
    return workers.TeamShowResult(
        guild_id=300,
        requester_id=101,
        team_id=42,
        team_name='Ronin',
        team_emoji='⚔️',
        house_name='Ninjas',
        house_emoji='🥷',
        league_tier=2,
        tier_name='Gold',
        external_server=987654,
        elo=1234,
        wins=12,
        losses=8,
        roster_rows=(
            workers.TeamShowRosterRow(
                discord_id=202,
                name='Beta',
                elo=1200,
                rank=3,
                recent_games=5,
                completed_games=20,
                registered=True,
            ),
            workers.TeamShowRosterRow(
                discord_id=101,
                name='Alpha',
                elo=1110,
                rank=8,
                recent_games=2,
                completed_games=11,
                registered=True,
            ),
        ),
        team_role_found=not missing,
        missing_role_name='Ronin' if missing else None,
        leaders=('<@101>',),
        coleaders=('<@202>',),
        recruiters=('<@202>',),
        captains=('<@202>',),
        recent_games=(('Game 1', '2026-08-01 - 2v2 - WINNER: Ronin'),),
        graph_bytes=b'graph-bytes',
        local_image_bytes=b'logo-bytes',
        image_url='https://example.test/ronin.png',
        activity_mode=activity_mode,
        team_elo_reset_label='01/01/2020',
    )


class TeamShowWorkerTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.team = FakeTeam()
        self.alpha = FakePlayer(501, 101, 'Alpha DB', 1110)
        self.beta = FakePlayer(502, 202, 'Beta DB', 1200)
        self.patches = ExitStack()
        self.patches.enter_context(
            mock.patch.object(workers.models, 'db', self.database)
        )
        self.patches.enter_context(
            mock.patch.object(
                workers.models.Team,
                'get_by_name',
                return_value=(self.team,),
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                workers.models.Game,
                'search',
                return_value=['game-row'],
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                workers.utilities,
                'summarize_game_list',
                return_value=(('Game 1', 'summary'),),
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                workers.image_storage,
                'local_image_bytes',
                return_value=b'local-logo',
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                workers,
                '_load_player_rows',
                return_value=(self.alpha, self.beta),
            )
        )
        self.metric_calls = []

        def metric_counts(player_ids, *, completed):
            self.metric_calls.append((tuple(player_ids), completed))
            return (
                {501: 2, 502: 5}
                if not completed
                else {501: 11, 502: 20}
            )

        self.patches.enter_context(
            mock.patch.object(
                workers,
                '_roster_metric_counts',
                side_effect=metric_counts,
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                workers,
                '_rank_by_player',
                return_value={501: 8, 502: 3},
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                workers,
                '_history_rows',
                side_effect=(
                    lambda team_id, elo_field: (
                        (('2026-01-01', 1100),)
                        if 'alltime' in getattr(elo_field, 'name', '')
                        else (('2026-01-01', 1110),)
                    )
                ),
            )
        )
        self.render_events = []

        def render(data):
            self.render_events.append('render')
            self.database.events.append('render')
            return b'graph'

        self.patches.enter_context(
            mock.patch.object(workers, '_render_graph', side_effect=render)
        )
        self.patches.enter_context(
            mock.patch.object(
                workers.settings,
                'tier_lookup',
                return_value=(2, 'Gold'),
            )
        )

    def tearDown(self):
        self.patches.close()

    def test_worker_uses_primitive_snapshot_and_closes_connection_before_render(self):
        loaded = workers.load_team_show(request())

        self.assertEqual(self.database.events, [
            'connection-open',
            'connection-close',
            'render',
        ])
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)
        self.assertEqual(loaded.graph_bytes, b'graph')
        self.assertEqual(loaded.local_image_bytes, b'local-logo')
        self.assertEqual(loaded.house_name, 'Ninjas')
        self.assertEqual(loaded.tier_name, 'Gold')
        self.assertEqual(loaded.external_server, 987654)
        self.assertEqual(
            [row.name for row in loaded.roster_rows],
            ['Alpha DB', 'Beta DB'],
        )
        self.assertEqual(loaded.leaders, ('<@101>',))
        self.assertEqual(loaded.coleaders, ('<@202>',))
        self.assertEqual(loaded.recruiters, ('<@202>',))
        self.assertEqual(loaded.captains, ('<@202>',))

    def test_roster_queries_are_batched_and_inactive_member_is_excluded(self):
        loaded = workers.load_team_show(request())

        self.assertEqual(
            self.metric_calls,
            [((501, 502), False), ((501, 502), True)],
        )
        self.assertEqual(
            [row.discord_id for row in loaded.roster_rows],
            [101, 202],
        )
        self.assertNotIn(303, [row.discord_id for row in loaded.roster_rows])

    def test_worker_preserves_role_order_for_presentation_sorting(self):
        loaded = workers.load_team_show(
            request(activity_mode=workers.TEAM_ACTIVITY_COMPLETED)
        )
        self.assertEqual(
            [row.discord_id for row in loaded.roster_rows],
            [101, 202],
        )
        self.assertEqual(loaded.activity_mode, workers.TEAM_ACTIVITY_COMPLETED)

    def test_missing_exact_team_role_returns_warning_data_without_member_queries(self):
        with mock.patch.object(workers, '_load_player_rows') as load_players:
            loaded = workers.load_team_show(request(missing=True))
        load_players.assert_called_once_with(300, ())
        self.assertFalse(loaded.team_role_found)
        self.assertEqual(loaded.missing_role_name, 'Ronin')
        self.assertEqual(loaded.roster_rows, ())

    def test_worker_request_and_result_are_frozen(self):
        request_value = request()
        with self.assertRaises(FrozenInstanceError):
            request_value.guild_id = 999
        loaded = workers.load_team_show(request_value)
        with self.assertRaises(FrozenInstanceError):
            loaded.team_name = 'changed'
        with self.assertRaises(FrozenInstanceError):
            loaded.roster_rows[0].name = 'changed'

    def test_explicit_and_inferred_team_resolution_are_guild_scoped(self):
        team = self.team
        with mock.patch.object(
                workers.models.Team,
                'select',
                return_value=FakeQuery((team,))):
            inferred = workers._resolve_team(
                request(team_lookup=None)
            )
        self.assertIs(inferred, team)

        with mock.patch.object(
                workers.models.Team,
                'select',
                return_value=FakeQuery((team, team))):
            with self.assertRaisesRegex(workers.TeamShowLookupError, 'ambiguous'):
                workers._resolve_team(request(team_lookup=None))

    async def _run_slow_worker(self):
        original = workers.load_team_show

        def slow(_request):
            time.sleep(0.08)
            return result()

        workers.load_team_show = slow
        try:
            heartbeat = asyncio.create_task(asyncio.sleep(0.01))
            task = asyncio.create_task(workers.run_team_show(request()))
            await asyncio.wait_for(heartbeat, timeout=0.04)
            self.assertFalse(task.done())
            self.assertEqual((await task).graph_bytes, b'graph-bytes')
        finally:
            workers.load_team_show = original

    def test_slow_read_does_not_block_event_loop(self):
        asyncio.run(self._run_slow_worker())

    async def _run_cancelled_worker(self):
        original = workers.load_team_show
        events = []

        def slow(_request):
            events.append('start')
            time.sleep(0.05)
            events.append('done')
            return result()

        workers.load_team_show = slow
        try:
            task = asyncio.create_task(workers.run_team_show(request()))
            await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(events, ['start', 'done'])
        finally:
            workers.load_team_show = original

    def test_cancellation_drains_thread_worker(self):
        asyncio.run(self._run_cancelled_worker())


class TeamShowPresentationTests(unittest.TestCase):
    @staticmethod
    def _cross_metric_result():
        card = result()
        alpha = replace(
            card.roster_rows[1],
            recent_games=9,
            completed_games=1,
        )
        beta = replace(
            card.roster_rows[0],
            recent_games=2,
            completed_games=10,
        )
        return replace(card, roster_rows=(alpha, beta))

    @staticmethod
    def _assert_order(description, first, second):
        self_index = description.index(first)
        other_index = description.index(second)
        if self_index >= other_index:
            raise AssertionError(
                f'expected {first!r} before {second!r}: {description!r}'
            )

    def test_dense_card_has_legacy_fields_and_both_roster_modes(self):
        card = result()
        recent = service.render_embed(card)
        completed = service.render_embed(card, completed=True)
        self.assertEqual(recent.title, 'Team card for **Ronin** ⚔️\nHouse Ninjas 🥷')
        self.assertEqual(recent.fields[0].name, 'Results')
        self.assertIn('Recent Games', recent.description)
        self.assertIn('Completed Games', completed.description)
        self.assertIn('**House Leader**', {field.name for field in recent.fields})
        self.assertIn('**Recent games**', {field.name for field in recent.fields})
        self.assertEqual(
            recent.image.url,
            'attachment://team-elo-42.png',
        )
        self.assertEqual(
            recent.thumbnail.url,
            'attachment://team-logo-42.png',
        )

    def test_each_roster_view_sorts_its_metric_and_keeps_stable_ties(self):
        card = self._cross_metric_result()
        recent = service.render_embed(card)
        completed = service.render_embed(
            replace(
                card,
                activity_mode=workers.TEAM_ACTIVITY_COMPLETED,
            )
        )
        self._assert_order(recent.description, 'Alpha', 'Beta')
        self._assert_order(completed.description, 'Beta', 'Alpha')

        tied = replace(
            card,
            roster_rows=tuple(
                replace(row, recent_games=5, completed_games=5)
                for row in card.roster_rows
            ),
        )
        self._assert_order(
            service.render_embed(tied).description,
            'Alpha',
            'Beta',
        )

        class Response:
            def __init__(self):
                self.edit_message = mock.AsyncMock()

            def is_done(self):
                return False

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=101),
            response=Response(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        view = service.TeamShowView(card, requester_id=101)
        asyncio.run(view._activity_clicked(interaction))
        refreshed_completed = interaction.response.edit_message.await_args.kwargs[
            'embed'
        ]
        self._assert_order(refreshed_completed.description, 'Beta', 'Alpha')

        interaction.response.edit_message.reset_mock()
        asyncio.run(view._activity_clicked(interaction))
        refreshed_recent = interaction.response.edit_message.await_args.kwargs[
            'embed'
        ]
        self._assert_order(refreshed_recent.description, 'Alpha', 'Beta')

        interaction.response.edit_message.reset_mock()
        completed_view = service.TeamShowView(
            replace(
                card,
                activity_mode=workers.TEAM_ACTIVITY_COMPLETED,
            ),
            requester_id=101,
        )
        self.assertTrue(completed_view.completed)
        asyncio.run(completed_view._activity_clicked(interaction))
        refreshed_from_completed = interaction.response.edit_message.await_args.kwargs[
            'embed'
        ]
        self._assert_order(refreshed_from_completed.description, 'Alpha', 'Beta')

    def test_graph_renderer_uses_object_owned_agg_without_pyplot(self):
        source = inspect.getsource(workers._render_graph)
        self.assertIn('FigureCanvasAgg', source)
        self.assertIn('Figure', source)
        self.assertNotIn('pyplot', source)
        self.assertNotIn('plt', source)
        self.assertNotIn('style.use', source)
        self.assertNotIn('plt', workers.__dict__)

        data = SimpleNamespace(
            team_name='Ronin',
            team_elo_reset_label='01/01/2020',
            current_history=(
                ('2026-01-01', 1110),
                ('2026-02-01', 1120),
            ),
            alltime_history=(
                ('2026-01-01', 1100),
                ('2026-02-01', 1120),
            ),
        )
        graph = workers._render_graph(data)
        self.assertGreater(len(graph), 0)
        self.assertTrue(graph.startswith(b'\x89PNG\r\n\x1a\n'))

    def test_missing_role_warning_and_image_files_are_preserved_without_graph_png(self):
        card = result(missing=True)
        self.assertIn('No matching discord role', service.render_content(card))
        files = service.render_files(card)
        self.assertEqual(
            [file.filename for file in files],
            ['team-elo-42.png', 'team-logo-42.png'],
        )
        self.assertNotIn('graph.png', [file.filename for file in files])
        self.assertEqual(files[0].fp.read(), b'graph-bytes')
        self.assertEqual(files[1].fp.read(), b'logo-bytes')

    def test_view_has_one_requester_bound_control_and_refreshes_publicly(self):
        view = service.TeamShowView(result(), requester_id=101)
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.activity_button.label, 'Show all completed games')

        class Response:
            def __init__(self):
                self.send_message = mock.AsyncMock()
                self.edit_message = mock.AsyncMock()

            def is_done(self):
                return False

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=101),
            response=Response(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        asyncio.run(view._activity_clicked(interaction))
        interaction.response.edit_message.assert_awaited_once()
        self.assertNotIn(
            'ephemeral', interaction.response.edit_message.await_args.kwargs
        )
        self.assertTrue(view.completed)
        self.assertEqual(view.activity_button.label, 'Show recent 30 days')

    def test_view_rejects_other_requesters_and_expired_controls_privately(self):
        view = service.TeamShowView(result(), requester_id=101)

        class Response:
            def __init__(self):
                self.send_message = mock.AsyncMock()
                self.edit_message = mock.AsyncMock()

            def is_done(self):
                return False

        unauthorized = SimpleNamespace(
            user=SimpleNamespace(id=202),
            response=Response(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        asyncio.run(view._activity_clicked(unauthorized))
        unauthorized.response.send_message.assert_awaited_once()
        self.assertTrue(
            unauthorized.response.send_message.await_args.kwargs['ephemeral']
        )

        view.expires_at = 0
        expired = SimpleNamespace(
            user=SimpleNamespace(id=101),
            response=Response(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        asyncio.run(view._activity_clicked(expired))
        expired.response.send_message.assert_awaited_once()
        self.assertTrue(
            expired.response.send_message.await_args.kwargs['ephemeral']
        )

    def test_snapshot_contains_only_primitives_and_is_immutable(self):
        original_guild = guild()
        snapshot = service.capture_guild_snapshot(original_guild)
        self.assertIsInstance(snapshot, workers.TeamShowGuildSnapshot)
        with self.assertRaises(FrozenInstanceError):
            snapshot.guild_id = 999
        original_guild.roles[0].members[0].name = 'Changed live object'
        self.assertEqual(snapshot.members[0].name, 'Alpha')
        self.assertTrue(
            all(
                isinstance(role.member_ids, tuple)
                and all(isinstance(member_id, int) for member_id in role.member_ids)
                for role in snapshot.roles
            )
        )

    def test_native_publish_is_public_after_private_defer(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=101),
            channel=SimpleNamespace(send=mock.AsyncMock(return_value='message')),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )
        asyncio.run(service.publish_native(interaction, result()))
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertNotIn(
            'ephemeral', interaction.channel.send.await_args.kwargs
        )


class TeamShowCommandTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self):
        return SimpleNamespace(
            guild=SimpleNamespace(id=300, roles=(), members=()),
            channel_id=555,
            channel=SimpleNamespace(send=mock.AsyncMock(return_value='message')),
            user=SimpleNamespace(
                id=101,
                name='Alpha',
                display_name='Alpha',
                mention='<@101>',
            ),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )

    async def test_slash_show_shape_infers_and_publishes_public_card(self):
        group = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        )
        command = group.get_command('show')
        interaction = self._interaction()
        loaded = result()
        request_mock = mock.Mock(return_value=request())
        with (
            mock.patch.object(
                administration.team_show_service,
                'native_access_error',
                return_value=None,
            ),
            mock.patch.object(
                administration.team_show_service,
                'build_request',
                request_mock,
            ),
            mock.patch.object(
                administration.team_show_service,
                'run',
                new=mock.AsyncMock(return_value=loaded),
            ),
            mock.patch.object(
                administration.settings,
                'guild_setting',
                return_value='$',
            ),
        ):
            await command.callback(
                administration.administration.__new__(administration.administration),
                interaction,
                None,
            )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.channel.send.assert_awaited_once()
        self.assertIsNone(request_mock.call_args.kwargs['team_lookup'])
        self.assertEqual(
            request_mock.call_args.kwargs['activity_mode'],
            workers.TEAM_ACTIVITY_RECENT,
        )

    async def test_slash_lookup_and_database_failures_are_private(self):
        group = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        )
        command = group.get_command('show')
        interaction = self._interaction()
        with (
            mock.patch.object(
                administration.team_show_service,
                'native_access_error',
                return_value=None,
            ),
            mock.patch.object(
                administration.team_show_service,
                'build_request',
                return_value=request(),
            ),
            mock.patch.object(
                administration.team_show_service,
                'run',
                new=mock.AsyncMock(
                    side_effect=workers.TeamShowLookupError('ambiguous')
                ),
            ),
            mock.patch.object(
                administration.settings,
                'guild_setting',
                return_value='$',
            ),
        ):
            await command.callback(
                administration.administration.__new__(administration.administration),
                interaction,
                None,
            )
        interaction.followup.send.assert_awaited_once_with(
            'ambiguous',
            ephemeral=True,
        )
        interaction.channel.send.assert_not_awaited()

    async def test_prefix_completed_deep_links_shared_service(self):
        command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'team'
        )

        class Typing(AbstractAsyncContextManager):
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        ctx = SimpleNamespace(
            author=SimpleNamespace(id=101, name='Alpha', display_name='Alpha', mention='<@101>'),
            guild=SimpleNamespace(id=300, roles=(), members=()),
            channel=SimpleNamespace(id=555),
            prefix='$',
            invoked_with='team',
            typing=lambda: Typing(),
            send=mock.AsyncMock(),
        )
        built = mock.Mock(return_value=request(
            activity_mode=workers.TEAM_ACTIVITY_COMPLETED,
        ))
        publish = mock.AsyncMock()
        with (
            mock.patch.object(games.team_show_service, 'build_request', built),
            mock.patch.object(games.team_show_service, 'run', new=mock.AsyncMock(return_value=result(
                activity_mode=workers.TEAM_ACTIVITY_COMPLETED,
            ))),
            mock.patch.object(games.team_show_service, 'publish_prefix', publish),
        ):
            await command.callback(
                games.polygames.__new__(games.polygames),
                ctx,
                team_string='Ronin completed',
            )
        self.assertEqual(
            built.call_args.kwargs['activity_mode'],
            workers.TEAM_ACTIVITY_COMPLETED,
        )
        self.assertEqual(built.call_args.kwargs['team_lookup'], 'Ronin')
        publish.assert_awaited_once()


class TeamShowIntegrationGateShapeTests(unittest.TestCase):
    def test_real_schema_case_is_present_but_not_run_by_offline_discovery(self):
        from tests import test_database_integration

        method = getattr(
            test_database_integration.DevelopmentDatabaseIntegrationTests,
            'test_team_show_worker_reads_real_schema_without_writes',
            None,
        )
        self.assertIsNotNone(method)
        self.assertTrue(
            test_database_integration.DevelopmentDatabaseIntegrationTests.__unittest_skip__
        )


if __name__ == '__main__':
    unittest.main()
