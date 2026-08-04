"""Focused offline coverage for the P7.13 native role leaderboard."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.role_leaderboard_workers')
service = import_offline_runtime('modules.role_leaderboard')
views = import_offline_runtime('modules.role_leaderboard_views')
games = import_offline_runtime('modules.games')
misc = import_offline_runtime('modules.misc')


GUILD_ID = 478571892832206869
CHANNEL_ID = 479292913080336397


class FakeRole:
    def __init__(
        self,
        role_id,
        name,
        *,
        guild=None,
        managed=False,
        default=False,
    ):
        self.id = role_id
        self.name = name
        self.guild = guild
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default


class FakeMember:
    def __init__(self, member_id, name, roles, guild=None):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.roles = list(roles)
        self.guild = guild


def make_guild():
    guild = SimpleNamespace(id=GUILD_ID)
    everyone = FakeRole(GUILD_ID, '@everyone', guild=guild, default=True)
    free = FakeRole(10, 'Free Agent', guild=guild)
    inactive = FakeRole(11, 'Inactive', guild=guild)
    alpha = FakeRole(12, 'Alpha', guild=guild)
    beta = FakeRole(13, 'Beta', guild=guild)
    managed = FakeRole(14, 'Managed Bot', guild=guild, managed=True)
    members = [
        FakeMember(101, 'Free Alpha', [everyone, free, alpha], guild),
        FakeMember(102, 'Free Beta', [everyone, free, beta], guild),
        FakeMember(103, 'Inactive Alpha', [everyone, alpha, inactive], guild),
    ]
    guild.roles = [everyone, free, inactive, alpha, beta, managed]
    guild.members = members
    return guild, {
        'everyone': everyone,
        'free': free,
        'inactive': inactive,
        'alpha': alpha,
        'beta': beta,
        'managed': managed,
    }


def role_snapshots():
    return (
        workers.RoleLeaderboardRoleSnapshot(GUILD_ID, '@everyone', is_default=True),
        workers.RoleLeaderboardRoleSnapshot(10, 'Free Agent'),
        workers.RoleLeaderboardRoleSnapshot(11, 'Inactive'),
        workers.RoleLeaderboardRoleSnapshot(12, 'Alpha'),
        workers.RoleLeaderboardRoleSnapshot(13, 'Beta'),
        workers.RoleLeaderboardRoleSnapshot(14, 'Managed Bot', managed=True),
    )


def row(
    discord_id,
    name,
    role_ids,
    *,
    global_elo,
    local_elo,
    global_wins,
    global_losses,
    local_wins,
    local_losses,
    total_games,
    recent_games,
):
    return workers.RoleLeaderboardRow(
        discord_id=discord_id,
        name=name,
        role_ids=tuple(role_ids),
        global_elo=global_elo,
        local_elo=local_elo,
        global_wins=global_wins,
        global_losses=global_losses,
        local_wins=local_wins,
        local_losses=local_losses,
        total_games=total_games,
        recent_games=recent_games,
    )


def result_for_views():
    rows = (
        row(
            30,
            'Alpha One',
            (12,),
            global_elo=1500,
            local_elo=1200,
            global_wins=8,
            global_losses=2,
            local_wins=3,
            local_losses=1,
            total_games=10,
            recent_games=2,
        ),
        row(
            20,
            'Alpha Beta',
            (12, 13),
            global_elo=1400,
            local_elo=1300,
            global_wins=5,
            global_losses=5,
            local_wins=6,
            local_losses=2,
            total_games=20,
            recent_games=7,
        ),
        row(
            10,
            'Beta One',
            (13,),
            global_elo=1600,
            local_elo=1100,
            global_wins=2,
            global_losses=1,
            local_wins=1,
            local_losses=4,
            total_games=4,
            recent_games=9,
        ),
        row(
            40,
            'Inactive Alpha',
            (12, 11),
            global_elo=1700,
            local_elo=1700,
            global_wins=20,
            global_losses=0,
            local_wins=20,
            local_losses=0,
            total_games=30,
            recent_games=10,
        ),
    )
    return workers.RoleLeaderboardResult(
        rows=rows,
        loaded_count=len(rows),
        candidate_count=len(rows),
        truncated=False,
        inactive_role_id=11,
    )


class FakeResponse:
    def __init__(self):
        self.done = False
        self.defer = mock.AsyncMock(side_effect=self._mark_done)
        self.send_message = mock.AsyncMock(side_effect=self._mark_done)
        self.send_modal = mock.AsyncMock(side_effect=self._mark_done)
        self.edit_message = mock.AsyncMock(side_effect=self._mark_done)

    async def _mark_done(self, *args, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


def interaction(user_id=777, *, guild_id=GUILD_ID):
    response = FakeResponse()
    guild = SimpleNamespace(id=guild_id)
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=guild,
        channel_id=CHANNEL_ID,
        channel=SimpleNamespace(send=mock.AsyncMock()),
        response=response,
        followup=SimpleNamespace(send=mock.AsyncMock()),
        delete_original_response=mock.AsyncMock(),
    )


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1
                return False

        return Context()


class FakeQuery:
    def __init__(self, rows, count=None):
        self.rows = tuple(rows)
        self.count_value = len(self.rows) if count is None else count
        self.limit_value = None

    def join(self, *args, **kwargs):
        return self

    def join_from(self, *args, **kwargs):
        return self

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def count(self):
        return self.count_value

    def __iter__(self):
        if self.limit_value is None:
            return iter(self.rows)
        return iter(self.rows[:self.limit_value])


class RoleBoundaryTests(unittest.TestCase):
    def test_capture_freezes_roles_members_and_inactive_role(self):
        guild, roles = make_guild()
        with mock.patch.object(
            service,
            '_setting',
            side_effect=lambda _guild_id, name, default=None: (
                'Inactive' if name == 'inactive_role' else default
            ),
        ):
            role_data, member_data, inactive_id = service.capture_guild_snapshot(
                guild,
            )
        self.assertIsInstance(role_data, tuple)
        self.assertIsInstance(member_data, tuple)
        self.assertEqual(inactive_id, 11)
        self.assertEqual(member_data[0].discord_id, 101)
        self.assertIn(10, member_data[0].role_ids)
        with self.assertRaises(FrozenInstanceError):
            member_data[0].discord_id = 1
        self.assertFalse(any(role is roles['free'] for role in role_data))

    def test_boundary_rejects_everyone_managed_cross_guild_and_more_than_five(self):
        snapshots = role_snapshots()
        with self.assertRaisesRegex(ValueError, 'Everyone'):
            service.validate_role_values(
                [FakeRole(GUILD_ID, '@everyone', guild=SimpleNamespace(id=GUILD_ID), default=True)],
                guild_id=GUILD_ID,
                role_snapshots=snapshots,
            )
        with self.assertRaisesRegex(ValueError, 'managed'):
            service.validate_role_values(
                [FakeRole(14, 'Managed Bot', guild=SimpleNamespace(id=GUILD_ID), managed=True)],
                guild_id=GUILD_ID,
                role_snapshots=snapshots,
            )
        with self.assertRaisesRegex(ValueError, 'current guild'):
            service.validate_role_values(
                [FakeRole(12, 'Alpha', guild=SimpleNamespace(id=999))],
                guild_id=GUILD_ID,
                role_snapshots=snapshots,
            )
        six = tuple(
            workers.RoleLeaderboardRoleSnapshot(index, f'Role {index}')
            for index in range(1, 7)
        )
        with self.assertRaisesRegex(ValueError, 'between 1 and 5'):
            service.validate_role_values(
                [SimpleNamespace(id=index) for index in range(1, 7)],
                guild_id=GUILD_ID,
                role_snapshots=six,
            )

    def test_free_agent_request_is_broad_but_arbitrary_roles_are_elevated(self):
        guild, _roles = make_guild()
        with mock.patch.object(
            service,
            '_setting',
            side_effect=lambda _guild_id, name, default=None: (
                'Inactive' if name == 'inactive_role' else default
            ),
        ), mock.patch.object(
            service.settings,
            'servers_included_in_global_lb',
            return_value=(GUILD_ID,),
        ):
            request = service.request_for_native(guild=guild)
        self.assertEqual(request.selected_role_ids, (10,))
        self.assertEqual(request.selected_role_names, ('Free Agent',))
        self.assertEqual(len(request.member_snapshots), 3)
        ordinary = SimpleNamespace(id=100, roles=())
        leader = SimpleNamespace(
            id=101,
            roles=[SimpleNamespace(name='House Leader')],
        )
        self.assertFalse(service.requester_can_select_roles(ordinary))
        self.assertTrue(service.requester_can_select_roles(leader))

    def test_server_and_channel_policy_matches_retained_role_lookup(self):
        ordinary = SimpleNamespace(id=100, roles=[])
        staff = SimpleNamespace(id=101, roles=[])
        with mock.patch.object(service, '_is_staff', side_effect=lambda member: member is staff), mock.patch.object(
            service,
            '_setting',
            side_effect=lambda _guild_id, name, default=None: {
                'bot_channels_strict': [CHANNEL_ID],
                'bot_channels_private': [],
            }.get(name, default),
        ), mock.patch.object(service.settings, 'server_ids', {'polychampions': GUILD_ID}):
            self.assertIsNone(service.native_access_error(ordinary, GUILD_ID, CHANNEL_ID))
            self.assertIn('not permitted', service.native_access_error(ordinary, 999, CHANNEL_ID))
            self.assertIsNone(service.native_access_error(staff, 999, CHANNEL_ID))
            self.assertIn('designated', service.native_access_error(staff, 999, 123))


class RoleSemanticsTests(unittest.TestCase):
    def test_all_any_inactive_and_explicit_inactive_semantics(self):
        result = result_for_views()
        all_alpha = workers.role_leaderboard_page(
            result,
            selected_role_ids=(12,),
            selected_role_names=('Alpha',),
            match_mode='all',
            sort_key='global_elo',
            scope='global',
        )
        self.assertEqual([item.discord_id for item in all_alpha.rows], [30, 20])
        any_beta = workers.role_leaderboard_page(
            result,
            selected_role_ids=(13,),
            selected_role_names=('Beta',),
            match_mode='any',
            sort_key='global_elo',
            scope='global',
        )
        self.assertEqual([item.discord_id for item in any_beta.rows], [10, 20])
        explicit_inactive = workers.role_leaderboard_page(
            result,
            selected_role_ids=(12, 11),
            selected_role_names=('Alpha', 'Inactive'),
            match_mode='all',
            sort_key='global_elo',
            scope='local',
        )
        self.assertEqual([item.discord_id for item in explicit_inactive.rows], [40])
        self.assertEqual(explicit_inactive.rows[0].local_wins, 20)
        self.assertEqual(explicit_inactive.rows[0].local_losses, 0)

    def test_all_four_sorts_are_descending_and_ties_use_stable_ids(self):
        result = result_for_views()
        expected = {
            'global_elo': [10, 30, 20],
            'local_elo': [20, 30, 10],
            'total_games': [20, 30, 10],
            'recent_games': [10, 20, 30],
        }
        for sort_key, ids in expected.items():
            with self.subTest(sort_key=sort_key):
                page = workers.role_leaderboard_page(
                    result,
                    selected_role_ids=(12, 13),
                    selected_role_names=('Alpha', 'Beta'),
                    match_mode='any',
                    sort_key=sort_key,
                    scope='global',
                )
                self.assertEqual(
                    [item.discord_id for item in page.rows],
                    ids,
                )

        tie_rows = tuple(
            row(
                discord_id,
                str(discord_id),
                (12,),
                global_elo=1000,
                local_elo=1000,
                global_wins=0,
                global_losses=0,
                local_wins=0,
                local_losses=0,
                total_games=0,
                recent_games=0,
            )
            for discord_id in (20, 10, 30)
        )
        tied = workers.role_leaderboard_page(
            workers.RoleLeaderboardResult(tie_rows, 3, 3, False, None),
            selected_role_ids=(12,),
            selected_role_names=('Alpha',),
            match_mode='all',
            sort_key='global_elo',
            scope='global',
        )
        self.assertEqual([item.discord_id for item in tied.rows], [10, 20, 30])
        self.assertEqual([item.rank for item in tied.rows], [1, 2, 3])

    def test_paging_is_deterministic_and_uses_selected_scope_only(self):
        rows = tuple(
            row(
                index,
                f'Player {index}',
                (12,),
                global_elo=2000 - index,
                local_elo=1000 + index,
                global_wins=index,
                global_losses=1,
                local_wins=2,
                local_losses=index,
                total_games=index,
                recent_games=index,
            )
            for index in range(1, 18)
        )
        result = workers.RoleLeaderboardResult(rows, 17, 17, False, None)
        page = workers.role_leaderboard_page(
            result,
            selected_role_ids=(12,),
            selected_role_names=('Alpha',),
            match_mode='all',
            sort_key='global_elo',
            scope='local',
            page_index=1,
        )
        self.assertEqual(page.page_count, 3)
        self.assertEqual(page.start_rank, 9)
        self.assertEqual(page.end_rank, 16)
        self.assertEqual(page.rows[0].local_elo, 1009)
        self.assertEqual(page.rows[0].global_wins, 9)


class RoleWorkerTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return workers.RoleLeaderboardRequest(
            guild_id=300,
            selected_role_ids=(12,),
            selected_role_names=('Alpha',),
            member_snapshots=(
                workers.RoleLeaderboardMemberSnapshot(100, 'Alpha', (12,)),
            ),
            role_snapshots=(workers.RoleLeaderboardRoleSnapshot(12, 'Alpha'),),
            global_guild_ids=(300,),
            recent_cutoff=datetime.datetime.now() - datetime.timedelta(days=14),
        )

    def test_worker_batches_metrics_and_closes_worker_connection(self):
        database = FakeDatabase()
        discord_member = SimpleNamespace(
            discord_id=100,
            name='Discord Alpha',
            elo_moonrise=1450,
        )
        player = SimpleNamespace(
            id=50,
            name='Player Alpha',
            elo_moonrise=1350,
            discord_member=discord_member,
        )
        query = FakeQuery((player,))
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.Player,
            'select',
            return_value=query,
        ) as select, mock.patch.object(
            workers,
            '_record_counts',
            side_effect=[{100: (9, 2)}, {50: (7, 3)}],
        ) as records, mock.patch.object(
            workers,
            '_game_counts',
            return_value={100: (20, 4)},
        ) as games_call:
            loaded = workers.load_role_leaderboard(self.request())

        self.assertEqual((database.opened, database.closed), (1, 1))
        select.assert_called_once()
        self.assertEqual(records.call_count, 2)
        games_call.assert_called_once()
        self.assertEqual(loaded.rows[0].name, 'Player Alpha')
        self.assertEqual(loaded.rows[0].global_wins, 9)
        self.assertEqual(loaded.rows[0].local_losses, 3)
        self.assertEqual(loaded.rows[0].recent_games, 4)
        with self.assertRaises(FrozenInstanceError):
            loaded.rows[0].name = 'changed'

    async def test_slow_read_keeps_event_loop_responsive_and_cancellation_drains(self):
        original = workers.load_role_leaderboard
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(request, **kwargs):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return workers.RoleLeaderboardResult((), 0, 0, False, None)

        workers.load_role_leaderboard = slow
        try:
            task = asyncio.create_task(workers.run_role_leaderboard(self.request()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            heartbeat = asyncio.create_task(asyncio.sleep(0.01))
            await asyncio.wait_for(heartbeat, timeout=0.1)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(finished.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.wait(timeout=1))
        finally:
            workers.load_role_leaderboard = original


class RoleWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, *, requester_id=777, can_select_roles=True):
        return views.RoleLeaderboardWorkspace(
            guild_id=GUILD_ID,
            requester_id=requester_id,
            result=result_for_views(),
            role_snapshots=role_snapshots(),
            selected_role_ids=(12,),
            selected_role_names=('Alpha',),
            can_select_roles=can_select_roles,
        )

    async def test_workspace_controls_are_requester_bound_and_expire_privately(self):
        view = self.make_view()
        denied = interaction(888)
        self.assertFalse(await view.interaction_check(denied))
        denied.response.send_message.assert_awaited_once_with(
            view.unauthorized_message,
            ephemeral=True,
        )
        view.stop()
        expired = interaction(777)
        await view._next_page(expired)
        expired.response.send_message.assert_awaited_once_with(
            view.expired_message,
            ephemeral=True,
        )

    async def test_loaded_state_changes_are_public_and_do_not_requery(self):
        view = self.make_view()
        with mock.patch.object(
            games.role_leaderboard_workers,
            'run_role_leaderboard',
            new=mock.AsyncMock(),
        ) as run:
            view.sort_select._values = ['local_elo']
            await view._select_sort(interaction(777))
            view.scope_select._values = ['local']
            await view._select_scope(interaction(777))
            view.match_select._values = ['any']
            await view._select_match(interaction(777))
            view.role_select._values = [
                FakeRole(13, 'Beta', guild=SimpleNamespace(id=GUILD_ID)),
            ]
            await view._select_roles(interaction(777))
            run.assert_not_awaited()
        self.assertEqual(view.selected_role_ids, (13,))
        self.assertEqual(view.match_mode, 'any')
        self.assertEqual(view.scope, 'local')

    async def test_role_select_is_hidden_for_ordinary_users_and_invalid_values_private(self):
        ordinary = self.make_view(can_select_roles=False)
        self.assertFalse(any(
            isinstance(item, discord.ui.RoleSelect)
            for item in ordinary.walk_children()
        ))
        elevated = self.make_view(can_select_roles=True)
        role_select = next(
            item for item in elevated.walk_children()
            if isinstance(item, discord.ui.RoleSelect)
        )
        self.assertEqual(role_select.max_values, 5)
        role_select._values = [
            FakeRole(GUILD_ID, '@everyone', guild=SimpleNamespace(id=GUILD_ID), default=True),
        ]
        invalid = interaction(777)
        await elevated._select_roles(invalid)
        invalid.response.send_message.assert_awaited_once()
        self.assertIn('Everyone', invalid.response.send_message.await_args.args[0])

    async def test_page_jump_and_public_success_render_dense_rows(self):
        view = self.make_view()
        self.assertIsInstance(view, discord.ui.LayoutView)
        text = '\n'.join(
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn('Role Leaderboard', text)
        self.assertIn('1500 ELO', text)
        modal = views.RoleLeaderboardPageJumpModal(view)
        modal.page_number._value = '1'
        submit = interaction(777)
        await modal.on_submit(submit)
        submit.response.edit_message.assert_awaited_once_with(view=view)


class RegistrationAndCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_registration_shape_and_prefix_retirement(self):
        leaderboard = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'leaderboard'
        )
        roles = leaderboard.get_command('roles')
        self.assertEqual(roles.parameters, [])
        prefix = misc.misc.__cog_commands__
        freeagents = next(command for command in prefix if command.name == 'freeagents')
        self.assertEqual(freeagents.aliases, [])
        self.assertNotIn('roleelo', {command.name for command in prefix})
        self.assertNotIn('roleeloany', {command.name for command in prefix})
        self.assertFalse(any(
            alias in {'roleelo', 'roleeloany'}
            for command in prefix
            for alias in command.aliases
        ))

    async def test_native_command_defers_privately_and_publishes_success_publicly(self):
        guild, _roles = make_guild()
        request = workers.RoleLeaderboardRequest(
            guild_id=GUILD_ID,
            selected_role_ids=(10,),
            selected_role_names=('Free Agent',),
            member_snapshots=(),
            role_snapshots=role_snapshots(),
            global_guild_ids=(GUILD_ID,),
        )
        result = workers.RoleLeaderboardResult((), 0, 0, False, 11)
        command = next(
            command
            for command in next(
                command
                for command in games.polygames.__cog_app_commands__
                if command.name == 'leaderboard'
            ).commands
            if command.name == 'roles'
        )
        native = interaction(777)
        native.guild = guild
        cog = object.__new__(games.polygames)
        with mock.patch.object(
            games.role_leaderboard_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            games.role_leaderboard_service,
            'request_for_native',
            return_value=request,
        ), mock.patch.object(
            games.role_leaderboard_service,
            'requester_can_select_roles',
            return_value=False,
        ), mock.patch.object(
            games.role_leaderboard_workers,
            'run_role_leaderboard',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            games.squad_show_service,
            'publish_native',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, native)

        native.response.defer.assert_awaited_once_with(ephemeral=True)
        publish.assert_awaited_once()
        self.assertIsInstance(publish.await_args.args[1], views.RoleLeaderboardWorkspace)
        native.followup.send.assert_not_awaited()

    async def test_native_access_failure_is_private_and_does_not_load(self):
        command = next(
            command
            for command in next(
                command
                for command in games.polygames.__cog_app_commands__
                if command.name == 'leaderboard'
            ).commands
            if command.name == 'roles'
        )
        native = interaction(777)
        native.guild = SimpleNamespace(id=GUILD_ID)
        cog = object.__new__(games.polygames)
        with mock.patch.object(
            games.role_leaderboard_service,
            'native_access_error',
            return_value='This command can only be used in a designated bot spam channel.',
        ), mock.patch.object(
            games.role_leaderboard_workers,
            'run_role_leaderboard',
            new=mock.AsyncMock(),
        ) as run:
            await command.callback(cog, native)
        native.response.defer.assert_awaited_once_with(ephemeral=True)
        native.followup.send.assert_awaited_once_with(
            'This command can only be used in a designated bot spam channel.',
            ephemeral=True,
        )
        run.assert_not_awaited()

    async def test_freeagents_prefix_uses_shared_read_service(self):
        command = next(
            command for command in misc.misc.__cog_commands__
            if command.name == 'freeagents'
        )
        request = SimpleNamespace()
        result = SimpleNamespace()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=GUILD_ID),
            author=SimpleNamespace(id=777),
            typing=lambda: _Typing(),
            send=mock.AsyncMock(),
        )
        cog = object.__new__(misc.misc)
        with mock.patch.object(
            misc.role_leaderboard_service,
            'request_for_prefix',
            return_value=request,
        ), mock.patch.object(
            misc.role_leaderboard_workers,
            'run_role_leaderboard',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            misc.role_leaderboard_service,
            'publish_prefix',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, ctx, arg=None)
        publish.assert_awaited_once_with(ctx, result, request)

    def test_freeagent_prefix_presentation_keeps_all_loaded_matches(self):
        rows = tuple(
            row(
                index,
                f'Player {index}',
                (10,),
                global_elo=1000 + index,
                local_elo=900 + index,
                global_wins=0,
                global_losses=0,
                local_wins=0,
                local_losses=0,
                total_games=index,
                recent_games=index,
            )
            for index in range(1, 10)
        )
        result = workers.RoleLeaderboardResult(rows, 9, 9, False, None)
        request = workers.RoleLeaderboardRequest(
            guild_id=GUILD_ID,
            selected_role_ids=(10,),
            selected_role_names=('Free Agent',),
            member_snapshots=(),
            role_snapshots=role_snapshots(),
        )
        self.assertEqual(
            [item.discord_id for item in service.prefix_rows(result, request)],
            list(range(1, 10)),
        )


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False
