"""Offline coverage for final-review startup schema authority blockers."""

import asyncio
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from io import StringIO
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase

from modules import development_schema_bootstrap as bootstrap
from modules import startup_schema_preflight as preflight
from modules.database_schema_contract import REQUIRED_TABLES
from scripts import bootstrap_development_database as bootstrap_script


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.connection.statements.append(statement)
        normalized = ' '.join(statement.split()).casefold()
        if normalized.startswith('show transaction_read_only'):
            self.rows = [('on',)]
        elif normalized.startswith('select current_database(), current_user'):
            self.rows = [self.connection.identity]
        elif (
            'select exists' in normalized
            and 'information_schema.tables' in normalized
        ):
            self.rows = [(True,)]
        elif 'information_schema.tables' in normalized:
            self.rows = [(table,) for table in self.connection.tables]
        elif 'information_schema.columns' in normalized:
            if len(params or ()) == 3:
                self.rows = [
                    ('timezone_offset_minutes', 'smallint', 'YES', None),
                    ('timezone_offset_cleared', 'boolean', 'NO', 'false'),
                ] if self.connection.timezone else []
                return
            column_name = str((params or ('', ''))[-1])
            if column_name == 'badges':
                self.rows = [(
                    'ARRAY', '_text', 'NO', 'ARRAY[]::text[]',
                )] if self.connection.badges else []
            elif column_name == 'cleanup_deferred_until':
                self.rows = [(
                    'date', 'date', 'YES', None,
                )] if self.connection.cleanup else []
            else:
                self.rows = []
        elif 'from pg_constraint' in normalized:
            self.rows = [(self.connection.winner_foreign_key,)]
        else:
            raise AssertionError(f'Unexpected SQL: {statement}')

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(
        self,
        *,
        identity=('polytopia_dev', 'polybot_dev'),
        tables=REQUIRED_TABLES,
        winner_foreign_key=True,
        badges=True,
        timezone=True,
        cleanup=True,
    ):
        self.identity = identity
        self.tables = tables
        self.winner_foreign_key = winner_foreign_key
        self.badges = badges
        self.timezone = timezone
        self.cleanup = cleanup
        self.statements = []
        self.session = None
        self.closed = False

    def set_session(self, **kwargs):
        self.session = kwargs

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def request():
    return preflight.StartupSchemaPreflightRequest(
        environment='development',
        database_name='polytopia_dev',
        database_user='polybot_dev',
        database_password='schema-secret',
        database_host='localhost',
        database_port=5432,
    )


class StartupSchemaPreflightTests(unittest.TestCase):
    def test_production_local_peer_authentication_reaches_connector(self):
        peer_request = preflight.StartupSchemaPreflightRequest(
            environment='production',
            database_name='polytopia2',
            database_user='polyelo',
            database_password='',
            database_host=None,
            database_port=None,
        )
        connection = FakeConnection(identity=('polytopia2', 'polyelo'))
        with mock.patch.object(
            preflight.psycopg2,
            'connect',
            return_value=connection,
        ) as connect:
            result = preflight.inspect_startup_schema(peer_request)

        self.assertEqual(result.database_name, 'polytopia2')
        self.assertEqual(result.database_user, 'polyelo')
        connect.assert_called_once_with(
            dbname='polytopia2',
            user='polyelo',
            host=None,
            port=None,
        )

    def test_passwordless_development_and_tcp_production_fail_before_connect(self):
        for environment, host in (
                ('development', None),
                ('development', 'localhost'),
                ('production', 'localhost')):
            with self.subTest(environment=environment, host=host), \
                    mock.patch.object(preflight, '_connect') as connect:
                invalid_request = preflight.StartupSchemaPreflightRequest(
                    environment=environment,
                    database_name='database',
                    database_user='role',
                    database_password='',
                    database_host=host,
                    database_port=None,
                )
                with self.assertRaisesRegex(
                    preflight.StartupSchemaPreflightError,
                    'configured password',
                ):
                    preflight.inspect_startup_schema(invalid_request)
                connect.assert_not_called()

    def test_complete_schema_uses_read_only_selects_and_closes(self):
        connection = FakeConnection()
        with mock.patch.object(
            preflight, '_connect', return_value=connection
        ):
            result = preflight.inspect_startup_schema(request())

        self.assertEqual(
            result,
            preflight.StartupSchemaPreflightResult(
                database_name='polytopia_dev',
                database_user='polybot_dev',
                verified_tables=REQUIRED_TABLES,
                winner_foreign_key_verified=True,
            ),
        )
        self.assertEqual(
            connection.session,
            {'readonly': True, 'autocommit': True},
        )
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.statements), 10)
        for statement in connection.statements:
            normalized = ' '.join(statement.split()).upper()
            self.assertTrue(
                normalized.startswith('SELECT')
                or normalized.startswith('SHOW')
            )
            self.assertNotIn('CREATE ', normalized)
            self.assertNotIn('ALTER ', normalized)
            self.assertNotIn('INSERT ', normalized)
            self.assertNotIn('UPDATE ', normalized)
            self.assertNotIn('DELETE ', normalized)

    def test_missing_table_fails_closed_before_foreign_key_check(self):
        connection = FakeConnection(tables=REQUIRED_TABLES[:-1])
        with mock.patch.object(
            preflight, '_connect', return_value=connection
        ):
            with self.assertRaisesRegex(
                preflight.StartupSchemaPreflightError,
                'missing required tables: tribe',
            ):
                preflight.inspect_startup_schema(request())
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.statements), 3)

    def test_missing_winner_foreign_key_fails_closed(self):
        connection = FakeConnection(winner_foreign_key=False)
        with mock.patch.object(
            preflight, '_connect', return_value=connection
        ):
            with self.assertRaisesRegex(
                preflight.StartupSchemaPreflightError,
                'winner_id -> gameside.id',
            ):
                preflight.inspect_startup_schema(request())
        self.assertTrue(connection.closed)

    def test_missing_player_badges_column_fails_closed(self):
        connection = FakeConnection(badges=False)
        with mock.patch.object(preflight, '_connect', return_value=connection):
            with self.assertRaisesRegex(
                preflight.StartupSchemaPreflightError,
                'missing the required player.badges',
            ):
                preflight.inspect_startup_schema(request())
        self.assertTrue(connection.closed)

    def test_missing_keep_active_column_fails_closed(self):
        connection = FakeConnection(cleanup=False)
        with mock.patch.object(preflight, '_connect', return_value=connection):
            with self.assertRaisesRegex(
                preflight.StartupSchemaPreflightError,
                'missing game.cleanup_deferred_until',
            ):
                preflight.inspect_startup_schema(request())
        self.assertTrue(connection.closed)

    def test_missing_timezone_columns_fail_closed(self):
        connection = FakeConnection(timezone=False)
        with mock.patch.object(preflight, '_connect', return_value=connection):
            with self.assertRaisesRegex(
                preflight.StartupSchemaPreflightError,
                'missing required player-timezone columns',
            ):
                preflight.inspect_startup_schema(request())
        self.assertTrue(connection.closed)

    def test_live_database_identity_mismatch_fails_before_schema_read(self):
        connection = FakeConnection(identity=('wrong', 'wrong_role'))
        with mock.patch.object(
            preflight, '_connect', return_value=connection
        ):
            with self.assertRaisesRegex(
                preflight.StartupSchemaPreflightError,
                'database identity mismatch',
            ):
                preflight.inspect_startup_schema(request())
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.statements), 2)

    def test_request_is_frozen_and_redacts_password(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.database_name = 'changed'
        self.assertNotIn('schema-secret', repr(value))


class StartupSchemaAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_keeps_loop_responsive_and_cancellation_drains(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return preflight.StartupSchemaPreflightResult(
                'polytopia_dev', 'polybot_dev', REQUIRED_TABLES, True
            )

        with mock.patch.object(
            preflight, 'inspect_startup_schema', side_effect=slow
        ):
            task = asyncio.create_task(
                preflight.run_startup_schema_preflight(request())
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.002)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_later(0.01, heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.2)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())


class ModelImportAuthorityTests(unittest.TestCase):
    def test_model_import_performs_no_connection_or_ddl(self):
        model_path = Path(__file__).resolve().parents[1] / 'modules/models.py'
        spec = importlib.util.spec_from_file_location(
            '_polybot_models_schema_authority_probe', model_path
        )
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        settings_stub = SimpleNamespace(
            runtime_profile=SimpleNamespace(
                database_name='offline',
                database_user='offline',
                database_password='offline',
                database_host='localhost',
                database_port=5432,
            )
        )
        with mock.patch.dict('sys.modules', {'settings': settings_stub}), mock.patch.object(
            PostgresqlExtDatabase, 'connect'
        ) as connect, mock.patch.object(
            PostgresqlExtDatabase, 'execute_sql'
        ) as execute_sql, mock.patch.object(
            PostgresqlExtDatabase, 'create_tables'
        ) as create_tables, mock.patch.object(
            SchemaManager, 'create_foreign_key'
        ) as create_foreign_key:
            spec.loader.exec_module(module)

        connect.assert_not_called()
        execute_sql.assert_not_called()
        create_tables.assert_not_called()
        create_foreign_key.assert_not_called()
        self.assertEqual(
            tuple(sorted(
                getattr(module, name)._meta.table_name
                for name in bootstrap.MODEL_NAMES
            )),
            REQUIRED_TABLES,
        )


class DevelopmentBootstrapTests(unittest.TestCase):
    def target(self):
        return bootstrap.DevelopmentSchemaBootstrapTarget(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
            database_password='bootstrap-secret',
            database_host='localhost',
            database_port=5432,
        )

    def test_confirmation_mismatch_fails_before_model_import(self):
        target = self.target()
        with mock.patch.object(bootstrap.importlib, 'import_module') as importer:
            with self.assertRaisesRegex(
                bootstrap.DevelopmentSchemaBootstrapError,
                'confirmation mismatch',
            ):
                bootstrap.bootstrap_development_schema(
                    target, confirmation='wrong'
                )
        importer.assert_not_called()
        self.assertNotIn('bootstrap-secret', repr(target))

    def test_production_target_fails_before_model_import(self):
        target = bootstrap.DevelopmentSchemaBootstrapTarget(
            environment='production',
            database_name='production-database',
            database_user='production-role',
            database_password='production-secret',
            database_host=None,
            database_port=None,
        )
        with mock.patch.object(bootstrap.importlib, 'import_module') as importer:
            with self.assertRaisesRegex(
                bootstrap.DevelopmentSchemaBootstrapError,
                'development-only',
            ):
                bootstrap.bootstrap_development_schema(
                    target,
                    confirmation=bootstrap.confirmation_token(target),
                )
        importer.assert_not_called()

    def test_exact_apply_owns_one_transaction_and_verifies(self):
        target = self.target()
        events = []

        class QueryResult:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class Database:
            @contextmanager
            def connection_context(self):
                events.append('connect')
                try:
                    yield self
                finally:
                    events.append('close')

            @contextmanager
            def atomic(self):
                events.append('begin')
                try:
                    yield self
                finally:
                    events.append('commit')

            def execute_sql(self, statement):
                if 'current_database' in statement:
                    events.append('identity')
                    return QueryResult(('polytopia_dev', 'polybot_dev'))
                events.append('foreign-key-check')
                return QueryResult((False,))

            def create_tables(self, model_classes, *, safe):
                events.append(('create-tables', len(model_classes), safe))

        model_classes = {}
        for name, table in zip(bootstrap.MODEL_NAMES, REQUIRED_TABLES):
            model_classes[name] = SimpleNamespace(
                _meta=SimpleNamespace(table_name=table)
            )
        create_foreign_key = mock.Mock(
            side_effect=lambda _field: events.append('create-foreign-key')
        )
        model_classes['Game']._schema = SimpleNamespace(
            create_foreign_key=create_foreign_key
        )
        model_classes['Game'].winner = object()
        models = SimpleNamespace(db=Database(), **model_classes)
        verified = preflight.StartupSchemaPreflightResult(
            'polytopia_dev', 'polybot_dev', REQUIRED_TABLES, True
        )

        with mock.patch.object(
            bootstrap.importlib, 'import_module', return_value=models
        ), mock.patch.object(
            bootstrap, 'inspect_startup_schema', return_value=verified
        ) as inspect:
            result = bootstrap.bootstrap_development_schema(
                target,
                confirmation=bootstrap.confirmation_token(target),
            )

        self.assertEqual(result, verified)
        self.assertEqual(
            events,
            [
                'connect',
                'identity',
                'begin',
                ('create-tables', len(REQUIRED_TABLES), True),
                'foreign-key-check',
                'create-foreign-key',
                'commit',
                'close',
            ],
        )
        create_foreign_key.assert_called_once_with(models.Game.winner)
        inspect.assert_called_once()

    def test_plan_is_connection_free(self):
        profile = SimpleNamespace(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
            database_password='bootstrap-secret',
            database_host='localhost',
            database_port=5432,
        )
        output = StringIO()
        with mock.patch.object(
            bootstrap_script, 'load_runtime_profile', return_value=profile
        ), mock.patch.object(
            bootstrap_script, 'bootstrap_development_schema'
        ) as apply_bootstrap, mock.patch('sys.stdout', output):
            result = bootstrap_script.main([])

        self.assertEqual(result, 0)
        apply_bootstrap.assert_not_called()
        self.assertIn('Plan only; no database connection', output.getvalue())
        self.assertNotIn('bootstrap-secret', output.getvalue())

    def test_apply_holds_database_lock_through_bootstrap(self):
        profile = SimpleNamespace(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
            database_password='bootstrap-secret',
            database_host='localhost',
            database_port=5432,
        )
        events = []

        class Lock:
            def __enter__(self):
                events.append('lock-enter')

            def __exit__(self, exc_type, *_args):
                events.append(f'lock-exit-{exc_type is None}')

        def apply(*_args, **_kwargs):
            events.append('bootstrap')
            self.assertNotIn('lock-exit-True', events)
            return SimpleNamespace(verified_tables=('one',))

        token = bootstrap.confirmation_token(
            bootstrap.DevelopmentSchemaBootstrapTarget(
                environment=profile.environment,
                database_name=profile.database_name,
                database_user=profile.database_user,
                database_password=profile.database_password,
                database_host=profile.database_host,
                database_port=profile.database_port,
            )
        )
        with mock.patch.object(
            bootstrap_script, 'load_runtime_profile', return_value=profile,
        ), mock.patch.object(
            bootstrap_script.beta_database_writer_lock,
            'BetaDatabaseWriterLock',
            return_value=Lock(),
        ), mock.patch.object(
            bootstrap_script,
            'bootstrap_development_schema',
            side_effect=apply,
        ):
            self.assertEqual(
                bootstrap_script.main(['--apply', '--confirm', token]),
                0,
            )

        self.assertEqual(
            events,
            ['lock-enter', 'bootstrap', 'lock-exit-True'],
        )


if __name__ == '__main__':
    unittest.main()
