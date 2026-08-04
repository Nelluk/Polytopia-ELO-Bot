"""Offline safety tests for the P6.2 additive migration path."""

import unittest

from tests.test_newgame_worker import import_offline_runtime


migration = import_offline_runtime('modules.player_timezone_migration')


class MigrationPlanTests(unittest.TestCase):
    def test_plan_is_additive_idempotent_and_has_expected_schema(self):
        plan = migration.plan_migration({})
        self.assertEqual(
            plan.added_columns,
            (
                migration.MINUTES_COLUMN,
                migration.CLEARED_COLUMN,
            ),
        )
        self.assertIn('SMALLINT NULL', plan.statements[0])
        self.assertIn('BOOLEAN NOT NULL DEFAULT FALSE', plan.statements[1])
        self.assertEqual(
            plan.rollback_statements,
            (
                'ALTER TABLE "discordmember" DROP COLUMN '
                '"timezone_offset_cleared"',
                'ALTER TABLE "discordmember" DROP COLUMN '
                '"timezone_offset_minutes"',
            ),
        )

        already = migration.plan_migration({
            migration.MINUTES_COLUMN: migration.ColumnState(
                name=migration.MINUTES_COLUMN,
                data_type='smallint',
                is_nullable='YES',
            ),
            migration.CLEARED_COLUMN: migration.ColumnState(
                name=migration.CLEARED_COLUMN,
                data_type='boolean',
                is_nullable='NO',
                column_default='false',
            ),
        })
        self.assertTrue(already.already_applied)
        self.assertEqual(already.statements, ())

    def test_plan_fails_closed_on_missing_table_or_wrong_type(self):
        with self.assertRaises(migration.MigrationSafetyError):
            migration.plan_migration({}, table_exists=False)
        with self.assertRaises(migration.MigrationSafetyError):
            migration.plan_migration({
                migration.MINUTES_COLUMN: {
                    'data_type': 'integer',
                    'is_nullable': 'YES',
                },
            })
        with self.assertRaises(migration.MigrationSafetyError):
            migration.plan_migration({
                migration.CLEARED_COLUMN: {
                    'data_type': 'boolean',
                    'is_nullable': 'YES',
                },
            })

    def test_target_identity_and_development_apply_gate_are_fail_closed(self):
        production = migration.MigrationTarget(
            environment='production',
            database_name='polytopia2',
            database_user='polybot',
        )
        with self.assertRaises(migration.MigrationSafetyError):
            migration.validate_target_identity(
                production,
                actual_database='polytopia_dev',
                actual_user='polybot_dev',
            )
        development = migration.MigrationTarget(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
        )
        with self.assertRaises(migration.MigrationSafetyError):
            migration.validate_apply_target(development)


class FakeCursor:
    def __init__(self, *, fail_statement=False):
        self.statements = []
        self.fail_statement = fail_statement
        self._fetchone = None
        self._fetchall = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if statement.startswith('SELECT current_database'):
            self._fetchone = ('polytopia2', 'polybot')
        elif 'information_schema.tables' in statement:
            self._fetchone = (True,)
        elif 'information_schema.columns' in statement:
            self._fetchall = []
        elif self.fail_statement and statement.startswith('ALTER TABLE'):
            raise RuntimeError('simulated DDL failure')

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class FakeConnection:
    def __init__(self, *, fail_statement=False):
        self.cursor_value = FakeCursor(fail_statement=fail_statement)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class MigrationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.target = migration.MigrationTarget(
            environment='production',
            database_name='polytopia2',
            database_user='polybot',
        )

    def test_apply_is_transactional_and_uses_expected_identity(self):
        connection = FakeConnection()
        plan = migration.apply_migration(
            connection,
            target=self.target,
            confirmation=migration.ADD_CONFIRMATION,
        )
        ddl = [statement for statement, _ in connection.cursor_value.statements]
        self.assertEqual(plan.added_columns, (
            migration.MINUTES_COLUMN,
            migration.CLEARED_COLUMN,
        ))
        self.assertEqual(
            [statement for statement in ddl if statement.startswith('ALTER')],
            list(plan.statements),
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)

    def test_apply_rolls_back_on_ddl_failure_without_partial_success(self):
        connection = FakeConnection(fail_statement=True)
        with self.assertRaises(RuntimeError):
            migration.apply_migration(
                connection,
                target=self.target,
                confirmation=migration.ADD_CONFIRMATION,
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_rollback_is_reverse_order_and_transactional(self):
        connection = FakeConnection()
        plan = migration.rollback_migration(
            connection,
            target=self.target,
            confirmation=migration.ROLLBACK_CONFIRMATION,
            owned_columns=(
                migration.MINUTES_COLUMN,
                migration.CLEARED_COLUMN,
            ),
        )
        ddl = [statement for statement, _ in connection.cursor_value.statements]
        self.assertEqual(
            [statement for statement in ddl if statement.startswith('ALTER')],
            list(plan.statements),
        )
        self.assertEqual(plan.statements[0].split('"')[3], migration.CLEARED_COLUMN)
        self.assertEqual(connection.commits, 1)


if __name__ == '__main__':
    unittest.main()
