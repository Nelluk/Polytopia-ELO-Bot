"""Offline transaction and CLI safety coverage for the P5.17 migrations."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import game_keep_active_migration as development
from modules import game_keep_active_production_migration as production


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


development_script = load_script(
    'test_game_keep_active_development_migration_script',
    ROOT / 'scripts' / 'migrate_game_keep_active.py',
)
production_script = load_script(
    'test_game_keep_active_production_migration_script',
    ROOT / 'scripts' / 'migrate_game_keep_active_production.py',
)


EXACT_COLUMN = ('date', 'date', 'YES', None)


class Cursor:
    def __init__(
        self, *, column=None, database='polytopia_dev', user='polybot_dev',
        readonly='on', fail_on_alter=False, fail_on_lock=False,
        fail_post_verify=False,
    ):
        self.column = column
        self.database = database
        self.user = user
        self.readonly = readonly
        self.fail_on_alter = fail_on_alter
        self.fail_on_lock = fail_on_lock
        self.fail_post_verify = fail_post_verify
        self.metadata_reads = 0
        self.rows = []
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        normalized = ' '.join(statement.split()).casefold()
        if normalized == 'set transaction read only':
            return
        if normalized == 'show transaction_read_only':
            self.rows = [(self.readonly,)]
            return
        if normalized == "set local lock_timeout = '5s'":
            if self.fail_on_lock:
                raise RuntimeError('simulated lock timeout')
            return
        if normalized.startswith('select current_database'):
            self.rows = [(self.database, self.user)]
            return
        if 'information_schema.tables' in normalized:
            self.rows = [(True,)]
            return
        if 'information_schema.columns' in normalized:
            self.metadata_reads += 1
            column = self.column
            if self.fail_post_verify and self.metadata_reads > 1:
                column = None
            self.rows = [] if column is None else [column]
            return
        if normalized.startswith('alter table'):
            if self.fail_on_alter:
                raise RuntimeError('simulated DDL failure')
            self.column = EXACT_COLUMN
            self.rows = []
            return
        raise AssertionError(statement)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, **kwargs):
        self.cursor_value = Cursor(**kwargs)
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


def development_target(**changes):
    values = dict(
        environment='development',
        database_name='polytopia_dev',
        database_user='polybot_dev',
    )
    values.update(changes)
    return development.MigrationTarget(**values)


def production_target(**changes):
    values = dict(
        environment='production',
        database_name='polytopia2',
        database_user='polybot',
    )
    values.update(changes)
    return production.MigrationTarget(**values)


class DevelopmentMigrationSafetyTests(unittest.TestCase):
    def test_inspect_sets_read_only_and_rolls_back_after_metadata_read(self):
        connection = Connection()
        plan = development.inspect_migration(
            connection, target=development_target(),
        )
        self.assertFalse(plan.already_applied)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            connection.cursor_value.statements[:2],
            [('SET TRANSACTION READ ONLY', None),
             ('SHOW transaction_read_only', None)],
        )
        self.assertFalse(any(
            statement.startswith('ALTER TABLE')
            for statement, _params in connection.cursor_value.statements
        ))

    def test_inspect_fails_closed_and_rolls_back_when_read_only_is_not_on(self):
        connection = Connection(readonly='off')
        with self.assertRaises(development.MigrationSafetyError):
            development.inspect_migration(
                connection, target=development_target(),
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_inspect_identity_mismatch_rolls_back(self):
        connection = Connection(database='polytopia2', user='polybot')
        with self.assertRaises(development.MigrationSafetyError):
            development.inspect_migration(
                connection, target=development_target(),
            )
        self.assertEqual(connection.rollbacks, 1)


class ProductionMigrationSafetyTests(unittest.TestCase):
    def test_verify_is_read_only_and_always_rolls_back(self):
        connection = Connection(database='polytopia2', user='polybot')
        connection.cursor_value.readonly = 'on'
        plan = production.verify_migration(
            connection, target=production_target(),
        )
        self.assertFalse(plan.already_applied)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            [statement for statement, _params in connection.cursor_value.statements[:2]],
            ['SET TRANSACTION READ ONLY', 'SHOW transaction_read_only'],
        )

    def test_verify_read_only_failure_and_identity_mismatch_roll_back(self):
        for kwargs in (
            {'database': 'polytopia_dev', 'user': 'polybot_dev'},
            {'database': 'polytopia2', 'user': 'polybot', 'readonly': 'off'},
        ):
            connection = Connection(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(
                production.MigrationSafetyError
            ):
                production.verify_migration(
                    connection, target=production_target(),
                )
            self.assertEqual(connection.commits, 0)
            self.assertEqual(connection.rollbacks, 1)

    def test_apply_is_idempotent_and_rolls_back_every_failure(self):
        kwargs = dict(
            database='polytopia2', user='polybot',
        )
        connection = Connection(**kwargs)
        plan = production.apply_migration(
            connection, target=production_target(),
            confirmation=production.PRODUCTION_APPLY_CONFIRMATION,
        )
        self.assertEqual(plan.statements, (development.DDL,))
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)

        repeat = Connection(column=EXACT_COLUMN, **kwargs)
        self.assertTrue(production.apply_migration(
            repeat, target=production_target(),
            confirmation=production.PRODUCTION_APPLY_CONFIRMATION,
        ).already_applied)
        self.assertEqual(repeat.commits, 1)
        self.assertFalse(any(
            statement.startswith('ALTER TABLE')
            for statement, _params in repeat.cursor_value.statements
        ))

        for failure in (
            {'fail_on_lock': True},
            {'fail_on_alter': True},
            {'fail_post_verify': True},
        ):
            failed = Connection(**kwargs, **failure)
            with self.subTest(failure=failure), self.assertRaises(Exception):
                production.apply_migration(
                    failed, target=production_target(),
                    confirmation=production.PRODUCTION_APPLY_CONFIRMATION,
                )
            self.assertEqual(failed.commits, 0)
            self.assertEqual(failed.rollbacks, 1)

    def test_exact_identity_and_confirmation_are_required(self):
        with self.assertRaises(production.MigrationSafetyError):
            production.validate_live_identity(
                production_target(), actual_database='polytopia_dev',
                actual_user='polybot_dev',
            )
        with self.assertRaises(production.MigrationSafetyError):
            production.validate_apply_confirmation('wrong')


class MigrationCliSafetyTests(unittest.TestCase):
    def test_both_plan_only_clis_are_connection_free(self):
        for script in (development_script, production_script):
            output = StringIO()
            with redirect_stdout(output), mock.patch.dict(
                os.environ, {}, clear=True,
            ):
                self.assertEqual(script.main([]), 0)
            self.assertIn('No database connection or DDL was performed.', output.getvalue())

    def test_live_cli_modes_refuse_wrong_environment_before_profile(self):
        for script, expected in (
            (development_script, 'development'),
            (production_script, 'production'),
        ):
            error = StringIO()
            with mock.patch.dict(
                os.environ, {'POLYBOT_ENV': 'not-the-target'}, clear=True,
            ), redirect_stderr(error):
                self.assertEqual(script.main(['--verify']), 2)
            self.assertIn(expected, error.getvalue())

    def test_production_cli_apply_requires_exact_confirmation(self):
        profile = SimpleNamespace(
            environment='production', database_name='polytopia2',
            database_user='polybot',
        )
        error = StringIO()
        with (
            mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}, clear=True),
            mock.patch('runtime_config.get_runtime_profile', return_value=profile),
            redirect_stderr(error),
        ):
            self.assertEqual(production_script.main(['--apply']), 2)
        self.assertIn('acknowledgement', error.getvalue())


if __name__ == '__main__':
    unittest.main()
