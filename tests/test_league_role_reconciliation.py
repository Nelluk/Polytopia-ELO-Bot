"""Focused P8.24 league team-role reconciliation coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_role_workers')
league = import_offline_runtime('modules.league')


def request(*, before=(), after=()):
    return workers.LeagueRoleUpdateRequest(
        guild_id=300,
        member_id=20,
        member_description='**Member** (`20`)',
        before_role_names=tuple(before),
        after_role_names=tuple(after),
    )


def result(**overrides):
    values = dict(
        guild_id=300,
        member_id=20,
        changed=True,
        registered=True,
        ambiguous=False,
        before_team_names=(),
        after_team_name='The Ronin',
        player_id=40,
        previous_team_id=None,
        team_id=50,
        team_name='The Ronin',
        house_name='Ronin House',
        league_tier=2,
        managed_house_names=('Old House', 'Ronin House'),
        log_message='**Member** (`20`) had team role **The Ronin** added.',
    )
    values.update(overrides)
    return workers.LeagueRoleUpdateResult(**values)


class FakeRole:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name


class FakeGuild:
    def __init__(self, roles, guild_id=300):
        self.id = guild_id
        self.roles = list(roles)


class FakeMember:
    def __init__(self, member_id, roles, guild, *, edit_error=None):
        self.id = member_id
        self.name = f'Member {member_id}'
        self.display_name = self.name
        self.roles = list(roles)
        self.guild = guild
        self.edit_error = edit_error
        self.edit_calls = []

    async def edit(self, *, roles, reason):
        self.edit_calls.append((tuple(roles), reason))
        if self.edit_error is not None:
            raise self.edit_error
        self.roles = list(roles)


class WorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitive_boundaries(self):
        row = request(before=('Old',), after=('New',))
        with self.assertRaises(FrozenInstanceError):
            row.member_id = 1
        self.assertEqual(row.before_role_names, ('Old',))
        self.assertTrue(all(isinstance(name, str) for name in row.after_role_names))

    def test_assignment_updates_team_clears_preferences_and_audits_atomically(self):
        player = SimpleNamespace(id=40, team_id=None, team=None)
        player.save = mock.Mock()
        team = SimpleNamespace(
            id=50,
            name='The Ronin',
            house_id=60,
            house=SimpleNamespace(name='Ronin House'),
            league_tier=2,
        )
        atomic = mock.MagicMock(return_value=nullcontext())
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers.models.db, 'atomic', atomic
        ), mock.patch.object(
            workers, '_active_team_rows', return_value=((50, 'The Ronin'),)
        ), mock.patch.object(
            workers, '_player_for_member', return_value=player
        ), mock.patch.object(
            workers, '_team_by_id', return_value=team
        ), mock.patch.object(
            workers, '_managed_house_names', return_value=('Ronin House',)
        ), mock.patch.object(
            workers.models.PlayerHousePreference, 'clear_preferences'
        ) as clear, mock.patch.object(
            workers.models.GameLog, 'write'
        ) as audit:
            actual = workers.reconcile_league_team_role(
                request(after=('The Ronin',))
            )

        self.assertEqual(player.team, 50)
        player.save.assert_called_once_with(only=[workers.models.Player.team])
        clear.assert_called_once_with(40)
        audit.assert_called_once_with(
            guild_id=300,
            game_id=0,
            message='**Member** (`20`) had team role **The Ronin** added.',
        )
        atomic.assert_called_once_with()
        self.assertEqual(actual.team_id, 50)
        self.assertEqual(actual.house_name, 'Ronin House')
        self.assertEqual(actual.league_tier, 2)

    def test_removal_clears_stale_team_without_erasing_preferences(self):
        player = SimpleNamespace(id=40, team_id=50, team=50)
        player.save = mock.Mock()
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers.models.db, 'atomic', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_active_team_rows', return_value=((50, 'The Ronin'),)
        ), mock.patch.object(
            workers, '_player_for_member', return_value=player
        ), mock.patch.object(
            workers, '_managed_house_names', return_value=('Ronin House',)
        ), mock.patch.object(
            workers.models.PlayerHousePreference, 'clear_preferences'
        ) as clear, mock.patch.object(
            workers.models.GameLog, 'write'
        ) as audit:
            actual = workers.reconcile_league_team_role(
                request(before=('The Ronin',))
            )

        self.assertIsNone(player.team)
        clear.assert_not_called()
        self.assertIsNone(actual.team_id)
        self.assertIn('removed and is teamless', audit.call_args.kwargs['message'])

    def test_unchanged_and_ambiguous_role_sets_do_not_load_or_mutate_player(self):
        for row, expected_ambiguous in (
            (request(before=('The Ronin',), after=('The Ronin',)), False),
            (request(after=('The Ronin', 'The Novas')), True),
        ):
            with self.subTest(ambiguous=expected_ambiguous), mock.patch.object(
                workers.models.db, 'connection_context', return_value=nullcontext()
            ), mock.patch.object(
                workers.models.db, 'atomic', return_value=nullcontext()
            ), mock.patch.object(
                workers,
                '_active_team_rows',
                return_value=((50, 'The Ronin'), (51, 'The Novas')),
            ), mock.patch.object(
                workers, '_player_for_member'
            ) as load_player, mock.patch.object(
                workers.models.GameLog, 'write'
            ) as audit:
                actual = workers.reconcile_league_team_role(row)
            load_player.assert_not_called()
            audit.assert_not_called()
            self.assertEqual(actual.ambiguous, expected_ambiguous)
            self.assertFalse(actual.registered)

    def test_unregistered_transition_has_no_audit(self):
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers.models.db, 'atomic', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_active_team_rows', return_value=((50, 'The Ronin'),)
        ), mock.patch.object(
            workers, '_player_for_member', side_effect=workers.peewee.DoesNotExist
        ), mock.patch.object(workers.models.GameLog, 'write') as audit:
            actual = workers.reconcile_league_team_role(
                request(after=('The Ronin',))
            )
        audit.assert_not_called()
        self.assertFalse(actual.registered)

    def test_executor_is_responsive_and_drains_after_cancellation(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(
                workers, 'reconcile_league_team_role', side_effect=slow
            ):
                task = asyncio.create_task(
                    workers.run_league_team_role_update(request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = False

                async def marker():
                    nonlocal responsive
                    responsive = True

                await marker()
                task.cancel()
                await asyncio.sleep(0.005)
                still_draining = not task.done()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return responsive, still_draining

        self.assertEqual(asyncio.run(scenario()), (True, True))


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.everyone = FakeRole(1, '@everyone')
        self.team = FakeRole(2, 'The Ronin')
        self.old_house = FakeRole(3, 'Old House')
        self.new_house = FakeRole(4, 'Ronin House')
        self.old_tier = FakeRole(5, 'Junior Player')
        self.new_tier = FakeRole(6, 'Pro Player')
        self.league_role = FakeRole(7, 'League Member')
        self.preference = FakeRole(8, 'Prefers Ronin House')
        self.unrelated = FakeRole(9, 'Tester')
        self.guild = FakeGuild((
            self.everyone,
            self.team,
            self.old_house,
            self.new_house,
            self.old_tier,
            self.new_tier,
            self.league_role,
            self.preference,
            self.unrelated,
        ))
        self.cog = league.league(SimpleNamespace())

    async def test_commit_precedes_role_edit_and_public_audit(self):
        before = FakeMember(20, (self.everyone, self.unrelated), self.guild)
        after = FakeMember(
            20,
            (
                self.everyone,
                self.unrelated,
                self.team,
                self.old_house,
                self.old_tier,
                self.preference,
            ),
            self.guild,
        )
        events = []

        async def run(captured):
            events.append('worker')
            self.assertEqual(captured.after_role_names[-1], 'Prefers Ronin House')
            return result()

        async def edit(*, roles, reason):
            events.append('edit')
            after.roles = list(roles)

        async def send(_guild, message):
            events.append(('log', message))

        after.edit = edit
        with mock.patch.object(
            league.settings, 'server_ids', {'polychampions': 300, 'test': 301}
        ), mock.patch.object(
            league.league_role_workers,
            'run_league_team_role_update',
            side_effect=run,
        ), mock.patch.object(
            league, '_send_league_role_log', side_effect=send
        ), mock.patch.object(
            league.settings, 'league_tiers', ((1, 'Junior'), (2, 'Pro'))
        ):
            await self.cog.on_member_update(before, after)

        self.assertEqual(events[0:2], ['worker', 'edit'])
        self.assertEqual(events[2][0], 'log')
        role_names = {role.name for role in after.roles}
        self.assertIn('The Ronin', role_names)
        self.assertIn('Tester', role_names)
        self.assertIn('Ronin House', role_names)
        self.assertIn('Pro Player', role_names)
        self.assertIn('League Member', role_names)
        self.assertNotIn('Old House', role_names)
        self.assertNotIn('Junior Player', role_names)
        self.assertNotIn('Prefers Ronin House', role_names)

    async def test_removal_strips_derived_roles_but_keeps_unrelated_roles(self):
        before = FakeMember(
            20,
            (self.everyone, self.team, self.new_house, self.new_tier, self.league_role),
            self.guild,
        )
        after = FakeMember(
            20,
            (self.everyone, self.new_house, self.new_tier, self.league_role, self.unrelated),
            self.guild,
        )
        removed = result(
            before_team_names=('The Ronin',),
            after_team_name=None,
            team_id=None,
            team_name=None,
            house_name=None,
            league_tier=None,
            log_message='removed',
        )
        with mock.patch.object(
            league.settings, 'server_ids', {'polychampions': 300, 'test': 301}
        ), mock.patch.object(
            league.league_role_workers,
            'run_league_team_role_update',
            new=mock.AsyncMock(return_value=removed),
        ), mock.patch.object(
            league, '_send_league_role_log', new=mock.AsyncMock()
        ), mock.patch.object(
            league.settings, 'league_tiers', ((1, 'Junior'), (2, 'Pro'))
        ):
            await self.cog.on_member_update(before, after)
        self.assertEqual(
            {role.name for role in after.roles},
            {'@everyone', 'Tester'},
        )

    async def test_worker_failure_has_no_discord_effect(self):
        before = FakeMember(20, (self.everyone,), self.guild)
        after = FakeMember(20, (self.everyone, self.team), self.guild)
        with mock.patch.object(
            league.settings, 'server_ids', {'polychampions': 300, 'test': 301}
        ), mock.patch.object(
            league.league_role_workers,
            'run_league_team_role_update',
            new=mock.AsyncMock(side_effect=RuntimeError('database down')),
        ), mock.patch.object(
            league, '_send_league_role_log', new=mock.AsyncMock()
        ) as send:
            await self.cog.on_member_update(before, after)
        self.assertEqual(after.edit_calls, [])
        send.assert_not_awaited()

    async def test_discord_failure_reports_committed_reconciliation_problem(self):
        before = FakeMember(20, (self.everyone,), self.guild)
        after = FakeMember(
            20,
            (self.everyone, self.team),
            self.guild,
            edit_error=RuntimeError('forbidden'),
        )
        with mock.patch.object(
            league.settings, 'server_ids', {'polychampions': 300, 'test': 301}
        ), mock.patch.object(
            league.league_role_workers,
            'run_league_team_role_update',
            new=mock.AsyncMock(return_value=result()),
        ), mock.patch.object(
            league, '_send_league_role_log', new=mock.AsyncMock()
        ) as send, mock.patch.object(
            league.settings, 'league_tiers', ((1, 'Junior'), (2, 'Pro'))
        ):
            await self.cog.on_member_update(before, after)
        self.assertEqual(send.await_count, 1)
        self.assertIn('database change committed', send.await_args.args[1])
        self.assertIn('need reconciliation', send.await_args.args[1])

    async def test_missing_configured_role_is_staff_visible(self):
        self.guild.roles.remove(self.new_house)
        before = FakeMember(20, (self.everyone,), self.guild)
        after = FakeMember(20, (self.everyone, self.team), self.guild)
        with mock.patch.object(
            league.settings, 'server_ids', {'polychampions': 300, 'test': 301}
        ), mock.patch.object(
            league.league_role_workers,
            'run_league_team_role_update',
            new=mock.AsyncMock(return_value=result()),
        ), mock.patch.object(
            league, '_send_league_role_log', new=mock.AsyncMock()
        ) as send, mock.patch.object(
            league.settings, 'league_tiers', ((1, 'Junior'), (2, 'Pro'))
        ):
            await self.cog.on_member_update(before, after)
        self.assertEqual(send.await_count, 2)
        self.assertIn('Ronin House', send.await_args_list[1].args[1])


if __name__ == '__main__':
    unittest.main()
