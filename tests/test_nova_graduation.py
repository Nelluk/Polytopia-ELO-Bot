"""Focused P5.19b Nova graduation worker and Discord-effect tests."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.nova_graduation_workers')
service = import_offline_runtime('modules.nova_graduation')
league = import_offline_runtime('modules.league')
start_adapter = import_offline_runtime('modules.game_start')
start_workers = import_offline_runtime('modules.game_start_workers')


def snapshot(discord_id, *, nova=True, grad=False):
    return workers.NovaParticipantSnapshot(
        discord_id=discord_id,
        member_name=f'Member {discord_id}',
        mention=f'<@{discord_id}>',
        has_nova_role=nova,
        has_grad_role=grad,
    )


def request(*participants):
    return workers.NovaGraduationRequest(
        game_id=42,
        guild_id=300,
        allowed_guild_ids=(300, 301),
        participants=participants or (snapshot(100),),
    )


def player(player_id, discord_id, *, elo=1234):
    member = SimpleNamespace(
        id=player_id + 1000,
        discord_id=discord_id,
        elo_moonrise=elo,
    )
    return SimpleNamespace(
        id=player_id,
        discord_member=member,
        discord_member_id=member.id,
    )


def result(*candidates, draft_open=False, channel_id=None, message_id=None):
    return workers.NovaGraduationResult(
        game_id=42,
        guild_id=300,
        candidates=tuple(candidates),
        draft_open=draft_open,
        draft_channel_id=channel_id,
        draft_message_id=message_id,
    )


def candidate(discord_id):
    return workers.NovaGraduationCandidate(
        discord_id=discord_id,
        member_name=f'Member {discord_id}',
        mention=f'<@{discord_id}>',
        global_elo=1234 + discord_id,
        wins=3,
        losses=2,
        qualifying_game_ids=(12, 11),
    )


class NovaGraduationWorkerTests(unittest.TestCase):
    def test_dtos_are_frozen_and_validation_is_fail_closed(self):
        value = snapshot(100)
        with self.assertRaises(FrozenInstanceError):
            value.has_grad_role = True
        with self.assertRaises(workers.NovaGraduationError):
            workers.load_nova_graduation(
                workers.NovaGraduationRequest(42, 999, (300,), (value,))
            )
        with self.assertRaises(workers.NovaGraduationError):
            workers.load_nova_graduation(request(value, value))

    def test_worker_owns_connection_preserves_threshold_and_reads_config(self):
        eligible = player(10, 100)
        incomplete = player(20, 200)
        game_rows = (
            {'player': 10, 'game_id': 12, 'is_pending': False, 'is_completed': False},
            {'player': 10, 'game_id': 11, 'is_pending': False, 'is_completed': True},
            {'player': 20, 'game_id': 21, 'is_pending': False, 'is_completed': True},
        )
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=connection,
        ), mock.patch.object(
            workers,
            '_load_players',
            return_value={100: eligible, 200: incomplete},
        ), mock.patch.object(
            workers,
            '_load_game_rows',
            return_value=game_rows,
        ), mock.patch.object(
            workers,
            '_load_smallest_sides',
            return_value={11: 2, 12: 2, 21: 2},
        ), mock.patch.object(
            workers,
            '_load_global_records',
            return_value={eligible.discord_member_id: (3, 2)},
        ), mock.patch.object(
            workers,
            '_load_draft_state',
            return_value=(True, 700, 701),
        ) as draft_state:
            loaded = workers.load_nova_graduation(
                request(snapshot(100), snapshot(200))
            )

        self.assertEqual(
            tuple(item.discord_id for item in loaded.candidates),
            (100,),
        )
        self.assertEqual(loaded.candidates[0].qualifying_game_ids, (12, 11))
        self.assertEqual((loaded.candidates[0].wins, loaded.candidates[0].losses), (3, 2))
        self.assertTrue(loaded.draft_open)
        draft_state.assert_called_once_with(300)
        connection.__enter__.assert_called_once()

    def test_pending_solo_and_no_completed_games_do_not_qualify(self):
        players = {
            100: player(10, 100),
            200: player(20, 200),
            300: player(30, 300),
        }
        rows = (
            {'player': 10, 'game_id': 1, 'is_pending': False, 'is_completed': True},
            {'player': 10, 'game_id': 2, 'is_pending': False, 'is_completed': True},
            {'player': 20, 'game_id': 3, 'is_pending': False, 'is_completed': False},
            {'player': 20, 'game_id': 4, 'is_pending': False, 'is_completed': False},
            {'player': 30, 'game_id': 5, 'is_pending': True, 'is_completed': False},
            {'player': 30, 'game_id': 6, 'is_pending': False, 'is_completed': True},
        )
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_load_players', return_value=players
        ), mock.patch.object(
            workers, '_load_game_rows', return_value=rows
        ), mock.patch.object(
            workers,
            '_load_smallest_sides',
            return_value={1: 1, 2: 2, 3: 2, 4: 2, 5: 2, 6: 2},
        ), mock.patch.object(
            workers, '_load_global_records', return_value={}
        ), mock.patch.object(
            workers, '_load_draft_state', return_value=(False, None, None)
        ):
            loaded = workers.load_nova_graduation(
                request(snapshot(100), snapshot(200), snapshot(300))
            )
        self.assertEqual(loaded.candidates, ())

    def test_draft_state_read_never_creates_configuration(self):
        with mock.patch.object(
            workers.models.Configuration,
            'get_or_none',
            return_value=None,
        ) as get, mock.patch.object(
            workers.models.Configuration,
            'get_or_create',
        ) as create:
            self.assertEqual(workers._load_draft_state(300), (False, None, None))
        get.assert_called_once()
        create.assert_not_called()

    def test_slow_worker_keeps_loop_responsive_and_cancellation_drains(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return result()

        async def scenario():
            with mock.patch.object(
                workers,
                'load_nova_graduation',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_load_nova_graduation(request(snapshot(100)))
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = not task.done()
                task.cancel()
                await asyncio.sleep(0.005)
                draining = not task.done()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                return responsive, draining

        responsive, draining = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertTrue(draining)
        self.assertTrue(finished.is_set())


class FakeRole:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name


class FakeMember:
    def __init__(self, member_id, roles, *, add_error=None):
        self.id = member_id
        self.name = f'Member {member_id}'
        self.mention = f'<@{member_id}>'
        self.roles = list(roles)
        self.add_error = add_error
        self.added = []

    async def add_roles(self, role, **kwargs):
        if self.add_error:
            raise self.add_error
        self.added.append((role, kwargs))
        self.roles.append(role)


class NovaGraduationDiscordTests(unittest.IsolatedAsyncioTestCase):
    def guild(self, members, *, draft_channel=None):
        self.nova_role = FakeRole(1, 'The Novas')
        self.grad_role = FakeRole(2, 'Nova Grad')
        mapping = {member.id: member for member in members}
        return SimpleNamespace(
            id=300,
            roles=(self.nova_role, self.grad_role),
            get_member=lambda member_id: mapping.get(member_id),
            get_channel=lambda channel_id: (
                draft_channel
                if draft_channel is not None and draft_channel.id == channel_id
                else None
            ),
        )

    async def test_success_revalidates_roles_and_preserves_open_draft_wording(self):
        member = FakeMember(100, ())
        guild = self.guild((member,))
        member.roles.append(self.nova_role)
        draft_channel = SimpleNamespace(id=700, fetch_message=mock.AsyncMock())
        guild.get_channel = lambda channel_id: draft_channel if channel_id == 700 else None
        output = SimpleNamespace(send=mock.AsyncMock())
        log = mock.AsyncMock()
        loaded = result(
            candidate(100),
            draft_open=True,
            channel_id=700,
            message_id=701,
        )
        with mock.patch.dict(
            service.settings.server_ids,
            {'polychampions': 300, 'test': 301},
            clear=True,
        ), mock.patch.object(
            service.workers,
            'run_load_nova_graduation',
            new=mock.AsyncMock(return_value=loaded),
        ) as run, mock.patch.object(
            service.utilities,
            'send_to_log_channel',
            new=log,
        ):
            outcome = await service.run_nova_graduation(
                guild=guild,
                game_id=42,
                participant_ids=(100,),
                output_channel=output,
                nova_role_name='The Novas',
                grad_role_name='Nova Grad',
            )

        self.assertEqual(outcome.graduated_member_ids, (100,))
        self.assertEqual(outcome.warnings, ())
        self.assertEqual(run.await_args.args[0].participants, (snapshot(100),))
        self.assertEqual(member.added[0][0], self.grad_role)
        message = output.send.await_args.args[0]
        self.assertIn('Global ELO: 1334', message)
        self.assertIn('W 3 / L 2', message)
        self.assertIn('currently open in <#700>', message)
        log.assert_awaited_once_with(guild, message)

    async def test_candidate_failures_are_isolated_and_reported(self):
        first = FakeMember(100, (), add_error=RuntimeError('role denied'))
        second = FakeMember(200, ())
        guild = self.guild((first, second))
        first.roles.append(self.nova_role)
        second.roles.append(self.nova_role)
        output = SimpleNamespace(send=mock.AsyncMock())
        loaded = result(candidate(100), candidate(200))
        with mock.patch.dict(
            service.settings.server_ids,
            {'polychampions': 300, 'test': 301},
            clear=True,
        ), mock.patch.object(
            service.workers,
            'run_load_nova_graduation',
            new=mock.AsyncMock(return_value=loaded),
        ), mock.patch.object(
            service.utilities,
            'send_to_log_channel',
            new=mock.AsyncMock(),
        ):
            outcome = await service.run_nova_graduation(
                guild=guild,
                game_id=42,
                participant_ids=(100, 200),
                output_channel=output,
                nova_role_name='The Novas',
                grad_role_name='Nova Grad',
            )

        self.assertEqual(outcome.graduated_member_ids, (200,))
        self.assertEqual(len(outcome.warnings), 1)
        self.assertIn('<@100>', outcome.warnings[0])
        self.assertEqual(len(second.added), 1)
        output.send.assert_awaited_once()

    async def test_missing_roles_and_out_of_scope_guild_do_no_work(self):
        guild = SimpleNamespace(id=999, roles=(), get_member=mock.Mock())
        worker = mock.AsyncMock()
        with mock.patch.dict(
            service.settings.server_ids,
            {'polychampions': 300, 'test': 301},
            clear=True,
        ), mock.patch.object(
            service.workers,
            'run_load_nova_graduation',
            new=worker,
        ):
            outcome = await service.run_nova_graduation(
                guild=guild,
                game_id=42,
                participant_ids=(100,),
                nova_role_name='The Novas',
                grad_role_name='Nova Grad',
            )
        self.assertEqual(outcome.graduated_member_ids, ())
        worker.assert_not_awaited()

    async def test_legacy_wrapper_delegates_to_shared_service(self):
        game = SimpleNamespace(
            id=42,
            lineup=(
                SimpleNamespace(
                    player=SimpleNamespace(
                        discord_member=SimpleNamespace(discord_id=100)
                    )
                ),
            ),
        )
        guild = SimpleNamespace(id=300)
        outcome = service.NovaGraduationOutcome(42, (), ())
        with mock.patch.object(
            league.nova_graduation,
            'run_nova_graduation',
            new=mock.AsyncMock(return_value=outcome),
        ) as run:
            returned = await league.auto_grad_novas(guild, game)
        self.assertIs(returned, outcome)
        self.assertEqual(run.await_args.kwargs['participant_ids'], (100,))

    async def test_start_reload_failure_still_uses_committed_participant_ids(self):
        sent = []

        async def send(content=None, **_kwargs):
            sent.append(content)

        guild = SimpleNamespace(id=300)
        output = SimpleNamespace(send=send)
        start_result = start_workers.StartResult(
            game_id=42,
            guild_id=300,
            name='Fields of Fire',
            requester_id=100,
            mentions=('<@100>', '<@200>'),
            participant_ids=(100, 200),
            missing_member_warnings=(),
            name_warning=None,
            league_warning=None,
            creator_id=100,
            host_id=100,
        )
        run = mock.AsyncMock(
            return_value=service.NovaGraduationOutcome(
                42,
                (),
                ('Nova warning',),
            )
        )
        with mock.patch.object(
            start_adapter.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(side_effect=RuntimeError('reload failed')),
        ), mock.patch.object(
            start_adapter.settings,
            'guild_setting',
            return_value=False,
        ), mock.patch.object(
            start_adapter.nova_graduation,
            'run_nova_graduation',
            new=run,
        ):
            await start_adapter.publish_start_result(
                start_result,
                output_context=output,
                guild=guild,
                prefix='$',
                bot_guilds=(guild,),
            )

        self.assertEqual(run.await_args.kwargs['game_id'], 42)
        self.assertEqual(run.await_args.kwargs['participant_ids'], (100, 200))
        self.assertIn('Nova warning', sent)
        self.assertTrue(any('now being tracked' in str(item) for item in sent))


if __name__ == '__main__':
    unittest.main()
