"""Focused coverage for the compact Beta Lab foundation."""

import asyncio
import contextlib
import io
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


lab = import_offline_runtime('modules.beta_lab_workers')
guide_module = import_offline_runtime('modules.beta_testing_guide')
dashboard = import_offline_runtime('modules.beta_testing_dashboard')
from scripts import manage_beta_lab


def pack(key, state='ready'):
    return lab.BetaLabPackStatus(
        key=key,
        title=key.replace('-', ' ').title(),
        state=state,
        detail=f'{key} detail',
        action=f'{key} action',
    )


class BetaLabWorkerTests(unittest.TestCase):
    def test_validation_requires_exact_beta_profile_and_guild(self):
        profile = SimpleNamespace(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
            background_tasks_enabled=False,
            api_enabled=False,
            bullet_enabled=False,
            allowed_guild_ids=(lab.beta_readiness.BETA_GUILD_ID,),
        )
        with mock.patch.object(lab.settings, 'runtime_profile', profile):
            self.assertEqual(
                lab._validate(lab.beta_readiness.BETA_GUILD_ID),
                lab.beta_readiness.BETA_GUILD_ID,
            )
            with self.assertRaises(lab.BetaLabError):
                lab._validate(999)
            profile.allowed_guild_ids = (
                lab.beta_readiness.BETA_GUILD_ID,
                999,
            )
            with self.assertRaises(lab.BetaLabError):
                lab._validate(lab.beta_readiness.BETA_GUILD_ID)

    def test_status_is_frozen_and_reports_attention(self):
        snapshot = SimpleNamespace()
        with mock.patch.object(lab, '_validate', return_value=300), \
                mock.patch.object(lab, '_structure_status', return_value=pack(lab.STRUCTURE)), \
                mock.patch.object(lab, '_leaderboard_status', return_value=pack(lab.LEADERBOARD)), \
                mock.patch.object(
                    lab,
                    '_result_status',
                    return_value=(pack(lab.RESULTS, 'refreshable'), snapshot),
                ):
            status = lab.load_status(300)

        self.assertEqual(status.overall, 'needs attention')
        self.assertEqual([item.key for item in status.packs], list(lab.PACKS))
        plan = status.plan_dict()
        self.assertEqual(plan['live_apply_supported'], [lab.RESULTS])
        self.assertFalse(plan['discord_resource_mutation_supported'])

    def test_cli_refuses_wrong_confirmation_before_socket(self):
        with mock.patch.object(manage_beta_lab, '_profile', return_value=object()), \
                mock.patch.object(
                    manage_beta_lab,
                    'send_control_request',
                    new=mock.AsyncMock(),
                ) as request:
            with contextlib.redirect_stderr(io.StringIO()):
                result = manage_beta_lab.main([
                    'refresh',
                    '--pack',
                    'game-results',
                    '--confirm',
                    'wrong',
                ])
        self.assertEqual(result, 2)
        request.assert_not_awaited()

    def test_cli_status_uses_protected_control_request(self):
        with mock.patch.object(manage_beta_lab, '_profile', return_value=object()), \
                mock.patch.object(
                    manage_beta_lab,
                    'send_control_request',
                    new=mock.AsyncMock(return_value={'overall': 'ready'}),
                ) as request, mock.patch('builtins.print'):
            result = manage_beta_lab.main(['--json', 'status'])
        self.assertEqual(result, 0)
        request.assert_awaited_once_with(
            mock.ANY,
            {'operation': 'beta-lab-status'},
            timeout=60.0,
        )


class BetaLabAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_read_drains_worker(self):
        started = threading.Event()
        release = threading.Event()

        def blocking():
            started.set()
            release.wait(2)
            return 'done'

        task = asyncio.create_task(lab._run(blocking))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_refresh_uses_preview_and_coordinated_commit(self):
        preview = lab.result_workers.BetaFixturePreview(
            operation=lab.result_workers.RESET,
            snapshot=SimpleNamespace(game_ids=(1, 2, 3), fingerprint='fingerprint'),
            user_ids=(10, 20),
            can_commit=True,
        )
        final = lab.BetaLabStatus(300, 'ready', (), None)
        committed = lab.result_workers.BetaFixtureResult(
            operation=lab.result_workers.RESET,
            guild_id=300,
            user_ids=(10, 20),
            scenarios=(),
            old_game_ids=(1, 2, 3),
            new_game_ids=(4, 5, 6),
        )
        with mock.patch.object(lab, '_validate', return_value=300), \
                mock.patch.object(lab.settings, 'owner_id', 10), \
                mock.patch.object(
                    lab.result_workers,
                    'run_preview',
                    new=mock.AsyncMock(return_value=preview),
                ), mock.patch.object(
                    lab.result_workers,
                    'run_commit',
                    new=mock.AsyncMock(return_value=committed),
                ) as commit, mock.patch.object(
                    lab,
                    'run_status',
                    new=mock.AsyncMock(return_value=final),
                ):
            result = await lab.refresh_results(guild_id=300, actor='operator')
        self.assertTrue(result.committed)
        self.assertIs(result.status, final)
        self.assertEqual(result.new_game_ids, (4, 5, 6))
        request = commit.await_args.args[0]
        self.assertEqual(request.expected_game_ids, (1, 2, 3))
        self.assertEqual(request.requester_description, 'operator')

    async def test_cancelled_refresh_drains_started_commit_and_returns_truth(self):
        preview = lab.result_workers.BetaFixturePreview(
            operation=lab.result_workers.RESET,
            snapshot=SimpleNamespace(game_ids=(1, 2, 3), fingerprint='fingerprint'),
            user_ids=(10, 20),
            can_commit=True,
        )
        committed = lab.result_workers.BetaFixtureResult(
            operation=lab.result_workers.RESET,
            guild_id=300,
            user_ids=(10, 20),
            scenarios=(),
            old_game_ids=(1, 2, 3),
            new_game_ids=(4, 5, 6),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def commit(_request):
            started.set()
            await release.wait()
            return committed

        with mock.patch.object(lab, '_validate', return_value=300), \
                mock.patch.object(lab.settings, 'owner_id', 10), \
                mock.patch.object(
                    lab.result_workers,
                    'run_preview',
                    new=mock.AsyncMock(return_value=preview),
                ), mock.patch.object(
                    lab.result_workers,
                    'run_commit',
                    side_effect=commit,
                ), mock.patch.object(
                    lab,
                    'run_status',
                    new=mock.AsyncMock(
                        return_value=lab.BetaLabStatus(300, 'ready', (), None)
                    ),
                ):
            task = asyncio.create_task(
                lab.refresh_results(guild_id=300, actor='operator')
            )
            await started.wait()
            task.cancel()
            release.set()
            result = await task
        self.assertTrue(result.committed)
        self.assertEqual(result.new_game_ids, (4, 5, 6))

    async def test_post_commit_status_failure_warns_without_inviting_retry(self):
        preview = lab.result_workers.BetaFixturePreview(
            operation=lab.result_workers.RESET,
            snapshot=SimpleNamespace(game_ids=(1, 2, 3), fingerprint='fingerprint'),
            user_ids=(10, 20),
            can_commit=True,
        )
        committed = lab.result_workers.BetaFixtureResult(
            operation=lab.result_workers.RESET,
            guild_id=300,
            user_ids=(10, 20),
            scenarios=(),
            old_game_ids=(1, 2, 3),
            new_game_ids=(4, 5, 6),
        )
        with mock.patch.object(lab, '_validate', return_value=300), \
                mock.patch.object(lab.settings, 'owner_id', 10), \
                mock.patch.object(
                    lab.result_workers, 'run_preview',
                    new=mock.AsyncMock(return_value=preview),
                ), mock.patch.object(
                    lab.result_workers, 'run_commit',
                    new=mock.AsyncMock(return_value=committed),
                ), mock.patch.object(
                    lab, 'run_status',
                    new=mock.AsyncMock(side_effect=RuntimeError('reload failed')),
                ):
            result = await lab.refresh_results(guild_id=300, actor='operator')
        self.assertTrue(result.committed)
        self.assertIsNone(result.status)
        self.assertIn('Do not retry', result.warning)


class BetaLabDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guide = guide_module.parse_checklist(
            '# 🧪 WHAT TO TEST\n\n'
            'Short guidance.\n\n'
            '## Games\n\n- Run /game show.\n- Run /game win.\n\n'
            '## Teams\n\n- Run /team show.'
        )
        self.status = lab.BetaLabStatus(
            guild_id=300,
            overall='ready',
            packs=(pack(lab.STRUCTURE), pack(lab.LEADERBOARD), pack(lab.RESULTS)),
            result_snapshot=None,
        )

    def test_overview_is_compact_and_sectioned(self):
        view = dashboard.BetaTestingDashboard(
            requester_id=10,
            status=self.status,
            guide=self.guide,
        )
        text = dashboard.overview_markdown(self.status)
        self.assertIn('Quick release pass', text)
        self.assertLess(len(text), 1900)
        selects = [
            item for item in view.walk_children()
            if item.__class__.__name__.endswith('Select')
        ]
        self.assertEqual(len(selects), 1)
        self.assertEqual([item.label for item in selects[0].options], ['Games', 'Teams'])

    def test_section_numbering_accounts_for_character_limited_pages(self):
        guide = guide_module.parse_checklist(
            '# Guide\n\n## Games\n\n'
            '- ' + ('A' * 1700) + '\n'
            '- ' + ('B' * 1700) + '\n'
            '- Third item.\n'
        )
        view = dashboard.BetaTestingDashboard(
            requester_id=10,
            status=self.status,
            guide=guide,
        )
        view.section_key = 'games'
        view.page = 1
        body = view._body()
        self.assertIn('2. ', body)
        self.assertNotIn('6. ', body)

    def test_tracked_sections_fit_components_text_display(self):
        guide = guide_module.load_guide()
        view = dashboard.BetaTestingDashboard(
            requester_id=10,
            status=self.status,
            guide=guide,
        )
        for section in guide.sections:
            view.section_key = section.key
            for page_number in range(len(guide_module.item_pages(section))):
                view.page = page_number
                self.assertLessEqual(len(view._body()), 4000)

    def test_overview_shows_participant_names_before_ids(self):
        scenario = lab.result_workers.BetaFixtureScenario('ready', 41, 'ready')
        participant = lab.result_workers.BetaFixtureParticipant(
            user_id=10,
            display_name='Nelluk',
        )
        snapshot = lab.result_workers.BetaFixtureSnapshot(
            guild_id=300,
            user_ids=(10,),
            scenarios=(scenario,),
            game_ids=(41,),
            readiness='ready',
            detail='Ready.',
            resettable=True,
            fingerprint='fingerprint',
            participants=(participant,),
        )
        status = lab.BetaLabStatus(
            guild_id=300,
            overall='ready',
            packs=self.status.packs,
            result_snapshot=snapshot,
        )
        text = dashboard.overview_markdown(status)
        self.assertIn('Participants: Nelluk (`10`)', text)

    async def test_dashboard_is_requester_bound_and_expiry_disables_controls(self):
        view = dashboard.BetaTestingDashboard(
            requester_id=10,
            status=self.status,
            guide=self.guide,
        )
        intruder = SimpleNamespace(
            user=SimpleNamespace(id=11),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(intruder))
        intruder.response.send_message.assert_awaited_once_with(
            'Open `/whattotest` for your own private Beta Lab dashboard.',
            ephemeral=True,
        )

        view.message = SimpleNamespace(edit=mock.AsyncMock())
        await view.on_timeout()
        self.assertTrue(view.expired)
        self.assertTrue(all(item.disabled for item in view.walk_children() if hasattr(item, 'disabled')))


if __name__ == '__main__':
    unittest.main()
