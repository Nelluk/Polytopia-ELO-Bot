"""Tier-3 cross-family coverage for the P10.10 mutation coordinator."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest import mock

import bot as bot_module
from modules import administration
from modules import components_v2
from modules import operator_guild_command_capabilities as command_capabilities
from modules import operator_guild_configuration_draft_workers as draft_workers
from modules import operator_guild_enrollment_workers as enrollment_workers
from modules import operator_guild_lifecycle_workers as lifecycle_workers
from modules import operator_guild_mutation_coordinator as coordinator_module
import settings


GUILD_A = 478571892832206869
GUILD_B = GUILD_A + 101
OWNER_ID = int(settings.owner_id)


def interaction(guild_id: int = GUILD_A, requester_id: int = OWNER_ID):
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=requester_id),
    )


class CoordinatorOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def test_normal_restart_inventory_reports_active_guild_mutation(self):
        active = coordinator_module.ActiveGuildMutation(
            operation='guild suspension', guild_id=GUILD_A,
            requester_id=OWNER_ID, started_monotonic=1.0,
        )
        with mock.patch.object(
            coordinator_module.guild_mutation_coordinator,
            '_active',
            active,
        ), mock.patch.object(
            administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(active_job=None),
        ), mock.patch.object(
            administration.game_open_workers,
            'pending_game_coordinator',
            SimpleNamespace(active_count=0),
        ), mock.patch.object(
            administration.operator_channel_purge_service,
            'manual_purge_coordinator',
            SimpleNamespace(active_guilds=frozenset()),
        ):
            result = administration.current_restart_activity()
        self.assertEqual(
            result.descriptions,
            (f'guild-configuration mutation `guild suspension` for guild {GUILD_A}',),
        )

    async def test_cross_family_activation_rejects_suspension_before_worker(self):
        coordinator = coordinator_module.GuildMutationCoordinator()
        cog = administration.administration.__new__(administration.administration)
        started = asyncio.Event()
        release = asyncio.Event()
        state = {'database': {}, 'runtime': {}, 'remote': {}}

        async def activation(*_args, **_kwargs):
            state['database'][GUILD_A] = 2
            started.set()
            await release.wait()
            state['runtime'][GUILD_A] = 2
            state['remote'][GUILD_A] = 2
            return mock.sentinel.activation

        async def suspend(*_args, **_kwargs):
            state['database'][GUILD_B] = 2
            state['runtime'][GUILD_B] = 2
            state['remote'][GUILD_B] = 2
            return mock.sentinel.lifecycle

        lifecycle = mock.AsyncMock(side_effect=suspend)
        with mock.patch.object(
            coordinator_module, 'guild_mutation_coordinator', coordinator,
        ), mock.patch.object(
            cog, '_operator_guild_draft_operation_uncoordinated',
            side_effect=activation,
        ), mock.patch.object(
            cog, '_operator_guild_lifecycle_operation_uncoordinated', lifecycle,
        ):
            first = asyncio.create_task(cog._operator_guild_draft_operation(
                interaction(GUILD_A),
                draft_workers.ACTIVATE,
                target_guild_id=GUILD_A,
            ))
            await started.wait()
            with self.assertRaisesRegex(
                lifecycle_workers.OperatorGuildLifecycleConflict,
                'No new database or Discord write was started',
            ):
                await cog._operator_guild_lifecycle_operation(
                    interaction(GUILD_B),
                    target_guild_id=GUILD_B,
                    action=lifecycle_workers.SUSPEND,
                    operation=lifecycle_workers.COMMIT,
                )
            lifecycle.assert_not_awaited()
            release.set()
            self.assertIs(await first, mock.sentinel.activation)

            self.assertIs(
                await cog._operator_guild_lifecycle_operation(
                    interaction(GUILD_B),
                    target_guild_id=GUILD_B,
                    action=lifecycle_workers.SUSPEND,
                    operation=lifecycle_workers.COMMIT,
                ),
                mock.sentinel.lifecycle,
            )
            lifecycle.assert_awaited_once()
            self.assertEqual(
                state['database'],
                state['runtime'],
            )
            self.assertEqual(
                state['database'],
                state['remote'],
            )

    async def test_capability_claim_rejects_unrelated_enrollment_before_worker(self):
        coordinator = coordinator_module.GuildMutationCoordinator()
        cog = administration.administration.__new__(administration.administration)
        started = asyncio.Event()
        release = asyncio.Event()

        async def capability(*_args, **_kwargs):
            started.set()
            await release.wait()
            return mock.sentinel.capability

        enrollment = mock.AsyncMock(return_value=mock.sentinel.enrollment)
        plan = SimpleNamespace(
            mode=command_capabilities.ACTIVATE,
            guild_id=GUILD_A,
            confirmation='SYNC EXACT TREE',
        )
        with mock.patch.object(
            coordinator_module, 'guild_mutation_coordinator', coordinator,
        ), mock.patch.object(
            cog, '_operator_guild_command_commit_uncoordinated',
            side_effect=capability,
        ), mock.patch.object(
            cog, '_operator_guild_enrollment_operation_uncoordinated', enrollment,
        ):
            first = asyncio.create_task(cog._operator_guild_command_commit(
                interaction(GUILD_A), plan, plan.confirmation,
            ))
            await started.wait()
            with self.assertRaisesRegex(
                enrollment_workers.OperatorGuildEnrollmentConflict,
                'No new database or Discord write was started',
            ):
                await cog._operator_guild_enrollment_operation(
                    interaction(GUILD_B),
                    target_guild_id=GUILD_B,
                    template=enrollment_workers.BASIC_PREFIX_TEMPLATE,
                    operation=enrollment_workers.COMMIT,
                )
            enrollment.assert_not_awaited()
            release.set()
            self.assertIs(await first, mock.sentinel.capability)

    async def test_reentrant_inner_claim_retains_one_owner(self):
        coordinator = coordinator_module.GuildMutationCoordinator()
        events = []

        async def inner():
            events.append(('inner', coordinator.active.operation))
            return 7

        async def outer():
            events.append(('outer', coordinator.active.operation))
            return await coordinator.run(
                operation='nested publication', guild_id=GUILD_A,
                requester_id=OWNER_ID, runner=inner,
            )

        result = await coordinator.run(
            operation='complete capability activation', guild_id=GUILD_A,
            requester_id=OWNER_ID, runner=outer,
        )
        self.assertEqual(result, 7)
        self.assertEqual(events, [
            ('outer', 'complete capability activation'),
            ('inner', 'complete capability activation'),
        ])
        self.assertIsNone(coordinator.active)

    async def test_repeated_cancellation_holds_claim_through_owned_drain(self):
        coordinator = coordinator_module.GuildMutationCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()
        completed = False

        async def owned_operation():
            nonlocal completed
            started.set()
            await release.wait()
            completed = True

        owner = asyncio.create_task(coordinator.run(
            operation='guild suspension', guild_id=GUILD_A,
            requester_id=OWNER_ID, runner=owned_operation,
        ))
        await started.wait()
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        self.assertIsNotNone(coordinator.active)
        with self.assertRaises(coordinator_module.GuildMutationConflict):
            await coordinator.run(
                operation='guild enrollment', guild_id=GUILD_B,
                requester_id=OWNER_ID,
                runner=lambda: asyncio.sleep(0),
            )
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertTrue(completed)
        self.assertIsNone(coordinator.active)


class FailClosedDispatchTests(unittest.IsolatedAsyncioTestCase):
    def test_quarantine_install_failure_blocks_all_ordinary_commands(self):
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.settings,
            'quarantine_database_guild_configuration',
            side_effect=RuntimeError('latch unavailable'),
        ), mock.patch.object(
            administration.settings,
            'maintenance_mode',
            False,
        ):
            with self.assertLogs(
                'polybot.modules.administration', level='CRITICAL',
            ):
                cog._quarantine_committed_guild(GUILD_A)
            self.assertTrue(administration.settings.maintenance_mode)

    async def test_publication_uncertainty_quarantines_target(self):
        cog = administration.administration.__new__(administration.administration)
        transition = SimpleNamespace(
            guild_id=GUILD_B,
            action=lifecycle_workers.SUSPEND,
            generation=2,
        )
        with mock.patch.object(
            cog,
            '_operator_guild_lifecycle_operation_uncoordinated',
            mock.AsyncMock(
                side_effect=lifecycle_workers.OperatorGuildLifecycleCommitted(
                    transition
                )
            ),
        ), mock.patch.object(
            administration.settings,
            'quarantine_database_guild_configuration',
        ) as quarantine:
            with self.assertRaises(lifecycle_workers.OperatorGuildLifecycleCommitted):
                await cog._operator_guild_lifecycle_operation(
                    interaction(GUILD_A),
                    target_guild_id=GUILD_B,
                    action=lifecycle_workers.SUSPEND,
                    operation=lifecycle_workers.COMMIT,
                )
        quarantine.assert_called_once_with(GUILD_B)

    async def test_capability_command_uncertainty_quarantines_target(self):
        cog = administration.administration.__new__(administration.administration)
        plan = SimpleNamespace(
            mode=command_capabilities.ACTIVATE,
            guild_id=GUILD_B,
            confirmation='ACTIVATE COMMANDS exact',
        )
        with mock.patch.object(
            cog,
            '_operator_guild_command_commit_uncoordinated',
            mock.AsyncMock(
                side_effect=command_capabilities.OperatorGuildCommandCapabilityCommitted(
                    revision=2,
                    generation=3,
                    detail='remote verification failed',
                )
            ),
        ), mock.patch.object(
            administration.settings,
            'quarantine_database_guild_configuration',
        ) as quarantine:
            with self.assertRaises(
                command_capabilities.OperatorGuildCommandCapabilityCommitted
            ):
                await cog._operator_guild_command_commit(
                    interaction(GUILD_A), plan, plan.confirmation,
                )
        quarantine.assert_called_once_with(GUILD_B)

    async def test_lifecycle_command_uncertainty_quarantines_target(self):
        cog = administration.administration.__new__(administration.administration)
        transition = SimpleNamespace(
            guild_id=GUILD_B,
            action=lifecycle_workers.SUSPEND,
            enrollment_state='suspended',
            generation=2,
        )
        preview = SimpleNamespace(
            action=lifecycle_workers.SUSPEND,
            guild_id=GUILD_B,
            confirmation=lambda _digest: 'SUSPEND GUILD exact',
        )
        plan = SimpleNamespace(plan_digest='a' * 64)
        with mock.patch.object(
            cog,
            '_operator_guild_lifecycle_commit_uncoordinated',
            mock.AsyncMock(
                side_effect=lifecycle_workers.OperatorGuildLifecycleCommandUnverified(
                    transition,
                    'remote verification failed',
                )
            ),
        ), mock.patch.object(
            administration.settings,
            'quarantine_database_guild_configuration',
        ) as quarantine:
            with self.assertRaises(
                lifecycle_workers.OperatorGuildLifecycleCommandUnverified
            ):
                await cog._operator_guild_lifecycle_commit(
                    interaction(GUILD_A),
                    preview,
                    plan,
                    'SUSPEND GUILD exact',
                )
        quarantine.assert_called_once_with(GUILD_B)

    async def test_quarantine_denies_commands_but_keeps_exact_owner_restart(self):
        def response():
            return SimpleNamespace(
                is_done=lambda: False,
                send_message=mock.AsyncMock(),
            )

        ordinary_response = response()
        ordinary = SimpleNamespace(
            guild_id=GUILD_A,
            user=SimpleNamespace(id=OWNER_ID),
            data={
                'name': 'operator', 'options': [{
                    'name': 'guild', 'type': 2,
                    'options': [{'name': 'list', 'type': 1}],
                }],
            },
            response=ordinary_response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        restart = SimpleNamespace(
            guild_id=GUILD_A,
            user=SimpleNamespace(id=OWNER_ID),
            data={
                'name': 'operator', 'options': [{
                    'name': 'bot', 'type': 2,
                    'options': [{'name': 'restart', 'type': 1}],
                }],
            },
            response=response(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        impostor = SimpleNamespace(
            **{**restart.__dict__, 'user': SimpleNamespace(id=OWNER_ID + 1),
               'response': response()},
        )
        with mock.patch.object(settings, 'guild_configuration_ready', return_value=True), \
                mock.patch.object(
                    settings, 'database_guild_configuration_quarantined',
                    return_value=True,
                ), mock.patch.object(settings, 'maintenance_mode', True), \
                mock.patch.object(settings, 'config', {}):
            self.assertFalse(await bot_module.PolyBotCommandTree.interaction_check(
                None, ordinary,
            ))
            self.assertTrue(await bot_module.PolyBotCommandTree.interaction_check(
                None, restart,
            ))
            self.assertFalse(await bot_module.PolyBotCommandTree.interaction_check(
                None, impostor,
            ))
        self.assertIn(
            'temporarily quarantined',
            ordinary_response.send_message.await_args.args[0],
        )

    async def test_owner_restart_does_not_broaden_to_unknown_guild(self):
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=mock.AsyncMock(),
        )
        restart = SimpleNamespace(
            guild_id=GUILD_B,
            user=SimpleNamespace(id=OWNER_ID),
            data={
                'name': 'operator', 'options': [{
                    'name': 'bot', 'type': 2,
                    'options': [{'name': 'restart', 'type': 1}],
                }],
            },
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(
            settings, 'guild_configuration_ready', return_value=True,
        ), mock.patch.object(
            settings, 'database_guild_configuration_quarantined',
            return_value=False,
        ), mock.patch.object(settings, 'config', {}):
            self.assertFalse(await bot_module.PolyBotCommandTree.interaction_check(
                None, restart,
            ))
        self.assertIn('not active', response.send_message.await_args.args[0])

    async def test_quarantine_blocks_prefix_and_listener_dispatch(self):
        message = SimpleNamespace(
            guild=SimpleNamespace(id=GUILD_A, name='Quarantined Guild'),
            author=SimpleNamespace(name='tester'),
        )
        instance = bot_module.MyBot()
        try:
            with mock.patch.object(
                settings, 'guild_configuration_ready', return_value=True,
            ), mock.patch.object(
                settings, 'guild_configuration_allows_dispatch', return_value=False,
            ), mock.patch.object(
                settings, 'guild_setting',
            ) as guild_setting, mock.patch.object(
                bot_module.commands.Bot, 'dispatch', autospec=True,
            ) as parent_dispatch:
                self.assertEqual(bot_module.get_prefix(instance, message), 'fakeprefix')
                instance.dispatch('member_update', message)
        finally:
            await instance.close()
        guild_setting.assert_not_called()
        parent_dispatch.assert_not_called()

    async def test_quarantine_blocks_shared_modern_component_workspace(self):
        view = components_v2.RequesterLayoutView(requester_id=OWNER_ID)
        response = SimpleNamespace(send_message=mock.AsyncMock())
        target = SimpleNamespace(
            guild_id=GUILD_A,
            user=SimpleNamespace(id=OWNER_ID),
            response=response,
        )
        with mock.patch.object(
            settings,
            'database_guild_configuration_quarantined',
            return_value=True,
        ):
            self.assertFalse(await view.authorize(target))
        response.send_message.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
