"""Focused Tier-3 coverage for development-beta fixture controls."""

import asyncio
from dataclasses import FrozenInstanceError
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.operator_beta_fixtures_workers')
service = import_offline_runtime('modules.operator_beta_fixtures')
views = import_offline_runtime('modules.operator_beta_fixtures_views')
administration = import_offline_runtime('modules.administration')
misc = import_offline_runtime('modules.misc')


def game(
    scenario,
    game_id,
    *,
    completed=False,
    confirmed=False,
    pending=False,
    season=None,
    tier=None,
    participants=(10, 20),
):
    return workers.dev_fixtures.FixtureGame(
        scenario=scenario,
        game_id=game_id,
        name=f'Beta Fixture {scenario.title()}',
        is_completed=completed,
        is_confirmed=confirmed,
        is_ranked=True,
        is_pending=pending,
        expiration=None,
        league_season=season,
        league_tier=tier,
        participant_ids=participants,
        winner_position=1 if completed else None,
    )


def state(*games):
    return workers.dev_fixtures.FixtureState(
        guild_id=300,
        user_ids=(10, 20) if games else (),
        games=tuple(games),
    )


def canonical_state():
    return state(
        game('ready', 1),
        game(
            'unconfirmed',
            2,
            completed=True,
            season=workers.dev_fixtures.FIXTURE_CURRENT_LEAGUE_SEASON,
            tier=workers.dev_fixtures.FIXTURE_LEAGUE_TIER,
        ),
        game(
            'completed',
            3,
            completed=True,
            confirmed=True,
            season=workers.dev_fixtures.FIXTURE_COMPLETED_LEAGUE_SEASON,
            tier=workers.dev_fixtures.FIXTURE_LEAGUE_TIER,
        ),
    )


class BetaFixtureWorkerTests(unittest.TestCase):
    def test_readiness_and_completion_render_names_before_diagnostic_ids(self):
        participant = workers.BetaFixtureParticipant(10, 'Nelluk')
        snapshot = workers._snapshot(canonical_state())
        snapshot = workers.replace(snapshot, participants=(participant,))
        self.assertIn('**Nelluk** (`10`)', service.readiness_markdown(snapshot))
        result = workers.BetaFixtureResult(
            operation=workers.RESET,
            guild_id=300,
            user_ids=(10,),
            scenarios=(),
            old_game_ids=(1, 2, 3),
            new_game_ids=(4, 5, 6),
        )
        self.assertIn(
            '**Nelluk** (`10`)',
            service.completion_markdown(result, participants=(participant,)),
        )

    def test_requests_and_results_are_immutable_primitive_snapshots(self):
        request = workers.BetaFixtureCommitRequest(
            operation=workers.RESET,
            guild_id=300,
            requester_id=10,
            requester_description='owner',
            user_ids=(10, 20),
            expected_game_ids=(1, 2, 3),
            expected_fingerprint='abc',
        )
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 999
        self.assertNotIn('Member', repr(request))

    def test_snapshot_classifies_ready_exercised_and_ambiguous_bundles(self):
        ready = workers._snapshot(canonical_state())
        self.assertEqual(ready.readiness, 'ready')
        self.assertTrue(ready.resettable)
        exercised = canonical_state()
        exercised = state(
            game('ready', 1, pending=True),
            *exercised.games[1:],
        )
        self.assertEqual(
            workers._snapshot(exercised).readiness,
            'needs reset',
        )
        ambiguous = state(
            game('ready', 1, participants=(10,)),
        )
        snapshot = workers._snapshot(ambiguous)
        self.assertEqual(snapshot.readiness, 'manual review required')
        self.assertFalse(snapshot.resettable)

    def test_owner_is_revalidated_before_runtime_or_database(self):
        request = workers.BetaFixturePreviewRequest(
            operation=workers.PREPARE,
            guild_id=300,
            requester_id=99,
            user_ids=(10, 20),
        )
        with mock.patch.object(workers.settings, 'owner_id', 10), \
                mock.patch.object(workers, '_validate_runtime') as runtime:
            with self.assertRaises(workers.BetaFixturePermissionError):
                workers.load_preview(request)
        runtime.assert_not_called()

    def test_prepare_requires_empty_bundle_and_two_registered_users(self):
        request = workers.BetaFixturePreviewRequest(
            operation=workers.PREPARE,
            guild_id=300,
            requester_id=10,
            user_ids=(10, 20),
        )
        with mock.patch.object(workers.settings, 'owner_id', 10), \
                mock.patch.object(workers, '_validate_runtime', return_value=300), \
                mock.patch.object(workers, '_load_state', return_value=state()), \
                mock.patch.object(workers, '_load_participants', return_value=()), \
                mock.patch.object(workers, '_validate_registered_users') as registered:
            preview = workers.load_preview(request)
        self.assertEqual(preview.user_ids, (10, 20))
        registered.assert_called_once_with(300, (10, 20))

        with mock.patch.object(workers.settings, 'owner_id', 10), \
                mock.patch.object(workers, '_validate_runtime', return_value=300), \
                mock.patch.object(
                    workers, '_load_state', return_value=canonical_state()
                ), mock.patch.object(workers, '_load_participants', return_value=()), \
                mock.patch.object(workers, '_validate_registered_users'):
            with self.assertRaises(workers.BetaFixtureValidationError):
                workers.load_preview(request)

    def test_bot_identities_are_rejected_before_player_lookup(self):
        with mock.patch.object(workers.settings, 'bot_id', 10), \
                mock.patch.object(workers.settings, 'bot_id_beta', 20), \
                mock.patch.object(workers.models, 'db') as database:
            with self.assertRaises(workers.BetaFixtureValidationError):
                workers._validate_registered_users(300, (10, 30))
        database.connection_context.assert_not_called()

    def test_reset_uses_existing_participants_and_refuses_ambiguous_state(self):
        request = workers.BetaFixturePreviewRequest(
            operation=workers.RESET,
            guild_id=300,
            requester_id=10,
        )
        with mock.patch.object(workers.settings, 'owner_id', 10), \
                mock.patch.object(workers, '_validate_runtime', return_value=300), \
                mock.patch.object(
                    workers, '_load_state', return_value=canonical_state()
                ), mock.patch.object(workers, '_load_participants', return_value=()), \
                mock.patch.object(workers, '_validate_registered_users'):
            preview = workers.load_preview(request)
        self.assertEqual(preview.user_ids, (10, 20))
        self.assertEqual(preview.snapshot.game_ids, (1, 2, 3))

        ambiguous = state(game('ready', 1, participants=(10,)))
        with mock.patch.object(workers.settings, 'owner_id', 10), \
                mock.patch.object(workers, '_validate_runtime', return_value=300), \
                mock.patch.object(workers, '_load_state', return_value=ambiguous), \
                mock.patch.object(workers, '_load_participants', return_value=()):
            with self.assertRaises(workers.BetaFixtureValidationError):
                workers.load_preview(request)

    def test_commit_rechecks_fingerprint_before_mutation(self):
        request = workers.BetaFixtureCommitRequest(
            operation=workers.RESET,
            guild_id=300,
            requester_id=10,
            requester_description='owner',
            user_ids=(10, 20),
            expected_game_ids=(1, 2, 3),
            expected_fingerprint='stale',
        )
        with mock.patch.object(workers.settings, 'owner_id', 10), \
                mock.patch.object(workers, '_validate_runtime', return_value=300), \
                mock.patch.object(
                    workers, '_load_state', return_value=canonical_state()
                ), mock.patch.object(
                    workers.dev_fixtures, 'reset_fixtures_in_process'
                ) as reset:
            with self.assertRaises(workers.BetaFixtureStaleError):
                workers.commit_fixtures(request)
        reset.assert_not_called()

    def test_reset_commit_passes_exact_preview_state_and_returns_dto(self):
        before = canonical_state()
        after = state(
            game('ready', 11),
            game(
                'unconfirmed', 12, completed=True,
                season=workers.dev_fixtures.FIXTURE_CURRENT_LEAGUE_SEASON,
                tier=workers.dev_fixtures.FIXTURE_LEAGUE_TIER,
            ),
            game(
                'completed', 13, completed=True, confirmed=True,
                season=workers.dev_fixtures.FIXTURE_COMPLETED_LEAGUE_SEASON,
                tier=workers.dev_fixtures.FIXTURE_LEAGUE_TIER,
            ),
        )
        snapshot = workers._snapshot(before)
        request = workers.BetaFixtureCommitRequest(
            operation=workers.RESET,
            guild_id=300,
            requester_id=10,
            requester_description='owner',
            user_ids=(10, 20),
            expected_game_ids=snapshot.game_ids,
            expected_fingerprint=snapshot.fingerprint,
        )
        with mock.patch.object(workers.settings, 'owner_id', 10), \
                mock.patch.object(workers, '_validate_runtime', return_value=300), \
                mock.patch.object(workers, '_load_state', return_value=before), \
                mock.patch.object(workers, '_validate_registered_users'), \
                mock.patch.object(
                    workers.dev_fixtures,
                    'reset_fixtures_in_process',
                    return_value=after,
                ) as reset:
            result = workers.commit_fixtures(request)
        self.assertEqual(result.new_game_ids, (11, 12, 13))
        self.assertIs(reset.call_args.kwargs['expected_state'], before)


class BetaFixtureAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_and_timeout_close_preview_without_confirming(self):
        preview = workers.BetaFixturePreview(
            operation=workers.PREPARE,
            snapshot=workers._snapshot(state()),
            user_ids=(10, 20),
            can_commit=True,
        )
        confirmer = mock.AsyncMock()
        view = views.BetaFixturePreviewView(
            requester_id=10,
            preview=preview,
            confirmer=confirmer,
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock())
        )
        await view._cancel(interaction)
        self.assertTrue(view.finished)
        confirmer.assert_not_awaited()
        interaction.response.edit_message.assert_awaited_once_with(view=view)

        timeout_view = views.BetaFixturePreviewView(
            requester_id=10,
            preview=preview,
            confirmer=confirmer,
        )
        timeout_view.message = SimpleNamespace(edit=mock.AsyncMock())
        await timeout_view.on_timeout()
        self.assertTrue(timeout_view.finished)
        self.assertIn('Run the command again', timeout_view.status)
        confirmer.assert_not_awaited()

    async def test_mutation_runs_through_elo_coordinator(self):
        request = workers.BetaFixtureCommitRequest(
            operation=workers.RESET,
            guild_id=300,
            requester_id=10,
            requester_description='owner',
            user_ids=(10, 20),
            expected_game_ids=(1, 2, 3),
            expected_fingerprint='abc',
        )
        coordinator = SimpleNamespace(run=mock.AsyncMock(return_value='done'))
        with mock.patch.object(
            workers.settings, 'elo_job_coordinator', coordinator
        ):
            self.assertEqual(await workers.run_commit(request), 'done')
        kwargs = coordinator.run.await_args.kwargs
        self.assertEqual(kwargs['operation'], 'beta_fixture_reset')
        self.assertIs(kwargs['worker'], workers.commit_fixtures)

    async def test_cancelled_read_drains_owned_worker(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(_request):
            started.set()
            release.wait(2)
            return 'finished'

        task = asyncio.create_task(workers._run_read(blocking, object()))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(started.is_set())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_publication_failure_keeps_known_committed_result_terminal(self):
        snapshot = workers._snapshot(state())
        preview = workers.BetaFixturePreview(
            operation=workers.PREPARE,
            snapshot=snapshot,
            user_ids=(10, 20),
            can_commit=True,
        )
        result = workers.BetaFixtureResult(
            operation=workers.PREPARE,
            guild_id=300,
            user_ids=(10, 20),
            scenarios=(
                workers.BetaFixtureScenario('ready', 11, 'ready'),
            ),
            old_game_ids=(),
            new_game_ids=(11,),
        )
        view = views.BetaFixturePreviewView(
            requester_id=10,
            preview=preview,
            confirmer=mock.AsyncMock(return_value=result),
        )
        view.message = SimpleNamespace(edit=mock.AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(
                send=mock.AsyncMock(side_effect=RuntimeError('send failed'))
            ),
        )
        with self.assertRaisesRegex(RuntimeError, 'send failed'):
            await view._confirm(interaction)
        self.assertTrue(view.finished)
        self.assertFalse(view.busy)
        self.assertIn('prepared', view.status)


class BetaFixtureAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(
            administration.administration
        )
        self.cog.bot = object()
        operator = next(
            item
            for item in administration.administration.__cog_app_commands__
            if item.name == 'operator'
        )
        beta = operator.get_command('beta')
        self.prepare = beta.get_command('prepare')
        self.reset = beta.get_command('reset')

    def interaction(self, user_id=10):
        message = SimpleNamespace(edit=mock.AsyncMock())
        return SimpleNamespace(
            guild_id=300,
            channel_id=400,
            user=SimpleNamespace(id=user_id, display_name='Owner'),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            original_response=mock.AsyncMock(return_value=message),
        )

    def test_exact_command_shape(self):
        self.assertEqual(
            [(item.name, item.required) for item in self.prepare.parameters],
            [('participant_one', True), ('participant_two', True)],
        )
        self.assertEqual(self.reset.parameters, [])

    async def test_non_owner_denied_before_worker(self):
        interaction = self.interaction(99)
        with mock.patch.object(administration.settings, 'owner_id', 10), \
                mock.patch.object(
                    administration.operator_beta_fixtures_workers,
                    'run_preview',
                    new=mock.AsyncMock(),
                ) as run_preview:
            await self.reset.callback(self.cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        run_preview.assert_not_awaited()

    async def test_whattotest_opens_private_compact_dashboard(self):
        command = next(
            item for item in misc.misc.__cog_app_commands__
            if item.name == 'whattotest'
        )
        interaction = self.interaction()
        status = SimpleNamespace(overall='ready', packs=(), result_snapshot=None)
        guide = misc.beta_testing_guide.parse_checklist(
            '# 🧪 WHAT TO TEST\n\n## Games\n\n- Run /game show.'
        )
        with mock.patch.object(
            misc.settings,
            'runtime_profile',
            SimpleNamespace(environment='development'),
        ), mock.patch.object(
            misc.beta_testing_guide,
            'load_guide',
            return_value=guide,
        ), mock.patch.object(
            misc.beta_lab_workers,
            'run_status',
            new=mock.AsyncMock(return_value=status),
        ), mock.patch.object(
            misc.beta_lab_sessions,
            'run_requester_session',
            new=mock.AsyncMock(return_value=None),
        ):
            await command.callback(SimpleNamespace(bot=object()), interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn(
            'BetaTestingDashboard',
            type(interaction.edit_original_response.await_args.kwargs['view']).__name__,
        )
        interaction.followup.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
