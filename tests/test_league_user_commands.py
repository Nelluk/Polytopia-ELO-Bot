"""Focused coverage for P8.11 small native league user commands."""

from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import asyncio
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_user_workers')
service = import_offline_runtime('modules.league_user_commands')
league = import_offline_runtime('modules.league')


class FakeDatabase:
    def __init__(self):
        self.connections = 0
        self.atomics = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.connections += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()

    def atomic(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.atomics += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()


class FakeQuery:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def where(self, *args):
        return self

    def order_by(self, *args):
        return self

    def limit(self, count):
        return FakeQuery(self.rows[:count])

    def __iter__(self):
        return iter(self.rows)


def role(name):
    return SimpleNamespace(name=name)


def member(member_id=10, *, roles=(), name='Actor'):
    return SimpleNamespace(
        id=member_id,
        name=name,
        display_name=name,
        nick=None,
        mention=f'<@{member_id}>',
        roles=list(roles),
        add_roles=mock.AsyncMock(),
        remove_roles=mock.AsyncMock(),
    )


def join_result(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        registered=True,
        local_player_created=False,
        team_roles=(workers.LeagueTeamRole(1, 'The Jets', '✈️'),),
        team_roles_truncated=False,
    )
    values.update(overrides)
    return workers.LeagueJoinResult(**values)


class RegistrationAndServiceTests(unittest.TestCase):
    def test_native_shapes_and_prefix_conveniences_are_retained(self):
        root = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )
        self.assertEqual(
            {command.name for command in root.commands},
            {'tokens', 'guide', 'mark-active', 'join-novas', 'season'},
        )
        self.assertEqual(root.get_command('guide').parameters, [])
        self.assertEqual(root.get_command('join-novas').parameters, [])
        mark_active = root.get_command('mark-active')
        self.assertEqual(
            [
                (parameter.name, parameter.required, parameter.type)
                for parameter in mark_active.parameters
            ],
            [('member', False, discord.AppCommandOptionType.user)],
        )
        prefix = {command.name: command for command in league.league.__cog_commands__}
        self.assertIn('tutorial', prefix)
        self.assertIn('imalive', prefix)
        self.assertIn('novas', prefix)
        self.assertIn('joinnovas', prefix['novas'].aliases)

    def test_guide_uses_surviving_native_workflows(self):
        output = service.guide_message()
        for path in (
            '/player register',
            '/league join-novas',
            '/game search',
            '/game open',
            '/game start',
            '/game show',
        ):
            self.assertIn(path, output)
        self.assertNotIn('$setname', output)
        self.assertNotIn('$novagames', output)

    def test_mark_active_target_permission_matches_legacy_roles(self):
        actor = member(10)
        target = member(20)
        self.assertTrue(service.can_target_mark_active(actor, actor))
        self.assertFalse(service.can_target_mark_active(actor, target))
        for role_name in service.LEADER_ROLE_NAMES:
            privileged = member(10, roles=(role(role_name),))
            self.assertTrue(
                service.can_target_mark_active(privileged, target), role_name
            )

    def test_requests_and_results_are_frozen_primitives(self):
        request = workers.LeagueJoinRequest(300, 10, 'Actor', 'Nick', True)
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 1
        result = join_result()
        self.assertIsInstance(result.team_roles, tuple)
        self.assertIsInstance(result.team_roles[0].team_id, int)


class WorkerTests(unittest.TestCase):
    def test_worker_owns_connection_and_atomic_registration_lookup(self):
        database = FakeDatabase()
        teams = (
            SimpleNamespace(id=1, name='The Jets', emoji='✈️'),
            SimpleNamespace(id=2, name='The Ronin', emoji=None),
        )
        request = workers.LeagueJoinRequest(300, 10, 'Actor', 'Nick', True)
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.Player,
            'get_by_discord_id',
            return_value=(SimpleNamespace(id=7), True),
        ) as lookup, mock.patch.object(
            workers.models.Team, 'select', return_value=FakeQuery(teams)
        ):
            result = workers.load_join_eligibility(request)
        self.assertTrue(result.registered)
        self.assertTrue(result.local_player_created)
        self.assertEqual([team.name for team in result.team_roles], ['The Jets', 'The Ronin'])
        self.assertEqual(database.connections, 1)
        self.assertEqual(database.atomics, 1)
        self.assertEqual(lookup.call_args.kwargs['discord_id'], 10)

    def test_wrong_scope_fails_before_database_connection(self):
        database = FakeDatabase()
        request = workers.LeagueJoinRequest(300, 10, 'Actor', '', False)
        with mock.patch.object(workers.models, 'db', database):
            with self.assertRaises(workers.LeagueUserPermissionError):
                workers.load_join_eligibility(request)
        self.assertEqual(database.connections, 0)

    def test_slow_lookup_keeps_event_loop_responsive(self):
        async def run_case():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return join_result()

            with mock.patch.object(
                workers, 'load_join_eligibility', side_effect=slow
            ):
                task = asyncio.create_task(
                    workers.run_join_eligibility(
                        workers.LeagueJoinRequest(300, 10, 'Actor', '', True)
                    )
                )
                deadline = asyncio.get_running_loop().time() + 1
                while not started.is_set():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail('the league worker did not start')
                    await asyncio.sleep(0.001)
                heartbeat = 0
                for _ in range(3):
                    await asyncio.sleep(0.01)
                    heartbeat += 1
                release.set()
                await asyncio.sleep(0.05)
                result = await task
            return result, heartbeat

        result, heartbeat = asyncio.run(run_case())
        self.assertTrue(result.registered)
        self.assertEqual(heartbeat, 3)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _root():
        return next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )

    def _interaction(self, *, actor=None, guild_roles=()):
        actor = actor or member()
        guild = SimpleNamespace(id=300, roles=list(guild_roles))
        return SimpleNamespace(
            guild=guild,
            user=actor,
            channel=SimpleNamespace(send=mock.AsyncMock()),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )

    async def test_guide_is_public_and_requires_league_scope(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction()
        command = self._root().get_command('guide')
        with mock.patch.object(service, 'league_scope', return_value=True):
            await command.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once_with(
            service.guide_message()
        )

    async def test_mark_active_commits_role_before_public_attributed_success(self):
        cog = league.league.__new__(league.league)
        inactive = role('Inactive')
        actor = member(10, roles=(inactive,))
        interaction = self._interaction(actor=actor, guild_roles=(inactive,))
        events = []

        async def remove(*args, **kwargs):
            events.append('role')

        async def publish(*args, **kwargs):
            events.append('public')

        actor.remove_roles = mock.AsyncMock(side_effect=remove)
        interaction.channel.send = mock.AsyncMock(side_effect=publish)
        command = self._root().get_command('mark-active')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'inactive_role', return_value=inactive
        ):
            await command.callback(cog, interaction, None)
        self.assertEqual(events, ['role', 'public'])
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        public_message = interaction.channel.send.await_args.args[0]
        self.assertIn('<@10>', public_message)
        self.assertIn('marked', public_message)

    async def test_mark_active_other_member_denial_is_private(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction(actor=member(10))
        target = member(20, roles=(role('Inactive'),))
        command = self._root().get_command('mark-active')
        with mock.patch.object(service, 'league_scope', return_value=True):
            await command.callback(cog, interaction, target)
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs['ephemeral']
        )
        target.remove_roles.assert_not_awaited()

    async def test_mark_active_publication_failure_is_terminal_reconciliation(self):
        cog = league.league.__new__(league.league)
        inactive = role('Inactive')
        actor = member(10, roles=(inactive,))
        interaction = self._interaction(actor=actor, guild_roles=(inactive,))
        interaction.channel.send.side_effect = RuntimeError('publish failed')
        command = self._root().get_command('mark-active')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'inactive_role', return_value=inactive
        ):
            await command.callback(cog, interaction, None)
        actor.remove_roles.assert_awaited_once()
        message = interaction.followup.send.await_args.args[0]
        self.assertIn('was removed', message)
        self.assertIn('Do not retry', message)

    async def test_join_novas_checks_worker_then_changes_roles_then_publishes(self):
        cog = league.league.__new__(league.league)
        novas = role(service.NOVAS_ROLE_NAME)
        newbie = role(service.NEWBIE_ROLE_NAME)
        actor = member(10, roles=(newbie,))
        interaction = self._interaction(actor=actor, guild_roles=(novas, newbie))
        events = []

        async def check(*args):
            events.append('worker')
            return join_result()

        async def add(*args, **kwargs):
            events.append('add')

        async def remove(*args, **kwargs):
            events.append('remove')

        async def publish(*args, **kwargs):
            events.append('public')

        actor.add_roles = mock.AsyncMock(side_effect=add)
        actor.remove_roles = mock.AsyncMock(side_effect=remove)
        interaction.channel.send = mock.AsyncMock(side_effect=publish)
        command = self._root().get_command('join-novas')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'run_join_check', new=mock.AsyncMock(side_effect=check)
        ):
            await command.callback(cog, interaction)
        self.assertEqual(events, ['worker', 'add', 'remove', 'public'])
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)

    async def test_join_novas_unregistered_failure_is_private_and_nonmutating(self):
        cog = league.league.__new__(league.league)
        actor = member(10)
        interaction = self._interaction(actor=actor)
        command = self._root().get_command('join-novas')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service,
            'run_join_check',
            new=mock.AsyncMock(return_value=join_result(registered=False)),
        ):
            await command.callback(cog, interaction)
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
        self.assertIn('/player register', interaction.followup.send.await_args.args[0])
        actor.add_roles.assert_not_awaited()

    async def test_join_novas_existing_team_failure_is_private(self):
        cog = league.league.__new__(league.league)
        jets = role('The Jets')
        actor = member(10, roles=(jets,))
        interaction = self._interaction(actor=actor)
        command = self._root().get_command('join-novas')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service,
            'run_join_check',
            new=mock.AsyncMock(return_value=join_result()),
        ):
            await command.callback(cog, interaction)
        self.assertIn('already a member', interaction.followup.send.await_args.args[0])
        actor.add_roles.assert_not_awaited()

    async def test_join_novas_refuses_truncated_team_snapshot(self):
        cog = league.league.__new__(league.league)
        actor = member(10)
        interaction = self._interaction(actor=actor)
        command = self._root().get_command('join-novas')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service,
            'run_join_check',
            new=mock.AsyncMock(
                return_value=join_result(team_roles_truncated=True)
            ),
        ):
            await command.callback(cog, interaction)
        self.assertIn('too large', interaction.followup.send.await_args.args[0])
        actor.add_roles.assert_not_awaited()

    async def test_join_publication_failure_does_not_invite_role_retry(self):
        cog = league.league.__new__(league.league)
        novas = role(service.NOVAS_ROLE_NAME)
        actor = member(10)
        interaction = self._interaction(actor=actor, guild_roles=(novas,))
        interaction.channel.send.side_effect = RuntimeError('publish failed')
        command = self._root().get_command('join-novas')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service,
            'run_join_check',
            new=mock.AsyncMock(return_value=join_result()),
        ):
            await command.callback(cog, interaction)
        actor.add_roles.assert_awaited_once()
        message = interaction.followup.send.await_args.args[0]
        self.assertIn('was added', message)
        self.assertIn('Do not retry', message)


if __name__ == '__main__':
    unittest.main()
