"""Focused offline coverage for supervised native bot restarts."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


service = import_offline_runtime('modules.operator_restart')
views = import_offline_runtime('modules.operator_restart_views')
administration = import_offline_runtime('modules.administration')
bot_module = import_offline_runtime('bot')


CHECKPOINT = '1' * 40
NEXT_CHECKPOINT = '2' * 40
SYSTEMD_ENV = {'INVOCATION_ID': 'a' * 32}
COMPOSE_ENV = {
    'POLYBOT_RESTART_SUPERVISOR': service.COMPOSE_SUPERVISOR,
}


def request(**overrides):
    values = dict(
        requester_id=10,
        requester_name='Operator',
        is_superuser=True,
        is_owner=True,
        force=False,
        confirmation_text=None,
    )
    values.update(overrides)
    return service.RestartRequest(**values)


def checkout():
    return service.RestartCheckoutSnapshot(CHECKPOINT, NEXT_CHECKPOINT)


def preview(**overrides):
    values = dict(
        requester_id=10,
        requester_name='Operator',
        force=False,
        checkout=checkout(),
        activity=service.RestartActivitySnapshot(),
    )
    values.update(overrides)
    return service.RestartPreview(**values)


class RestartBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_requests_and_snapshots_are_frozen_primitives(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.force = True
        self.assertNotIn('Interaction', repr(value))
        self.assertTrue(
            service.RestartActivitySnapshot(('ELO job',)).busy
        )
        self.assertNotIn('modules.models', inspect.getsource(service))
        self.assertNotIn('modules.models', inspect.getsource(views))

    def test_activity_snapshot_reports_each_known_shutdown_inhibitor(self):
        elo_job = SimpleNamespace(operation='recalculate', game_id=None)
        with mock.patch.object(
            administration.settings.elo_job_coordinator,
            '_active_job',
            elo_job,
        ), mock.patch.object(
            administration.game_open_workers,
            'pending_game_coordinator',
            SimpleNamespace(active_count=2),
        ), mock.patch.object(
            administration.operator_channel_purge_service,
            'manual_purge_coordinator',
            SimpleNamespace(active_guilds={1}),
        ):
            result = administration.current_restart_activity()
        self.assertEqual(len(result.descriptions), 3)
        self.assertTrue(result.busy)

    def test_permission_and_force_confirmation_fail_closed(self):
        with self.assertRaises(service.RestartPermissionError):
            service.assert_authorized(request(is_superuser=False))
        with self.assertRaises(service.RestartPermissionError):
            service.assert_authorized(request(force=True, is_owner=False))
        with self.assertRaises(service.RestartConfirmationError):
            service.assert_authorized(
                request(force=True, confirmation_text='restart now')
            )

    def test_unsupervised_process_is_refused(self):
        for environment in (
            {},
            {'INVOCATION_ID': 'not-systemd'},
            {
                'POLYBOT_RESTART_SUPERVISOR': 'unknown',
                'INVOCATION_ID': 'a' * 32,
            },
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(service.RestartSupervisionError):
                    service.assert_supervised(environment)
        service.assert_supervised(SYSTEMD_ENV)
        service.assert_supervised(COMPOSE_ENV)

    async def test_compose_restart_uses_current_image_without_git_metadata(self):
        with mock.patch.object(
            service,
            '_git_output',
            new=mock.AsyncMock(),
        ) as git_output:
            result = await service.inspect_checkout(
                Path('/app'),
                environ=COMPOSE_ENV,
            )
        git_output.assert_not_awaited()
        self.assertEqual(result.running_source, 'current container image')
        self.assertEqual(result.restart_source, 'current container image')
        self.assertEqual(result.supervisor, service.COMPOSE_SUPERVISOR)

    async def test_systemd_checkout_must_be_clean_and_returns_source_details(self):
        with mock.patch.object(
            service,
            '_git_output',
            new=mock.AsyncMock(side_effect=['', NEXT_CHECKPOINT]),
        ):
            result = await service.inspect_checkout(
                Path('/tmp'),
                environ=SYSTEMD_ENV,
            )
        self.assertEqual(result.running_source, 'current process')
        self.assertEqual(result.restart_source, NEXT_CHECKPOINT)

        with mock.patch.object(
            service,
            '_git_output',
            new=mock.AsyncMock(return_value=' M bot.py'),
        ):
            with self.assertRaises(service.RestartCheckoutError):
                await service.inspect_checkout(Path('/tmp'))

    async def test_checkout_inspection_keeps_event_loop_responsive(self):
        ticks = 0

        async def delayed_output(_root, *arguments):
            nonlocal ticks
            for _ in range(3):
                await asyncio.sleep(0)
                ticks += 1
            return '' if arguments[0] == 'status' else NEXT_CHECKPOINT

        with mock.patch.object(service, '_git_output', side_effect=delayed_output):
            result = await service.inspect_checkout(Path('/tmp'))
        self.assertEqual(result.restart_source, NEXT_CHECKPOINT)
        self.assertGreaterEqual(ticks, 6)

    async def test_cancelled_checkout_inspection_kills_and_reaps_git(self):
        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.communicating = asyncio.Event()
                self.killed = asyncio.Event()
                self.waiting = asyncio.Event()
                self.release_wait = asyncio.Event()

            async def communicate(self):
                self.communicating.set()
                await asyncio.Event().wait()

            def kill(self):
                self.returncode = -9
                self.killed.set()

            async def wait(self):
                self.waiting.set()
                await self.release_wait.wait()
                return self.returncode

        process = FakeProcess()
        with mock.patch.object(
            service.asyncio,
            'create_subprocess_exec',
            new=mock.AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(
                service._git_output(Path('/tmp'), 'status')
            )
            await asyncio.wait_for(process.communicating.wait(), 0.2)
            task.cancel()
            await asyncio.wait_for(process.killed.wait(), 0.2)
            await asyncio.wait_for(process.waiting.wait(), 0.2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            process.release_wait.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(process.returncode, -9)

    async def test_normal_preview_and_commit_reject_active_work(self):
        coordinator = service.RestartCoordinator()
        busy = service.RestartActivitySnapshot(('1 pending-game worker(s)',))
        with mock.patch.object(
            service,
            'inspect_checkout',
            new=mock.AsyncMock(return_value=checkout()),
        ):
            with self.assertRaises(service.RestartBusyError):
                await coordinator.preview(
                    request(),
                    project_root=Path('/tmp'),
                    activity_loader=lambda: busy,
                    environ=SYSTEMD_ENV,
                )
            with self.assertRaises(service.RestartBusyError):
                await coordinator.run(
                    request(),
                    project_root=Path('/tmp'),
                    activity_loader=lambda: busy,
                    shutdown=mock.AsyncMock(),
                    environ=SYSTEMD_ENV,
                )
        self.assertIsNone(coordinator.active)

    async def test_owner_force_bypasses_only_active_work(self):
        coordinator = service.RestartCoordinator()
        shutdown = mock.AsyncMock()
        busy = service.RestartActivitySnapshot(('manual channel purge',))
        forced = request(force=True, confirmation_text=service.FORCE_CONFIRMATION)
        with mock.patch.object(
            service,
            'inspect_checkout',
            new=mock.AsyncMock(return_value=checkout()),
        ):
            result = await coordinator.preview(
                request(force=True),
                project_root=Path('/tmp'),
                activity_loader=lambda: busy,
                environ=SYSTEMD_ENV,
            )
            await coordinator.run(
                forced,
                project_root=Path('/tmp'),
                activity_loader=lambda: busy,
                shutdown=shutdown,
                environ=SYSTEMD_ENV,
            )
        self.assertTrue(result.force)
        shutdown.assert_awaited_once_with(10, True)
        self.assertIsNone(coordinator.active)

    async def test_conflict_and_repeated_cancellation_retain_shutdown(self):
        coordinator = service.RestartCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def shutdown(_requester_id, _force):
            started.set()
            await release.wait()

        with mock.patch.object(
            service,
            'inspect_checkout',
            new=mock.AsyncMock(return_value=checkout()),
        ):
            task = asyncio.create_task(coordinator.run(
                request(),
                project_root=Path('/tmp'),
                activity_loader=service.RestartActivitySnapshot,
                shutdown=shutdown,
                environ=SYSTEMD_ENV,
            ))
            await asyncio.wait_for(started.wait(), 0.2)
            with self.assertRaises(service.RestartConflictError):
                await coordinator.run(
                    request(requester_id=11),
                    project_root=Path('/tmp'),
                    activity_loader=service.RestartActivitySnapshot,
                    shutdown=shutdown,
                    environ=SYSTEMD_ENV,
                )
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            self.assertFalse(task.done())
            self.assertIsNotNone(coordinator.active)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(coordinator.active)


class RestartViewTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, user_id=10):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=mock.AsyncMock(),
                send_modal=mock.AsyncMock(),
                defer=mock.AsyncMock(),
                edit_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )

    async def test_controls_are_requester_bound(self):
        view = views.RestartConfirmationView(
            preview=preview(), runner=mock.AsyncMock()
        )
        outsider = self._interaction(99)
        self.assertFalse(await view.authorize(outsider))
        outsider.response.send_message.assert_awaited_once()

    async def test_normal_button_runs_without_force_text(self):
        runner = mock.AsyncMock()
        view = views.RestartConfirmationView(
            preview=preview(), runner=runner
        )
        interaction = self._interaction()
        await view._confirm(interaction)
        runner.assert_awaited_once_with(interaction, None)
        interaction.response.defer.assert_awaited_once()

    async def test_force_button_requires_exact_text_modal(self):
        forced = preview(
            force=True,
            activity=service.RestartActivitySnapshot(('ELO job',)),
        )
        view = views.RestartConfirmationView(
            preview=forced, runner=mock.AsyncMock()
        )
        interaction = self._interaction()
        await view._confirm(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, views.ForceRestartModal)
        self.assertEqual(modal.confirmation.placeholder, service.FORCE_CONFIRMATION)
        self.assertIn('OWNER FORCE', str(view.to_components()))
        self.assertIn('will not wait', str(view.to_components()))

    async def test_compose_panel_says_current_container_image(self):
        runner = mock.AsyncMock()
        compose_preview = preview(checkout=service.RestartCheckoutSnapshot(
            CHECKPOINT,
            CHECKPOINT,
            service.COMPOSE_SUPERVISOR,
        ))
        view = views.RestartConfirmationView(
            preview=compose_preview,
            runner=runner,
        )
        panel = str(view.to_components())
        self.assertIn('Docker Compose (current container image)', panel)
        interaction = self._interaction()
        await view._confirm(interaction)
        self.assertIn('current container image', str(view.to_components()))


class RestartAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(
            administration.administration
        )
        self.cog.bot = SimpleNamespace(
            request_supervised_restart=mock.AsyncMock()
        )
        self.operator = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.command = self.operator.get_command('bot').get_command('restart')

    def _interaction(self, user_id=10):
        message = SimpleNamespace()
        return SimpleNamespace(
            guild_id=300,
            channel_id=400,
            user=SimpleNamespace(id=user_id, display_name='Operator'),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            original_response=mock.AsyncMock(return_value=message),
        )

    def test_exact_shape_and_all_prefix_names_retired(self):
        self.assertEqual(
            [(value.name, value.required) for value in self.command.parameters],
            [('force', False)],
        )
        prefix_names = {
            name
            for command in administration.administration.__cog_commands__
            for name in (command.name, *command.aliases)
        }
        self.assertTrue({'restart', 'restart_force', 'quit'}.isdisjoint(prefix_names))

    async def test_non_superuser_denial_is_private_and_builds_no_view(self):
        interaction = self._interaction(99)
        with mock.patch.object(
            administration.settings,
            'is_superuser',
            return_value=False,
        ), mock.patch.object(administration.settings, 'owner_id', 10):
            await self.command.callback(self.cog, interaction, False)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
        interaction.edit_original_response.assert_not_awaited()

    async def test_owner_force_builds_view_and_revalidates_component_actor(self):
        interaction = self._interaction(10)

        async def coordinate(sent_request, **kwargs):
            await kwargs['shutdown'](
                sent_request.requester_id,
                sent_request.force,
            )

        coordinator = SimpleNamespace(
            preview=mock.AsyncMock(return_value=preview(force=True)),
            run=mock.AsyncMock(side_effect=coordinate),
        )

        async def request_restart(_requester_id, _force, *, before_close):
            await before_close()

        self.cog.bot.request_supervised_restart.side_effect = request_restart
        with mock.patch.object(
            administration.operator_restart_service,
            'restart_coordinator',
            coordinator,
        ), mock.patch.object(
            administration.settings,
            'is_superuser',
            return_value=True,
        ), mock.patch.object(administration.settings, 'owner_id', 10):
            await self.command.callback(self.cog, interaction, True)
            created = interaction.edit_original_response.await_args.kwargs['view']
            component = self._interaction(10)
            await created.runner(component, service.FORCE_CONFIRMATION)
        sent = coordinator.run.await_args.args[0]
        self.assertTrue(sent.force)
        self.assertTrue(sent.is_owner)
        self.assertEqual(sent.confirmation_text, service.FORCE_CONFIRMATION)
        component.edit_original_response.assert_awaited_once_with(view=created)
        accepted_panel = str(created.to_components())
        self.assertIn('Restart accepted', accepted_panel)
        self.assertIn('may keep showing the bot as online', accepted_panel)
        self.assertIn('cannot update after the current process', accepted_panel)
        self.assertIn('10–20 seconds', accepted_panel)


class BotRestartExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_marks_maintenance_and_closes_before_exit_status(self):
        instance = bot_module.MyBot()
        events = []

        async def acknowledge():
            self.assertTrue(bot_module.settings.maintenance_mode)
            events.append('acknowledge')

        async def close():
            events.append('close')

        try:
            with mock.patch.object(
                bot_module.operator_restart,
                'assert_supervised',
            ), mock.patch.object(
                instance,
                'close',
                new=mock.AsyncMock(side_effect=close),
            ) as close_mock, mock.patch.object(
                bot_module.settings,
                'maintenance_mode',
                False,
            ), mock.patch.object(
                instance,
                '_cleanup_restart_messages',
                new=mock.AsyncMock(side_effect=lambda: events.append('cleanup')),
            ):
                await instance.request_supervised_restart(
                    10,
                    False,
                    before_close=acknowledge,
                )
                close_mock.assert_awaited_once()
                self.assertTrue(bot_module.settings.maintenance_mode)
                self.assertEqual(events, ['acknowledge', 'cleanup', 'close'])
                self.assertEqual(
                    instance.restart_exit_status,
                    service.RESTART_EXIT_STATUS,
                )
        finally:
            await bot_module.MyBot.close(instance)

    async def test_native_commands_receive_private_restart_denial(self):
        instance = bot_module.MyBot()
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        try:
            with mock.patch.object(
                bot_module.settings,
                'maintenance_mode',
                True,
            ), mock.patch.object(
                bot_module.settings,
                'guild_configuration_ready',
                return_value=True,
            ):
                allowed = await instance.tree.interaction_check(interaction)
            self.assertFalse(allowed)
            response.send_message.assert_awaited_once_with(
                'The bot is restarting. Try the command again in a moment.',
                ephemeral=True,
            )
        finally:
            await instance.close()

    def test_ordinary_entrypoint_exits_with_deliberate_restart_status(self):
        class FakeBot:
            restart_exit_status = service.RESTART_EXIT_STATUS

            def check(self, function):
                return function

            def event(self, function):
                return function

            def before_invoke(self, function):
                return function

            def run(self, _token):
                return None

        fake = FakeBot()
        with mock.patch.object(bot_module, 'main'), mock.patch.object(
            bot_module, 'MyBot', return_value=fake
        ):
            with self.assertRaises(SystemExit) as raised:
                bot_module.init_bot()
        self.assertEqual(raised.exception.code, service.RESTART_EXIT_STATUS)
        source = inspect.getsource(bot_module.init_bot)
        self.assertNotIn('systemctl', source)
        self.assertNotIn('sudo', source)


if __name__ == '__main__':
    unittest.main()
