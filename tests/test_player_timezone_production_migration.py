"""Offline safety coverage for the B1 production timezone migration."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import player_timezone_migration as schema
from modules import player_timezone_production_migration as migration


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / 'scripts'
    / 'migrate_player_timezone_production.py'
)
_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    'test_migrate_player_timezone_production_script',
    _SCRIPT_PATH,
)
migration_script = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(migration_script)


def expected_columns():
    return {
        schema.MINUTES_COLUMN: schema.ColumnState(
            name=schema.MINUTES_COLUMN,
            data_type='smallint',
            is_nullable='YES',
            column_default=None,
        ),
        schema.CLEARED_COLUMN: schema.ColumnState(
            name=schema.CLEARED_COLUMN,
            data_type='boolean',
            is_nullable='NO',
            column_default='false',
        ),
    }


class FakeCursor:
    def __init__(
            self,
            *,
            columns=None,
            database='polytopia2',
            user='polybot',
            fail_on_alter=False,
            fail_post_verify=False):
        self.columns = dict(columns or {})
        self.database = database
        self.user = user
        self.fail_on_alter = fail_on_alter
        self.fail_post_verify = fail_post_verify
        self.statements = []
        self._fetchone = None
        self._fetchall = []
        self.metadata_reads = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if statement == 'SET TRANSACTION READ ONLY':
            return
        if statement.startswith('SELECT current_database'):
            self._fetchone = (self.database, self.user)
            return
        if 'information_schema.tables' in statement:
            self._fetchone = (True,)
            return
        if 'information_schema.columns' in statement:
            self.metadata_reads += 1
            values = self.columns
            if self.fail_post_verify and self.metadata_reads > 1:
                values = {
                    key: value for key, value in values.items()
                    if key != schema.CLEARED_COLUMN
                }
            self._fetchall = [
                (
                    value.name,
                    value.data_type,
                    value.is_nullable,
                    value.column_default,
                )
                for value in values.values()
            ]
            return
        if statement.startswith('ALTER TABLE'):
            if self.fail_on_alter:
                raise RuntimeError('simulated DDL failure')
            if schema.MINUTES_COLUMN in statement:
                self.columns[schema.MINUTES_COLUMN] = expected_columns()[
                    schema.MINUTES_COLUMN
                ]
            elif schema.CLEARED_COLUMN in statement:
                self.columns[schema.CLEARED_COLUMN] = expected_columns()[
                    schema.CLEARED_COLUMN
                ]

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class FakeConnection:
    def __init__(self, **cursor_kwargs):
        self.cursor_value = FakeCursor(**cursor_kwargs)
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


class ProductionMigrationPlanTests(unittest.TestCase):
    def test_offline_plan_is_additive_and_exposes_no_destructive_rollback(self):
        plan = migration.plan_migration({})
        self.assertEqual(plan.added_columns, (
            schema.MINUTES_COLUMN,
            schema.CLEARED_COLUMN,
        ))
        self.assertEqual(len(plan.statements), 2)
        self.assertTrue(all(
            'ALTER TABLE "public"."discordmember"' in statement
            for statement in plan.statements
        ))
        self.assertFalse(hasattr(plan, 'rollback_statements'))
        self.assertFalse(hasattr(migration, 'rollback_migration'))

    def test_existing_schema_requires_exact_type_nullability_and_defaults(self):
        self.assertTrue(
            migration.plan_migration(expected_columns()).already_applied
        )
        accepted = expected_columns()
        accepted[schema.CLEARED_COLUMN] = schema.ColumnState(
            name=schema.CLEARED_COLUMN,
            data_type='boolean',
            is_nullable='NO',
            column_default='false::boolean',
        )
        self.assertTrue(migration.plan_migration(accepted).already_applied)

        invalid_cases = []
        minutes_default = expected_columns()
        minutes_default[schema.MINUTES_COLUMN] = schema.ColumnState(
            name=schema.MINUTES_COLUMN,
            data_type='smallint',
            is_nullable='YES',
            column_default='0',
        )
        invalid_cases.append(minutes_default)
        cleared_default = expected_columns()
        cleared_default[schema.CLEARED_COLUMN] = schema.ColumnState(
            name=schema.CLEARED_COLUMN,
            data_type='boolean',
            is_nullable='NO',
            column_default='true',
        )
        invalid_cases.append(cleared_default)
        wrong_type = expected_columns()
        wrong_type[schema.MINUTES_COLUMN] = schema.ColumnState(
            name=schema.MINUTES_COLUMN,
            data_type='integer',
            is_nullable='YES',
        )
        invalid_cases.append(wrong_type)
        for columns in invalid_cases:
            with self.subTest(columns=columns), self.assertRaises(
                    migration.MigrationSafetyError):
                migration.plan_migration(columns)

    def test_fixed_production_target_and_confirmation_fail_closed(self):
        target = migration.MigrationTarget(
            environment='production',
            database_name='polytopia2',
            database_user='polybot',
        )
        migration.validate_target(target, policy=migration.PRODUCTION_POLICY)
        migration.validate_live_identity(
            target,
            policy=migration.PRODUCTION_POLICY,
            actual_database='polytopia2',
            actual_user='polybot',
        )
        migration.validate_apply_confirmation(
            migration.PRODUCTION_APPLY_CONFIRMATION,
            policy=migration.PRODUCTION_POLICY,
        )
        for changed in (
            migration.MigrationTarget('development', 'polytopia2', 'polybot'),
            migration.MigrationTarget('production', 'polytopia_dev', 'polybot'),
            migration.MigrationTarget('production', 'polytopia2', ''),
        ):
            with self.subTest(target=changed), self.assertRaises(
                    migration.MigrationSafetyError):
                migration.validate_target(changed, policy=migration.PRODUCTION_POLICY)
        with self.assertRaises(migration.MigrationSafetyError):
            migration.validate_apply_confirmation(
                '', policy=migration.PRODUCTION_POLICY
            )


class ProductionMigrationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.target = migration.MigrationTarget(
            environment='production',
            database_name='polytopia2',
            database_user='polybot',
        )

    def test_verify_is_read_only_and_reports_missing_or_complete_schema(self):
        missing = FakeConnection()
        plan = migration.verify_migration(
            missing,
            target=self.target,
            policy=migration.PRODUCTION_POLICY,
        )
        self.assertFalse(plan.already_applied)
        self.assertEqual(missing.commits, 0)
        self.assertEqual(missing.rollbacks, 1)
        self.assertEqual(
            missing.cursor_value.statements[0][0],
            'SET TRANSACTION READ ONLY',
        )
        self.assertFalse(any(
            statement.startswith('ALTER TABLE')
            for statement, _ in missing.cursor_value.statements
        ))

        complete = FakeConnection(columns=expected_columns())
        self.assertTrue(migration.verify_migration(
            complete,
            target=self.target,
            policy=migration.PRODUCTION_POLICY,
        ).already_applied)

    def test_apply_is_atomic_post_verified_and_idempotent(self):
        connection = FakeConnection()
        plan = migration.apply_migration(
            connection,
            target=self.target,
            policy=migration.PRODUCTION_POLICY,
            confirmation=migration.PRODUCTION_APPLY_CONFIRMATION,
        )
        self.assertEqual(len(plan.statements), 2)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(connection.cursor_value.metadata_reads, 2)
        self.assertEqual(
            connection.cursor_value.statements[0][0],
            "SET LOCAL lock_timeout = '5s'",
        )

        repeat = FakeConnection(columns=expected_columns())
        repeated_plan = migration.apply_migration(
            repeat,
            target=self.target,
            policy=migration.PRODUCTION_POLICY,
            confirmation=migration.PRODUCTION_APPLY_CONFIRMATION,
        )
        self.assertTrue(repeated_plan.already_applied)
        self.assertFalse(any(
            statement.startswith('ALTER TABLE')
            for statement, _ in repeat.cursor_value.statements
        ))
        self.assertEqual(repeat.commits, 1)

    def test_identity_ddl_and_post_verify_failures_roll_back(self):
        cases = (
            (FakeConnection(database='polytopia_dev'), migration.MigrationSafetyError),
            (FakeConnection(user='polybot_dev'), migration.MigrationSafetyError),
            (FakeConnection(fail_on_alter=True), RuntimeError),
            (FakeConnection(fail_post_verify=True), migration.MigrationSafetyError),
        )
        for connection, expected_error in cases:
            with self.subTest(connection=connection), self.assertRaises(expected_error):
                migration.apply_migration(
                    connection,
                    target=self.target,
                    policy=migration.PRODUCTION_POLICY,
                    confirmation=migration.PRODUCTION_APPLY_CONFIRMATION,
                )
            self.assertEqual(connection.commits, 0)
            self.assertEqual(connection.rollbacks, 1)


class ProductionMigrationCliTests(unittest.TestCase):
    def test_default_plan_loads_no_profile_and_opens_no_connection(self):
        stdout = StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                migration_script,
                '_live_connection',
                side_effect=AssertionError('connection opened'),
            ),
            redirect_stdout(stdout),
        ):
            self.assertEqual(migration_script.main([]), 0)
        output = stdout.getvalue()
        self.assertIn('planned apply statements (not executed)', output)
        self.assertIn('No runtime configuration was loaded', output)
        self.assertIn('retain the additive columns', output)

    def test_live_modes_refuse_nonproduction_before_profile_or_connection(self):
        for mode in ('--verify', '--apply'):
            stderr = StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {'POLYBOT_ENV': 'development'},
                    clear=True,
                ),
                mock.patch.object(
                    migration_script,
                    '_live_connection',
                    side_effect=AssertionError('connection opened'),
                ),
                redirect_stderr(stderr),
            ):
                arguments = [mode]
                if mode == '--apply':
                    arguments += [
                        '--confirm', migration.PRODUCTION_APPLY_CONFIRMATION,
                    ]
                self.assertEqual(migration_script.main(arguments), 2)
            self.assertIn('POLYBOT_ENV=production', stderr.getvalue())

    def test_apply_requires_confirmation_before_profile_or_connection(self):
        stderr = StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {'POLYBOT_ENV': 'production'},
                clear=True,
            ),
            mock.patch.object(
                migration_script,
                '_live_connection',
                side_effect=AssertionError('connection opened'),
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(migration_script.main(['--apply']), 2)
        self.assertIn('confirmation token', stderr.getvalue())

    def test_verify_and_apply_use_profile_target_and_close_connection(self):
        profile = SimpleNamespace(
            environment='production',
            database_name='polytopia2',
            database_user='polybot',
        )
        complete_connection = FakeConnection(columns=expected_columns())
        with (
            mock.patch.dict(
                os.environ,
                {'POLYBOT_ENV': 'production'},
                clear=True,
            ),
            mock.patch(
                'runtime_config.load_runtime_profile',
                return_value=profile,
            ),
            mock.patch.object(
                migration_script,
                '_live_connection',
                return_value=complete_connection,
            ),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(migration_script.main(['--verify']), 0)
        self.assertTrue(complete_connection.closed)

        apply_connection = FakeConnection(columns=expected_columns())
        with (
            mock.patch.dict(
                os.environ,
                {'POLYBOT_ENV': 'production'},
                clear=True,
            ),
            mock.patch(
                'runtime_config.load_runtime_profile',
                return_value=profile,
            ),
            mock.patch.object(
                migration_script,
                '_live_connection',
                return_value=apply_connection,
            ),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(migration_script.main([
                '--apply',
                '--confirm', migration.PRODUCTION_APPLY_CONFIRMATION,
            ]), 0)
        self.assertTrue(apply_connection.closed)

    def test_live_mode_rejects_misconfigured_profile_before_connection(self):
        bad_profile = SimpleNamespace(
            environment='production',
            database_name='polytopia_dev',
            database_user='polybot_dev',
        )
        stderr = StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {'POLYBOT_ENV': 'production'},
                clear=True,
            ),
            mock.patch(
                'runtime_config.load_runtime_profile',
                return_value=bad_profile,
            ),
            mock.patch.object(
                migration_script,
                '_live_connection',
                side_effect=AssertionError('connection opened'),
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(migration_script.main(['--verify']), 2)
        self.assertIn('polytopia2', stderr.getvalue())

    def test_cli_has_no_rollback_mode(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            migration_script._parse_args(['--rollback'])


if __name__ == '__main__':
    unittest.main()
