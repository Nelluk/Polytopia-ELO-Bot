"""Offline safety coverage for the model-free P12.1 migration."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migration = load(
    'test_player_badges_migration_module',
    ROOT / 'modules/player_badges_migration.py',
)
migration_script = load(
    'test_player_badges_migration_script',
    ROOT / 'scripts/migrate_player_badges.py',
)


class Cursor:
    def __init__(self, *, column=None, identity=('polytopia_dev', 'polybot_dev')):
        self.column = column
        self.identity = identity
        self.rows = []
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        normalized = ' '.join(statement.split()).casefold()
        if normalized.startswith('select current_database'):
            self.rows = [self.identity]
        elif 'information_schema.tables' in normalized:
            self.rows = [(True,)]
        elif 'information_schema.columns' in normalized:
            self.rows = [] if self.column is None else [self.column]
        elif normalized.startswith('alter table'):
            self.column = ('ARRAY', '_text', 'NO', 'ARRAY[]::text[]')
            self.rows = []
        else:
            raise AssertionError(statement)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class Connection:
    def __init__(self, **kwargs):
        self.cursor_value = Cursor(**kwargs)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def target(**changes):
    values = dict(
        environment='development',
        database_name='polytopia_dev',
        database_user='polybot_dev',
    )
    values.update(changes)
    return migration.MigrationTarget(**values)


class MigrationPlanTests(unittest.TestCase):
    def test_plan_is_exact_additive_and_idempotent(self):
        plan = migration.plan_migration(None)
        self.assertEqual(plan.statements, (migration.DDL,))
        self.assertIn('TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]', migration.DDL)
        exact = migration.ColumnState(
            'ARRAY', '_text', 'NO', 'ARRAY[]::text[]'
        )
        self.assertTrue(migration.plan_migration(exact).already_applied)

    def test_wrong_type_element_nullability_or_default_fails_closed(self):
        values = (
            migration.ColumnState('text', '_text', 'NO', 'ARRAY[]::text[]'),
            migration.ColumnState('ARRAY', '_varchar', 'NO', 'ARRAY[]::text[]'),
            migration.ColumnState('ARRAY', '_text', 'YES', 'ARRAY[]::text[]'),
            migration.ColumnState('ARRAY', '_text', 'NO', None),
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(migration.MigrationSafetyError):
                    migration.plan_migration(value)
        with self.assertRaises(migration.MigrationSafetyError):
            migration.plan_migration(None, table_exists=False)

    def test_identity_and_confirmation_are_exact(self):
        migration.validate_apply_target(target())
        migration.validate_apply_confirmation(
            migration.DEVELOPMENT_APPLY_CONFIRMATION
        )
        for invalid in (
            target(environment='production'),
            target(database_name='polytopia2'),
            target(database_user='polybot'),
        ):
            with self.assertRaises(migration.MigrationSafetyError):
                migration.validate_apply_target(invalid)
        with self.assertRaises(migration.MigrationSafetyError):
            migration.validate_apply_confirmation('')


class MigrationExecutionTests(unittest.TestCase):
    def test_apply_is_one_transaction_and_post_verifies(self):
        connection = Connection()
        plan = migration.apply_migration(
            connection,
            target=target(),
            confirmation=migration.DEVELOPMENT_APPLY_CONFIRMATION,
        )
        self.assertFalse(plan.already_applied)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        sql = [value for value, _params in connection.cursor_value.statements]
        self.assertEqual(sum(value == migration.DDL for value in sql), 1)
        self.assertGreaterEqual(
            sum('information_schema.columns' in value for value in sql), 2
        )

    def test_repeat_apply_is_idempotent_without_second_ddl(self):
        connection = Connection(column=(
            'ARRAY', '_text', 'NO', "'{}'::text[]",
        ))
        plan = migration.apply_migration(
            connection,
            target=target(),
            confirmation=migration.DEVELOPMENT_APPLY_CONFIRMATION,
        )
        self.assertTrue(plan.already_applied)
        self.assertFalse(any(
            value.startswith('ALTER TABLE')
            for value, _params in connection.cursor_value.statements
        ))
        self.assertEqual(connection.commits, 1)

    def test_wrong_live_identity_rolls_back_before_ddl(self):
        connection = Connection(identity=('polytopia2', 'polybot'))
        with self.assertRaises(migration.MigrationSafetyError):
            migration.apply_migration(
                connection,
                target=target(),
                confirmation=migration.DEVELOPMENT_APPLY_CONFIRMATION,
            )
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(any(
            value.startswith('ALTER TABLE')
            for value, _params in connection.cursor_value.statements
        ))


class MigrationCliTests(unittest.TestCase):
    def test_default_is_offline_and_no_rollback_command_exists(self):
        output = StringIO()
        with redirect_stdout(output), mock.patch.object(
            migration_script, '_connect'
        ) as connect:
            self.assertEqual(migration_script.main([]), 0)
        connect.assert_not_called()
        self.assertIn('No database connection or DDL was performed.', output.getvalue())
        parser_help = StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(parser_help):
            migration_script._parse_args(['--help'])
        self.assertNotIn('--rollback', parser_help.getvalue())

    def test_import_has_no_apply_or_startup_side_effect(self):
        source = (ROOT / 'modules/player_badges_migration.py').read_text()
        startup = (ROOT / 'bot.py').read_text()
        self.assertNotIn('apply_migration(', startup)
        self.assertNotIn('modules.models', source)


if __name__ == '__main__':
    unittest.main()
