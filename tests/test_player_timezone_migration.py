"""Offline safety tests for the P6.2 development-only migration path."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import os
from pathlib import Path
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


migration = import_offline_runtime('modules.player_timezone_migration')

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / 'scripts' /
    'migrate_player_timezone.py'
)
_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    'test_migrate_player_timezone_script',
    _SCRIPT_PATH,
)
migration_script = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(migration_script)


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

    def test_only_exact_development_target_and_acknowledgement_are_allowed(self):
        production = migration.MigrationTarget(
            environment='production',
            database_name='polytopia2',
            database_user='polybot',
        )
        with self.assertRaises(migration.MigrationSafetyError):
            migration.validate_target_identity(
                production,
                actual_database='polytopia2',
                actual_user='polybot',
            )
        development = migration.MigrationTarget(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
        )
        migration.validate_target_identity(
            development,
            actual_database='polytopia_dev',
            actual_user='polybot_dev',
        )
        migration.validate_apply_target(development)
        migration.validate_apply_confirmation(
            migration.DEVELOPMENT_APPLY_CONFIRMATION,
        )
        with self.assertRaises(migration.MigrationSafetyError):
            migration.validate_apply_confirmation('')
        for invalid in (
            migration.MigrationTarget(
                environment='development',
                database_name='polytopia2',
                database_user='polybot_dev',
            ),
            migration.MigrationTarget(
                environment='development',
                database_name='polytopia_dev',
                database_user='polybot',
            ),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(migration.MigrationSafetyError):
                    migration.validate_apply_target(invalid)


class FakeCursor:
    def __init__(
        self,
        *,
        fail_statement=False,
        database='polytopia_dev',
        user='polybot_dev',
    ):
        self.statements = []
        self.fail_statement = fail_statement
        self.database = database
        self.user = user
        self._fetchone = None
        self._fetchall = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if statement.startswith('SELECT current_database'):
            self._fetchone = (self.database, self.user)
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
    def __init__(
        self,
        *,
        fail_statement=False,
        database='polytopia_dev',
        user='polybot_dev',
    ):
        self.cursor_value = FakeCursor(
            fail_statement=fail_statement,
            database=database,
            user=user,
        )
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
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
        )

    def test_apply_is_transactional_and_uses_expected_identity(self):
        connection = FakeConnection()
        plan = migration.apply_migration(
            connection,
            target=self.target,
            confirmation=migration.DEVELOPMENT_APPLY_CONFIRMATION,
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
                confirmation=migration.DEVELOPMENT_APPLY_CONFIRMATION,
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_apply_rejects_wrong_live_session_identity_before_ddl(self):
        connection = FakeConnection(
            database='polytopia2',
            user='polybot',
        )
        with self.assertRaises(migration.MigrationSafetyError):
            migration.apply_migration(
                connection,
                target=self.target,
                confirmation=migration.DEVELOPMENT_APPLY_CONFIRMATION,
            )
        ddl = [
            statement for statement, _ in connection.cursor_value.statements
            if statement.startswith('ALTER TABLE')
        ]
        self.assertEqual(ddl, [])
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_plan_retains_reviewed_reverse_order_without_live_rollback(self):
        plan = migration.plan_migration({})
        self.assertEqual(
            plan.rollback_statements[0].split('"')[3],
            migration.CLEARED_COLUMN,
        )
        self.assertFalse(hasattr(migration, 'rollback_migration'))


class MigrationCliTests(unittest.TestCase):
    def test_apply_refuses_unset_or_production_environment_before_connection(self):
        for environment in (None, 'production'):
            with self.subTest(environment=environment):
                env = {} if environment is None else {
                    'POLYBOT_ENV': environment,
                }
                stderr = StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch.object(
                        migration_script,
                        '_live_connection',
                        side_effect=AssertionError('connection opened'),
                    ),
                    redirect_stderr(stderr),
                ):
                    result = migration_script.main([
                        '--apply',
                        '--confirm',
                        migration.DEVELOPMENT_APPLY_CONFIRMATION,
                    ])
                self.assertEqual(result, 2)
                self.assertIn('development', stderr.getvalue())

    def test_apply_requires_acknowledgement_before_connection(self):
        stderr = StringIO()
        with (
            mock.patch.dict(os.environ, {'POLYBOT_ENV': 'development'}, clear=True),
            mock.patch.object(
                migration_script,
                '_live_connection',
                side_effect=AssertionError('connection opened'),
            ),
            redirect_stderr(stderr),
        ):
            result = migration_script.main(['--apply'])
        self.assertEqual(result, 2)
        self.assertIn('confirmation token', stderr.getvalue())

    def test_rollback_is_offline_review_only_and_does_not_open_connection(self):
        stdout = StringIO()
        with (
            mock.patch.object(
                migration_script,
                '_live_connection',
                side_effect=AssertionError('connection opened'),
            ),
            redirect_stdout(stdout),
        ):
            result = migration_script.main(['--rollback'])
        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn('reviewed rollback statements (not executed)', output)
        self.assertIn(migration.CLEARED_COLUMN, output)
        self.assertIn('no database connection or DDL', output)


if __name__ == '__main__':
    unittest.main()
