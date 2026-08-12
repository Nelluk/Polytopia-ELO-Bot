"""Offline coverage for P10.9 delegation policy storage."""

from __future__ import annotations

import datetime
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import guild_configuration_delegation_storage as delegation
from scripts import manage_guild_configuration_delegation as script
from tests import test_guild_configuration_storage as fixtures


NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


def exact_inventory():
    return delegation.DelegationSchemaInventory(
        (delegation.DELEGATION_TABLE,),
        tuple(sorted(delegation.EXPECTED_COLUMNS)),
        delegation.EXPECTED_CONSTRAINTS,
    )


def row(*, version=1, roles=(200,), activation=False, actor='discord:1'):
    return (
        fixtures.GUILD_ID, version, delegation.DELEGATION_SCHEMA_VERSION,
        list(roles), activation, actor, NOW, NOW,
    )


class Cursor:
    def __init__(self, *, inventory=None, current=None):
        self.inventory = exact_inventory() if inventory is None else inventory
        self.current = current
        self.statements = []
        self.row = None
        self.rows = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement == 'SHOW transaction_read_only':
            self.row = ('off',)
        elif statement == 'SELECT current_database(), current_user':
            self.row = ('polytopia_dev', 'polybot_dev')
        elif 'information_schema.tables' in statement:
            self.rows = [(value,) for value in self.inventory.tables]
        elif 'information_schema.columns' in statement:
            self.rows = list(self.inventory.columns)
        elif 'FROM pg_constraint' in statement:
            self.rows = list(self.inventory.constraints)
        elif 'enrollment_state, active_revision, generation' in statement:
            self.row = ('active', 1, 1)
        elif statement.startswith(
                f'SELECT guild_id, policy_version'):
            self.row = self.current
        elif 'RETURNING guild_id, policy_version' in statement:
            version = 1 if self.current is None else self.current[1] + 1
            self.row = row(
                version=version,
                roles=tuple(parameters[2] and __import__('json').loads(parameters[2])),
                activation=parameters[3],
                actor=parameters[4],
            )
        elif 'MAX(event_number)' in statement:
            self.row = (1,)
        elif statement.startswith('SELECT document_digest'):
            self.row = ('a' * 64,)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, cursor=None):
        self.cursor_value = cursor or Cursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class SchemaTests(unittest.TestCase):
    def test_plan_is_additive_deterministic_and_connection_free(self):
        first = delegation.delegation_schema_plan(fixtures.target())
        self.assertEqual(first, delegation.delegation_schema_plan(fixtures.target()))
        self.assertRegex(first.confirmation, r'^P10\.9 APPLY [0-9a-f]{64}$')
        sql = '\n'.join(first.statements).upper()
        self.assertEqual(sql.count('CREATE TABLE'), 1)
        for forbidden in ('DROP ', 'ALTER ', 'DELETE FROM', 'TRUNCATE '):
            self.assertNotIn(forbidden, sql)
        self.assertFalse(delegation.plan_to_mapping(first)['database_connected'])

    def test_inventory_detects_absent_exact_and_drift(self):
        self.assertFalse(delegation.validate_delegation_schema(
            delegation.DelegationSchemaInventory((), (), ())
        ))
        self.assertTrue(delegation.validate_delegation_schema(exact_inventory()))
        with self.assertRaisesRegex(
            delegation.GuildConfigurationDelegationStorageError, 'columns',
        ):
            delegation.validate_delegation_schema(
                delegation.DelegationSchemaInventory(
                    (delegation.DELEGATION_TABLE,),
                    delegation.EXPECTED_COLUMNS[:-1],
                    delegation.EXPECTED_CONSTRAINTS,
                )
            )

    def test_apply_requires_exact_confirmation_before_cursor(self):
        connection = mock.Mock()
        plan = delegation.delegation_schema_plan(fixtures.target())
        with self.assertRaisesRegex(
            delegation.GuildConfigurationDelegationStorageError, 'confirmation',
        ):
            delegation.apply_delegation_schema(
                connection, target=fixtures.target(), plan=plan,
                confirmation='wrong',
            )
        connection.cursor.assert_not_called()

    def test_apply_failure_rolls_back(self):
        connection = Connection()
        plan = delegation.delegation_schema_plan(fixtures.target())
        with mock.patch.object(
            delegation, '_validate_live_connection',
            side_effect=RuntimeError('boom'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'boom'):
                delegation.apply_delegation_schema(
                    connection, target=fixtures.target(), plan=plan,
                    confirmation=plan.confirmation,
                )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)


class PolicyTests(unittest.TestCase):
    def test_role_ids_are_bounded_unique_positive_and_canonical(self):
        self.assertEqual(delegation.normalize_manager_role_ids((3, 1, 2)), (1, 2, 3))
        for invalid in (
            (1, 1), (0,), (True,), (1, '2'), tuple(range(1, 22)),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                delegation.GuildConfigurationDelegationStorageError
            ):
                delegation.normalize_manager_role_ids(invalid)

    def test_row_is_immutable_and_strict(self):
        value = delegation.delegation_from_row(row(roles=(300, 200)))
        self.assertEqual(value.manager_role_ids, (200, 300))
        self.assertTrue(value.enabled)
        with self.assertRaisesRegex(
            delegation.GuildConfigurationDelegationStorageError, 'metadata',
        ):
            delegation.delegation_from_row(
                (fixtures.GUILD_ID, 1, 2, [200], False, 'x', NOW, NOW)
            )

    def test_put_replaces_policy_appends_audit_without_generation_write(self):
        cursor = Cursor(current=row())
        result = delegation.put_delegation(
            cursor,
            guild_id=fixtures.GUILD_ID,
            expected_version=1,
            manager_role_ids=(300, 200),
            allow_activation=True,
            actor='discord:1',
        )
        self.assertEqual(result.policy_version, 2)
        self.assertEqual(result.manager_role_ids, (200, 300))
        sql = '\n'.join(value[0] for value in cursor.statements)
        self.assertIn(f'INSERT INTO "{delegation.storage.AUDIT_TABLE}"', sql)
        self.assertNotIn(
            f'UPDATE "{delegation.storage.REGISTRY_TABLE}"', sql
        )

    def test_stale_version_refuses_before_policy_or_audit_write(self):
        cursor = Cursor(current=row(version=2))
        with self.assertRaisesRegex(
            delegation.GuildConfigurationDelegationStorageError, 'changed',
        ):
            delegation.put_delegation(
                cursor,
                guild_id=fixtures.GUILD_ID,
                expected_version=1,
                manager_role_ids=(200,),
                allow_activation=False,
                actor='discord:1',
            )
        sql = '\n'.join(value[0] for value in cursor.statements)
        self.assertNotIn(f'INSERT INTO "{delegation.DELEGATION_TABLE}"', sql)
        self.assertNotIn(f'INSERT INTO "{delegation.storage.AUDIT_TABLE}"', sql)


class ScriptTests(unittest.TestCase):
    def profile(self):
        return SimpleNamespace(
            environment='development', database_name='polytopia_dev',
            database_user='polybot_dev', database_password='secret',
            database_host='localhost', database_port=5432,
            expected_bot_id=delegation.storage.DEVELOPMENT_BETA_APPLICATION_ID,
            background_tasks_enabled=False, api_enabled=False,
            bullet_enabled=False,
        )

    def test_plan_never_connects(self):
        emitted = []
        with mock.patch.dict(os.environ, {'POLYBOT_ENV': 'development'}, clear=True), \
                mock.patch.object(script, '_profile', return_value=self.profile()), \
                mock.patch.object(script, '_connection') as connection, \
                mock.patch.object(script, '_emit', side_effect=emitted.append):
            self.assertEqual(script.main(['plan']), 0)
        connection.assert_not_called()
        self.assertFalse(emitted[0]['database_connected'])

    def test_apply_refuses_wrong_environment_before_profile_or_connection(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(script, 'load_runtime_profile') as profile, \
                mock.patch.object(script, '_connection') as connection:
            self.assertEqual(script.main(['apply', '--confirm', 'wrong']), 2)
        profile.assert_not_called()
        connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
