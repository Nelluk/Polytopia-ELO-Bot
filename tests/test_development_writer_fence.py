"""Contracts for the development writer-fence schema and writer inventory."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import development_writer_fence as fence
from scripts import manage_development_writer_fence as script


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DevelopmentWriterFenceTests(unittest.TestCase):
    class Connection:
        def __init__(self):
            self.identity = ('polytopia_dev', 'polybot_dev')
            self.row = (
                fence.FENCE_SCHEMA_VERSION,
                0,
                {},
            )
            self.statements = []
            self.commits = 0
            self.rollbacks = 0

        class Cursor:
            def __init__(self, connection):
                self.connection = connection
                self.result = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement, params=None):
                self.connection.statements.append((statement, params))
                if 'pg_try_advisory_lock' in statement:
                    self.result = (*self.connection.identity, True)
                elif 'current_database()' in statement:
                    self.result = self.connection.identity
                elif 'SELECT EXISTS' in statement:
                    self.result = (True,)
                elif 'pg_advisory_unlock' in statement:
                    self.result = (True,)
                elif 'SELECT schema_version' in statement:
                    self.result = self.connection.row

            def fetchone(self):
                return self.result

        def cursor(self):
            return self.Cursor(self)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    @staticmethod
    def target():
        return fence.WriterFenceTarget(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
            database_password='private',
            database_host='postgres',
            database_port=5432,
        )

    def test_target_is_fixed_to_exact_development_identity(self):
        target = self.target()
        self.assertIs(fence.validate_target(target), target)
        self.assertIn('polytopia_dev', fence.confirmation_token(target))

        for field, value in (
            ('environment', 'production'),
            ('database_name', 'polytopia2'),
            ('database_user', 'postgres'),
        ):
            unsafe = SimpleNamespace(**{**target.__dict__, field: value})
            with self.assertRaises(fence.DevelopmentWriterFenceError):
                fence.validate_target(unsafe)

    def test_schema_apply_is_confirmation_and_identity_gated(self):
        target = self.target()
        connection = self.Connection()
        fence.apply_schema(
            connection,
            target,
            confirmation=fence.confirmation_token(target),
            takeover_grace_seconds=0,
        )
        statements = '\n'.join(value[0] for value in connection.statements)
        self.assertIn('CREATE TABLE IF NOT EXISTS', statements)
        self.assertIn('INSERT INTO', statements)
        self.assertEqual(connection.commits, 2)

        wrong = self.Connection()
        wrong.identity = ('wrong', 'wrong')
        with self.assertRaisesRegex(
            fence.DevelopmentWriterFenceError,
            'identity mismatch',
        ):
            fence.apply_schema(
                wrong,
                target,
                confirmation=fence.confirmation_token(target),
                takeover_grace_seconds=0,
            )
        self.assertEqual(wrong.commits, 0)
        self.assertEqual(wrong.rollbacks, 1)

    def test_canonical_evidence_detects_document_change(self):
        document = {'z': 2, 'a': [1]}
        _payload, digest = fence.canonical_document(document)
        entry = {
            'schema_version': fence.FENCE_SCHEMA_VERSION,
            'evidence_key': fence.PERSONA_EVIDENCE_KEY,
            'fence_generation': 7,
            'document_sha256': digest,
            'document': document,
        }
        self.assertTrue(fence.evidence_matches(
            entry,
            evidence_key=fence.PERSONA_EVIDENCE_KEY,
            document=document,
        ))
        self.assertFalse(fence.evidence_matches(
            entry,
            evidence_key=fence.PERSONA_EVIDENCE_KEY,
            document={'z': 3, 'a': [1]},
        ))

    def test_schema_plan_is_connection_free(self):
        profile = SimpleNamespace(**self.target().__dict__)
        with mock.patch.object(
            script, 'load_runtime_profile', return_value=profile,
        ), mock.patch.object(script, '_connection') as connection:
            self.assertEqual(script.main(['plan']), 0)
        connection.assert_not_called()

    def test_supported_out_of_process_writers_contend_on_shared_lock(self):
        required_sources = (
            'bot.py',
            'scripts/manage_dev_fixtures.py',
            'modules/beta_lab_personas.py',
            'modules/beta_wider_setup.py',
            'scripts/manage_guild_configuration_storage.py',
            'scripts/manage_guild_configuration_drafts.py',
            'scripts/manage_guild_configuration_delegation.py',
            'scripts/bootstrap_first_guild_configuration.py',
            'scripts/migrate_player_timezone.py',
        )
        for relative in required_sources:
            with self.subTest(source=relative):
                source = (PROJECT_ROOT / relative).read_text(encoding='utf-8')
                self.assertTrue(
                    'BetaDatabaseWriterLock' in source
                    or '_mutation_writer_scope' in source,
                    f'{relative} does not visibly enter the shared writer fence',
                )

        bootstrap = (
            PROJECT_ROOT / 'scripts/bootstrap_development_database.py'
        ).read_text(encoding='utf-8')
        self.assertIn('BetaDatabaseWriterLock', bootstrap)
        self.assertIn('pg_try_advisory_lock', bootstrap)
        self.assertIn('relation_count != 0', bootstrap)


if __name__ == '__main__':
    unittest.main()
