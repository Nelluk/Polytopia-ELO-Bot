"""Offline coverage for the P10.6b1 inactive draft store."""

from __future__ import annotations

import datetime
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import guild_configuration_draft_storage as drafts
from modules.guild_configuration_schema import document_digest, document_to_mapping
from scripts import manage_guild_configuration_drafts as script
from tests import test_guild_configuration_storage as fixtures


NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


def exact_inventory():
    return drafts.DraftSchemaInventory(
        (drafts.DRAFT_TABLE,),
        tuple(sorted(drafts.EXPECTED_COLUMNS)),
        drafts.EXPECTED_CONSTRAINTS,
    )


def draft_row(*, version=1, digest=None, actor='discord:1'):
    document = fixtures.bundle().imports[0].document
    return (
        fixtures.GUILD_ID, version, 1, 1, document.schema_version,
        document_to_mapping(document), digest or document_digest(document), actor,
        NOW, NOW, NOW + datetime.timedelta(hours=24),
    )


class Cursor:
    def __init__(self, *, inventory=None):
        self.inventory = exact_inventory() if inventory is None else inventory
        self.statements = []
        self.row = None
        self.rows = []
        self.rowcount = 1
        self.inventory_reads = 0

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
            self.inventory_reads += 1
            self.rows = [(value,) for value in self.inventory.tables]
        elif 'information_schema.columns' in statement:
            self.rows = list(self.inventory.columns)
        elif 'FROM pg_constraint' in statement:
            self.rows = list(self.inventory.constraints)
        elif 'RETURNING guild_id' in statement:
            self.row = draft_row(version=2 if statement.startswith('UPDATE') else 1)
        elif 'MAX(revision_number)' in statement:
            self.row = (1,)
        elif 'MAX(event_number)' in statement:
            self.row = (1,)

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


class SchemaContractTests(unittest.TestCase):
    def test_plan_is_deterministic_additive_and_connection_free(self):
        first = drafts.draft_schema_plan(fixtures.target())
        second = drafts.draft_schema_plan(fixtures.target())
        self.assertEqual(first, second)
        self.assertRegex(first.confirmation, r'^P10\.6B1 APPLY [0-9a-f]{64}$')
        sql = '\n'.join(first.statements).upper()
        self.assertEqual(sql.count('CREATE TABLE'), 1)
        for forbidden in ('DROP ', 'ALTER ', 'DELETE FROM', 'TRUNCATE '):
            self.assertNotIn(forbidden, sql)
        self.assertFalse(drafts.plan_to_mapping(first)['database_connected'])

    def test_inventory_distinguishes_absent_exact_and_drift(self):
        self.assertFalse(drafts.validate_draft_schema(
            drafts.DraftSchemaInventory((), (), ())
        ))
        self.assertTrue(drafts.validate_draft_schema(exact_inventory()))
        with self.assertRaisesRegex(drafts.GuildConfigurationDraftStorageError, 'columns'):
            drafts.validate_draft_schema(drafts.DraftSchemaInventory(
                (drafts.DRAFT_TABLE,), drafts.EXPECTED_COLUMNS[:-1],
                drafts.EXPECTED_CONSTRAINTS,
            ))

    def test_confirmation_is_checked_before_cursor(self):
        connection = mock.Mock()
        plan = drafts.draft_schema_plan(fixtures.target())
        with self.assertRaisesRegex(drafts.GuildConfigurationDraftStorageError, 'confirmation'):
            drafts.apply_draft_schema(
                connection, target=fixtures.target(), plan=plan, confirmation='wrong'
            )
        connection.cursor.assert_not_called()

    def test_fresh_apply_locks_creates_reverifies_and_commits(self):
        connection = Connection()
        plan = drafts.draft_schema_plan(fixtures.target())
        inventories = iter((drafts.DraftSchemaInventory((), (), ()), exact_inventory()))
        with mock.patch.object(drafts, '_validate_live_connection'), \
                mock.patch.object(drafts, 'inspect_draft_schema', side_effect=inventories):
            result = drafts.apply_draft_schema(
                connection, target=fixtures.target(), plan=plan,
                confirmation=plan.confirmation,
            )
        self.assertTrue(result.schema_created)
        self.assertEqual(connection.commits, 1)
        statements = connection.cursor_value.statements
        self.assertIn(('SELECT pg_advisory_xact_lock(%s)', (drafts.DRAFT_ADVISORY_LOCK_KEY,)), statements)
        self.assertIn((drafts.CREATE_DRAFT_SCHEMA_STATEMENTS[0], None), statements)

    def test_apply_failure_rolls_back(self):
        connection = Connection()
        plan = drafts.draft_schema_plan(fixtures.target())
        with mock.patch.object(
            drafts, '_validate_live_connection', side_effect=RuntimeError('boom')
        ):
            with self.assertRaisesRegex(RuntimeError, 'boom'):
                drafts.apply_draft_schema(
                    connection, target=fixtures.target(), plan=plan,
                    confirmation=plan.confirmation,
                )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)


class DraftPersistenceTests(unittest.TestCase):
    def test_row_is_canonical_immutable_document(self):
        value = drafts.draft_from_row(draft_row())
        self.assertEqual(value.document, fixtures.bundle().imports[0].document)
        self.assertEqual(value.base_revision, 1)
        with self.assertRaisesRegex(drafts.GuildConfigurationDraftStorageError, 'metadata'):
            drafts.draft_from_row(draft_row(digest='0' * 64))
        with self.assertRaisesRegex(drafts.GuildConfigurationDraftStorageError, 'actor'):
            drafts.draft_from_row(draft_row(actor='x' * 201))

    def test_put_replaces_complete_document_and_refreshes_ttl(self):
        cursor = Cursor()
        document = fixtures.bundle().imports[0].document
        result = drafts.put_draft(
            cursor, guild_id=fixtures.GUILD_ID, base_revision=1,
            base_generation=1, document=document, actor='discord:1',
        )
        statement, parameters = cursor.statements[-1]
        self.assertIn('ON CONFLICT (guild_id) DO UPDATE', statement)
        self.assertIn("INTERVAL '24 hours'", statement)
        self.assertEqual(parameters[-1], 'discord:1')
        self.assertEqual(result.document, document)

    def test_replace_and_discard_require_optimistic_owner_evidence(self):
        cursor = Cursor()
        document = fixtures.bundle().imports[0].document
        drafts.replace_draft(
            cursor, guild_id=fixtures.GUILD_ID, expected_version=1,
            expected_digest=document_digest(document), base_revision=1,
            base_generation=1, document=document, actor='discord:1',
        )
        statement, parameters = cursor.statements[-1]
        self.assertIn('draft_version = %s AND document_digest = %s', statement)
        self.assertEqual(parameters[3], 'discord:1')
        drafts.expire_draft(
            cursor, guild_id=fixtures.GUILD_ID, expected_version=2,
            expected_digest=document_digest(document), actor='discord:1',
        )
        self.assertIn('expires_at = CURRENT_TIMESTAMP', cursor.statements[-1][0])
        self.assertNotIn('actor = %s', cursor.statements[-1][0])

    def test_stale_replace_and_discard_fail_closed(self):
        document = fixtures.bundle().imports[0].document
        cursor = Cursor()
        cursor.row = None
        original_execute = cursor.execute

        def no_return(statement, parameters=None):
            original_execute(statement, parameters)
            if 'RETURNING guild_id' in statement:
                cursor.row = None

        cursor.execute = no_return
        with self.assertRaisesRegex(drafts.GuildConfigurationDraftStorageError, 'changed or expired'):
            drafts.replace_draft(
                cursor, guild_id=fixtures.GUILD_ID, expected_version=1,
                expected_digest=document_digest(document), base_revision=1,
                base_generation=1, document=document, actor='discord:1',
            )
        cursor.rowcount = 0
        with self.assertRaisesRegex(drafts.GuildConfigurationDraftStorageError, 'changed or expired'):
            drafts.expire_draft(
                cursor, guild_id=fixtures.GUILD_ID, expected_version=1,
                expected_digest=document_digest(document), actor='discord:1',
            )

    def test_activation_appends_revision_audit_advances_generation_and_expires(self):
        cursor = Cursor()
        value = drafts.draft_from_row(draft_row())
        result = drafts.activate_draft(
            cursor,
            draft=value,
            active_revision=1,
            active_generation=1,
            active_document_digest=value.document_digest,
            actor='discord:1',
            changed_paths=('identity.display_name',),
        )
        self.assertEqual((result.revision, result.generation), (2, 2))
        self.assertEqual(result.event_number, 2)
        statements = tuple(value[0] for value in cursor.statements)
        self.assertTrue(any(
            statement.startswith(f'INSERT INTO "{drafts.storage.REVISION_TABLE}"')
            for statement in statements
        ))
        self.assertTrue(any(
            statement.startswith(f'UPDATE "{drafts.storage.REGISTRY_TABLE}"')
            for statement in statements
        ))
        self.assertTrue(any(
            statement.startswith(f'INSERT INTO "{drafts.storage.AUDIT_TABLE}"')
            for statement in statements
        ))
        self.assertIn('expires_at = CURRENT_TIMESTAMP', statements[-1])
        self.assertNotIn('DELETE FROM', '\n'.join(statements))

    def test_activation_registry_cas_failure_stops_before_audit_and_expiry(self):
        cursor = Cursor()
        value = drafts.draft_from_row(draft_row())
        original_execute = cursor.execute

        def fail_registry(statement, parameters=None):
            original_execute(statement, parameters)
            if statement.startswith(f'UPDATE "{drafts.storage.REGISTRY_TABLE}"'):
                cursor.rowcount = 0

        cursor.execute = fail_registry
        with self.assertRaisesRegex(
            drafts.GuildConfigurationDraftStorageError,
            'changed during activation',
        ):
            drafts.activate_draft(
                cursor,
                draft=value,
                active_revision=1,
                active_generation=1,
                active_document_digest=value.document_digest,
                actor='discord:1',
                changed_paths=('identity.display_name',),
            )
        statements = '\n'.join(value[0] for value in cursor.statements)
        self.assertNotIn(f'INSERT INTO "{drafts.storage.AUDIT_TABLE}"', statements)
        self.assertNotIn('expires_at = CURRENT_TIMESTAMP', statements)


class ScriptTests(unittest.TestCase):
    def profile(self):
        return SimpleNamespace(
            environment='development', database_name='polytopia_dev',
            database_user='polybot_dev', database_password='secret',
            database_host='localhost', database_port=5432,
            expected_bot_id=drafts.storage.DEVELOPMENT_BETA_APPLICATION_ID,
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
