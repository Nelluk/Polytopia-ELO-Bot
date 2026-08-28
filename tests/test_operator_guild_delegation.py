"""Focused Tier-3 coverage for P10.9 owner delegation controls."""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

from modules import administration
from modules import guild_configuration_delegation_storage as storage
from modules import operator_guild_delegation as service
from modules import operator_guild_delegation_views as views
from modules import operator_guild_delegation_workers as workers
import settings
from tests import test_guild_configuration_storage as fixtures


GUILD_ID = fixtures.GUILD_ID
OWNER_ID = int(settings.owner_id)
NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


def profile():
    return SimpleNamespace(
        environment='development', database_name='polytopia_dev',
        database_user='polybot_dev', database_password='secret',
        database_host='localhost', database_port=5432,
        expected_bot_id=storage.storage.DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False, api_enabled=False, bullet_enabled=False,
        guild_configuration_source='database',
    )


def evidence(*, managed=False, everyone=False):
    return (
        workers.DiscordRoleEvidence(200, managed, everyone),
        workers.DiscordRoleEvidence(GUILD_ID, False, True),
    )


def request(operation=workers.SHOW, **kwargs):
    return workers.request_from_profile(
        profile=profile(), requester_id=OWNER_ID, guild_id=GUILD_ID,
        operation=operation, role_evidence=evidence(),
        runtime_guild_ids=(GUILD_ID,), **kwargs,
    )


def policy(*, version=1, roles=(200,), activation=False):
    return storage.GuildConfigurationDelegation(
        guild_id=GUILD_ID, policy_version=version,
        manager_role_ids=roles, allow_activation=activation,
        actor=f'discord:{OWNER_ID}', created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )


class Cursor:
    def __init__(self):
        self.readonly = True
        self.row = None
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement == 'SHOW transaction_read_only':
            self.row = ('on' if self.readonly else 'off',)

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.cursor_value = Cursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def set_session(self, **kwargs):
        self.cursor_value.readonly = kwargs['readonly']

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class RequestAndWorkerTests(unittest.TestCase):
    def test_owner_and_live_role_evidence_are_checked_before_connection(self):
        value = request()
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect') as connect:
            with self.assertRaisesRegex(
                workers.OperatorGuildDelegationPermissionError, 'bot owner',
            ):
                workers.execute_delegation(
                    workers.GuildDelegationRequest(
                        **{**value.__dict__, 'requester_id': OWNER_ID + 1}
                    )
                )
        connect.assert_not_called()

        digest = storage.policy_digest(
            guild_id=GUILD_ID, expected_version=None,
            manager_role_ids=(200,), allow_activation=False,
        )
        with self.assertRaisesRegex(
            workers.OperatorGuildDelegationValidationError, 'managed roles',
        ):
            workers.request_from_profile(
                profile=profile(), requester_id=OWNER_ID, guild_id=GUILD_ID,
                operation=workers.APPLY,
                role_evidence=evidence(managed=True),
                runtime_guild_ids=(GUILD_ID,), expected_policy_version=None,
                manager_role_ids=(200,), allow_activation=False,
                expected_plan_digest=digest,
                confirmation_text=f'DELEGATE {GUILD_ID} {digest}',
            )

    def test_show_is_read_only_and_returns_digest_bound_to_current_version(self):
        connection = Connection()
        current = policy()
        with mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(workers.delegation, '_validate_live_connection'), \
                mock.patch.object(
                    workers.delegation, 'inspect_delegation_schema',
                    return_value=mock.sentinel.inventory,
                ), mock.patch.object(
                    workers.delegation, 'validate_delegation_schema', return_value=True,
                ), mock.patch.object(
                    workers.delegation, 'select_delegation', return_value=current,
                ):
            result = workers.execute_delegation(request())
        self.assertIs(result.policy, current)
        self.assertFalse(result.committed)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)
        self.assertEqual(result.confirmation, f'DELEGATE {GUILD_ID} {result.plan_digest}')

    def test_apply_rechecks_version_digest_confirmation_and_commits_one_policy(self):
        current = policy()
        desired = policy(version=2, activation=True)
        digest = storage.policy_digest(
            guild_id=GUILD_ID, expected_version=1,
            manager_role_ids=(200,), allow_activation=True,
        )
        value = request(
            workers.APPLY, expected_policy_version=1,
            manager_role_ids=(200,), allow_activation=True,
            expected_plan_digest=digest,
            confirmation_text=f'DELEGATE {GUILD_ID} {digest}',
        )
        connection = Connection()
        with mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(workers.delegation, '_validate_live_connection'), \
                mock.patch.object(
                    workers.delegation, 'inspect_delegation_schema',
                    return_value=mock.sentinel.inventory,
                ), mock.patch.object(
                    workers.delegation, 'validate_delegation_schema', return_value=True,
                ), mock.patch.object(
                    workers.delegation, 'select_delegation', return_value=current,
                ), mock.patch.object(
                    workers.delegation, 'put_delegation', return_value=desired,
                ) as put:
            result = workers.execute_delegation(value)
        self.assertTrue(result.committed)
        self.assertIs(result.policy, desired)
        self.assertEqual(connection.commits, 1)
        put.assert_called_once()

    def test_apply_rejects_noop_without_policy_or_audit_write(self):
        current = policy()
        digest = storage.policy_digest(
            guild_id=GUILD_ID, expected_version=1,
            manager_role_ids=(200,), allow_activation=False,
        )
        value = request(
            workers.APPLY, expected_policy_version=1,
            manager_role_ids=(200,), allow_activation=False,
            expected_plan_digest=digest,
            confirmation_text=f'DELEGATE {GUILD_ID} {digest}',
        )
        connection = Connection()
        with mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(workers.delegation, '_validate_live_connection'), \
                mock.patch.object(
                    workers.delegation, 'inspect_delegation_schema',
                    return_value=mock.sentinel.inventory,
                ), mock.patch.object(
                    workers.delegation, 'validate_delegation_schema', return_value=True,
                ), mock.patch.object(
                    workers.delegation, 'select_delegation', return_value=current,
                ), mock.patch.object(workers.delegation, 'put_delegation') as put:
            with self.assertRaisesRegex(
                workers.OperatorGuildDelegationValidationError, 'unchanged',
            ):
                workers.execute_delegation(value)
        put.assert_not_called()
        self.assertEqual(connection.commits, 0)

    def test_policy_or_audit_failure_rolls_back_and_closes_connection(self):
        digest = storage.policy_digest(
            guild_id=GUILD_ID, expected_version=None,
            manager_role_ids=(200,), allow_activation=False,
        )
        value = request(
            workers.APPLY, expected_policy_version=None,
            manager_role_ids=(200,), allow_activation=False,
            expected_plan_digest=digest,
            confirmation_text=f'DELEGATE {GUILD_ID} {digest}',
        )
        connection = Connection()
        with mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(workers.delegation, '_validate_live_connection'), \
                mock.patch.object(
                    workers.delegation, 'inspect_delegation_schema',
                    return_value=mock.sentinel.inventory,
                ), mock.patch.object(
                    workers.delegation, 'validate_delegation_schema', return_value=True,
                ), mock.patch.object(
                    workers.delegation, 'select_delegation', return_value=None,
                ), mock.patch.object(
                    workers.delegation,
                    'put_delegation',
                    side_effect=storage.GuildConfigurationDelegationStorageError(
                        'audit failed'
                    ),
                ):
            with self.assertRaisesRegex(
                workers.OperatorGuildDelegationValidationError, 'audit failed',
            ):
                workers.execute_delegation(value)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_drains_worker_and_loop_stays_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            return mock.sentinel.result

        with mock.patch.object(workers, 'execute_delegation', slow):
            task = asyncio.create_task(workers.run_delegation(request()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        ticked = False

        def briefly_slow(_request):
            time.sleep(0.03)
            return mock.sentinel.result

        async def ticker():
            nonlocal ticked
            await asyncio.sleep(0.003)
            ticked = True

        with mock.patch.object(workers, 'execute_delegation', briefly_slow):
            result, _ = await asyncio.gather(
                workers.run_delegation(request()), ticker(),
            )
        self.assertIs(result, mock.sentinel.result)
        self.assertTrue(ticked)


class ServiceViewAndAdapterTests(unittest.TestCase):
    def test_service_freezes_every_role_and_marks_everyone(self):
        everyone = SimpleNamespace(
            id=GUILD_ID, managed=False, is_default=lambda: True,
        )
        manager = SimpleNamespace(id=200, managed=False, is_default=lambda: False)
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            user=SimpleNamespace(id=OWNER_ID),
            guild=SimpleNamespace(id=GUILD_ID, roles=(manager, everyone)),
        )
        with mock.patch.object(service.settings, 'runtime_profile', profile()), \
                mock.patch.object(
                    service.settings, 'database_guild_ids', return_value=(GUILD_ID,),
                ):
            value = service.build_request(
                interaction=interaction, operation=workers.SHOW,
            )
        self.assertEqual(tuple(item.role_id for item in value.role_evidence), (200, GUILD_ID))
        self.assertTrue(value.role_evidence[-1].everyone)

    def test_workspace_stages_without_write_and_uses_full_digest_confirmation(self):
        result = workers.GuildDelegationResult(
            workers.SHOW, GUILD_ID, policy(), 'a' * 64,
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildDelegationWorkspace(
            requester_id=OWNER_ID, result=result, runner=runner,
            role_names={200: 'Managers'},
        )
        self.assertRegex(workspace.confirmation, rf'^DELEGATE {GUILD_ID} [0-9a-f]{{64}}$')
        modal = views.DelegationConfirmationModal(workspace)
        self.assertEqual(modal.expected, workspace.confirmation)
        self.assertEqual(modal.confirmation.min_length, len(modal.expected))

    def test_operator_policy_command_and_public_delegated_entry_are_separate(self):
        roots = {
            command.name: command
            for command in administration.administration.__cog_app_commands__
        }
        self.assertIn('operator', roots)
        self.assertIn('guild', roots)
        self.assertTrue(roots['operator'].default_permissions.administrator)
        self.assertIsNone(roots['guild'].default_permissions)
        self.assertIsNotNone(roots['guild'].get_command('settings'))
        self.assertIsNone(roots['guild'].get_command('edit'))
        self.assertIsNotNone(
            roots['operator'].get_command('guild').get_command('delegation')
        )


if __name__ == '__main__':
    unittest.main()
