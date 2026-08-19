"""Offline safety coverage for the production player badges migration."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import player_badges_migration as schema
from modules import player_badges_production_migration as migration


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / 'scripts' / 'migrate_player_badges_production.py'
SCRIPT_SPEC = importlib.util.spec_from_file_location(
    'test_migrate_player_badges_production_script', SCRIPT_PATH
)
migration_script = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(migration_script)


EXACT_COLUMN = ('ARRAY', '_text', 'NO', 'ARRAY[]::text[]')


class FakeCursor:
    def __init__(
        self,
        *,
        column=None,
        database='polytopia2',
        user='polybot',
        fail_on_alter=False,
        fail_post_verify=False,
    ):
        self.column = column
        self.database = database
        self.user = user
        self.fail_on_alter = fail_on_alter
        self.fail_post_verify = fail_post_verify
        self.statements = []
        self.rows = []
        self.metadata_reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        normalized = ' '.join(statement.split()).casefold()
        if normalized in {
            'set transaction read only',
            "set local lock_timeout = '5s'",
        }:
            return
        if normalized.startswith('select current_database'):
            self.rows = [(self.database, self.user)]
        elif 'information_schema.tables' in normalized:
            self.rows = [(True,)]
        elif 'information_schema.columns' in normalized:
            self.metadata_reads += 1
            column = self.column
            if self.fail_post_verify and self.metadata_reads > 1:
                column = None
            self.rows = [] if column is None else [column]
        elif normalized.startswith('alter table'):
            if self.fail_on_alter:
                raise RuntimeError('simulated DDL failure')
            self.column = EXACT_COLUMN
            self.rows = []
        else:
            raise AssertionError(statement)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, **kwargs):
        self.cursor_value = FakeCursor(**kwargs)
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


def target(**changes):
    values = dict(
        environment='production',
        database_name='polytopia2',
        database_user='polybot',
    )
    values.update(changes)
    return migration.MigrationTarget(**values)


class ProductionBadgeMigrationPlanTests(unittest.TestCase):
    def test_plan_is_exact_additive_idempotent_and_has_no_rollback(self):
        plan = migration.plan_migration(None)
        self.assertEqual(plan.statements, (schema.DDL,))
        self.assertTrue(migration.plan_migration(
            schema.ColumnState(*EXACT_COLUMN)
        ).already_applied)
        self.assertFalse(hasattr(migration, 'rollback_migration'))

    def test_existing_schema_must_match_exact_contract(self):
        invalid = (
            schema.ColumnState('text', '_text', 'NO', 'ARRAY[]::text[]'),
            schema.ColumnState('ARRAY', '_varchar', 'NO', 'ARRAY[]::text[]'),
            schema.ColumnState('ARRAY', '_text', 'YES', 'ARRAY[]::text[]'),
            schema.ColumnState('ARRAY', '_text', 'NO', None),
        )
        for column in invalid:
            with self.subTest(column=column), self.assertRaises(
                migration.MigrationSafetyError
            ):
                migration.plan_migration(column)

    def test_fixed_production_target_and_confirmation_fail_closed(self):
        migration.validate_target(target(), policy=migration.PRODUCTION_POLICY)
        migration.validate_live_identity(
            target(),
            policy=migration.PRODUCTION_POLICY,
            actual_database='polytopia2',
            actual_user='polybot',
        )
        migration.validate_apply_confirmation(
            migration.PRODUCTION_APPLY_CONFIRMATION,
            policy=migration.PRODUCTION_POLICY,
        )
        for invalid in (
            target(environment='development'),
            target(database_name='polytopia_dev'),
            target(database_user=''),
        ):
            with self.subTest(target=invalid), self.assertRaises(
                migration.MigrationSafetyError
            ):
                migration.validate_target(invalid, policy=migration.PRODUCTION_POLICY)
        with self.assertRaises(migration.MigrationSafetyError):
            migration.validate_apply_confirmation(
                '', policy=migration.PRODUCTION_POLICY
            )


class ProductionBadgeMigrationExecutionTests(unittest.TestCase):
    def test_verify_is_read_only_and_reports_missing_or_complete(self):
        missing = FakeConnection()
        plan = migration.verify_migration(
            missing, target=target(), policy=migration.PRODUCTION_POLICY
        )
        self.assertFalse(plan.already_applied)
        self.assertEqual(missing.commits, 0)
        self.assertEqual(missing.rollbacks, 1)
        self.assertEqual(
            missing.cursor_value.statements[0][0], 'SET TRANSACTION READ ONLY'
        )
        self.assertFalse(any(
            statement.startswith('ALTER TABLE')
            for statement, _params in missing.cursor_value.statements
        ))

        complete = FakeConnection(column=EXACT_COLUMN)
        self.assertTrue(migration.verify_migration(
            complete, target=target(), policy=migration.PRODUCTION_POLICY
        ).already_applied)

    def test_apply_is_atomic_post_verified_and_idempotent(self):
        connection = FakeConnection()
        plan = migration.apply_migration(
            connection,
            target=target(),
            policy=migration.PRODUCTION_POLICY,
            confirmation=migration.PRODUCTION_APPLY_CONFIRMATION,
        )
        self.assertEqual(plan.statements, (schema.DDL,))
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(connection.cursor_value.metadata_reads, 2)
        self.assertEqual(
            connection.cursor_value.statements[0][0],
            "SET LOCAL lock_timeout = '5s'",
        )

        repeat = FakeConnection(column=EXACT_COLUMN)
        repeated = migration.apply_migration(
            repeat,
            target=target(),
            policy=migration.PRODUCTION_POLICY,
            confirmation=migration.PRODUCTION_APPLY_CONFIRMATION,
        )
        self.assertTrue(repeated.already_applied)
        self.assertFalse(any(
            statement.startswith('ALTER TABLE')
            for statement, _params in repeat.cursor_value.statements
        ))
        self.assertEqual(repeat.commits, 1)

    def test_identity_ddl_and_post_verify_failures_roll_back(self):
        cases = (
            (FakeConnection(database='polytopia_dev'), migration.MigrationSafetyError),
            (FakeConnection(user='polybot_dev'), migration.MigrationSafetyError),
            (FakeConnection(fail_on_alter=True), RuntimeError),
            (FakeConnection(fail_post_verify=True), migration.MigrationSafetyError),
        )
        for connection, error in cases:
            with self.subTest(error=error), self.assertRaises(error):
                migration.apply_migration(
                    connection,
                    target=target(),
                    policy=migration.PRODUCTION_POLICY,
                    confirmation=migration.PRODUCTION_APPLY_CONFIRMATION,
                )
            self.assertEqual(connection.commits, 0)
            self.assertEqual(connection.rollbacks, 1)


class ProductionBadgeMigrationCliTests(unittest.TestCase):
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
        self.assertIn('planned apply statements (not executed)', stdout.getvalue())
        self.assertIn('No runtime configuration was loaded', stdout.getvalue())

    def test_live_modes_refuse_nonproduction_before_profile_or_connection(self):
        for mode in ('--verify', '--apply'):
            stderr = StringIO()
            args = [mode]
            if mode == '--apply':
                args += ['--confirm', migration.PRODUCTION_APPLY_CONFIRMATION]
            with (
                mock.patch.dict(os.environ, {'POLYBOT_ENV': 'development'}, clear=True),
                mock.patch.object(
                    migration_script,
                    '_live_connection',
                    side_effect=AssertionError('connection opened'),
                ),
                redirect_stderr(stderr),
            ):
                self.assertEqual(migration_script.main(args), 2)
            self.assertIn('POLYBOT_ENV=production', stderr.getvalue())

    def test_apply_requires_confirmation_before_profile_or_connection(self):
        stderr = StringIO()
        with (
            mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}, clear=True),
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
        complete = FakeConnection(column=EXACT_COLUMN)
        with (
            mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}, clear=True),
            mock.patch(
                'runtime_config.load_runtime_profile', return_value=profile
            ),
            mock.patch.object(
                migration_script, '_live_connection', return_value=complete
            ),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(migration_script.main(['--verify']), 0)
        self.assertTrue(complete.closed)

        applied = FakeConnection(column=EXACT_COLUMN)
        with (
            mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}, clear=True),
            mock.patch(
                'runtime_config.load_runtime_profile', return_value=profile
            ),
            mock.patch.object(
                migration_script, '_live_connection', return_value=applied
            ),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(migration_script.main([
                '--apply',
                '--confirm', migration.PRODUCTION_APPLY_CONFIRMATION,
            ]), 0)
        self.assertTrue(applied.closed)


if __name__ == '__main__':
    unittest.main()
