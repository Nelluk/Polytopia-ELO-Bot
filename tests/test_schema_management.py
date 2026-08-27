"""Offline coverage for installation-neutral schema management."""

from io import StringIO
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import schema_management as schema
from modules.database_schema_contract import REQUIRED_TABLES
from scripts import manage_schema


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        normalized = ' '.join(statement.split()).casefold()
        self.connection.statements.append((statement, params))
        if normalized == 'show transaction_read_only':
            self.rows = [('on',)]
        elif normalized.startswith('select current_database'):
            self.rows = [self.connection.identity]
        elif 'information_schema.tables' in normalized:
            self.rows = [(name,) for name in self.connection.tables]
        else:
            raise AssertionError(statement)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class Connection:
    def __init__(self, identity=('community_bot', 'community_role'), tables=()):
        self.identity = identity
        self.tables = tables
        self.statements = []
        self.session = None
        self.closed = False

    def set_session(self, **kwargs):
        self.session = kwargs

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


def target(**changes):
    values = dict(
        environment='production',
        database_name='community_bot',
        database_user='community_role',
        database_password='schema-secret',
        database_host='localhost',
        database_port=5432,
    )
    values.update(changes)
    return schema.SchemaTarget(**values)


class SchemaManagementTests(unittest.TestCase):
    def test_fresh_database_plan_accepts_configured_non_upstream_identity(self):
        connection = Connection()
        plan = schema.inspect_schema(target(), connect=lambda _target: connection)

        self.assertEqual(plan.database_name, 'community_bot')
        self.assertEqual(plan.database_user, 'community_role')
        self.assertIn('create missing table public.player', plan.operations)
        self.assertIn(
            'create game.winner_id -> gameside.id foreign key',
            plan.operations,
        )
        self.assertEqual(
            sum(operation.startswith('create missing table') for operation in plan.operations),
            len(REQUIRED_TABLES),
        )
        self.assertEqual(connection.session, {'readonly': True, 'autocommit': True})
        self.assertTrue(connection.closed)

    def test_live_identity_mismatch_closes_without_writes(self):
        connection = Connection(identity=('wrong', 'wrong'))
        with self.assertRaisesRegex(schema.SchemaManagementError, 'identity mismatch'):
            schema.inspect_schema(target(), connect=lambda _target: connection)
        self.assertTrue(connection.closed)
        self.assertFalse(any(
            statement.lstrip().upper().startswith(('ALTER', 'CREATE', 'INSERT'))
            for statement, _params in connection.statements
        ))

    def test_confirmation_mismatch_precedes_model_import_and_redacts_password(self):
        value = target()
        self.assertNotIn('schema-secret', repr(value))
        with mock.patch.object(schema.importlib, 'import_module') as importer:
            with self.assertRaisesRegex(schema.SchemaManagementError, 'confirmation mismatch'):
                schema.apply_schema(value, confirmation='wrong')
        importer.assert_not_called()

    def test_cli_plan_prints_dynamic_confirmation_without_secrets(self):
        profile = SimpleNamespace(
            environment='production',
            database_name='community_bot',
            database_user='community_role',
            database_password='schema-secret',
            database_host='localhost',
            database_port=5432,
        )
        plan = schema.SchemaPlan('community_bot', 'community_role', ('one change',))
        output = StringIO()
        with mock.patch.object(
            manage_schema, 'load_runtime_profile', return_value=profile
        ), mock.patch.object(
            manage_schema, 'inspect_schema', return_value=plan
        ), mock.patch('sys.stdout', output):
            self.assertEqual(manage_schema.main([]), 0)

        text = output.getvalue()
        self.assertIn(
            'APPLY PRODUCTION SCHEMA TO community_bot AS community_role', text
        )
        self.assertNotIn('schema-secret', text)

    def test_cli_wrong_apply_confirmation_precedes_database_inspection(self):
        profile = SimpleNamespace(
            environment='production',
            database_name='community_bot',
            database_user='community_role',
            database_password='schema-secret',
            database_host='localhost',
            database_port=5432,
        )
        with mock.patch.object(
            manage_schema, 'load_runtime_profile', return_value=profile
        ), mock.patch.object(manage_schema, 'inspect_schema') as inspect:
            with self.assertRaisesRegex(
                schema.SchemaManagementError, 'confirmation mismatch'
            ):
                manage_schema.main(['--apply', '--confirm', 'wrong'])
        inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
