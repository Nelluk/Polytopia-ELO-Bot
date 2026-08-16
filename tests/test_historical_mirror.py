from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import historical_mirror as mirror


class FakeDatabase:
    def __init__(self, fail_on: int | None = None):
        self.statements = []
        self.fail_on = fail_on

    @contextmanager
    def connection_context(self):
        yield self

    @contextmanager
    def atomic(self):
        committed = False
        try:
            yield self
            committed = True
        finally:
            self.committed = committed

    def execute_sql(self, query, params=()):
        self.statements.append((query, tuple(params)))
        if self.fail_on is not None and len(self.statements) == self.fail_on:
            raise RuntimeError('injected database failure')
        return SimpleNamespace(fetchone=lambda: (1,), fetchall=lambda: [])


def profile():
    return SimpleNamespace(allowed_guild_ids=(mirror.TARGET_GUILD_ID,))


def plan(*, tables=mirror.DIRECT_TABLES):
    config = mirror.ConfigurationState(
        'ready', tuple(), tuple(), tuple(), True,
    )
    counts = tuple((table, 2) for table in tables)
    return mirror.MirrorPlan(
        checkpoint='a' * 40,
        present_tables=tuple(tables),
        source_counts=counts,
        target_counts=tuple((table, 3) for table in tables),
        parking_counts=tuple((table, 0) for table in tables),
        scrub_counts=tuple(),
        schema_fingerprint='b' * 64,
        configuration=config,
        digest='c' * 64,
    )


class HistoricalMirrorTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict('os.environ', {'POLYBOT_ENV': 'development'}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_confirmation_is_deterministic_and_round_trips_counts(self):
        first = plan()
        second = plan()
        self.assertEqual(first.confirmation, second.confirmation)
        digest, source, target, parking = mirror.parse_confirmation(first.confirmation)
        self.assertEqual(digest, first.digest)
        self.assertEqual(source, first.source_counts)
        self.assertEqual(target, first.target_counts)
        self.assertEqual(parking, first.parking_counts)

    def test_confirmation_requires_exact_live_table_sequence(self):
        token = plan().confirmation
        with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'tables do not match'):
            mirror.parse_confirmation(token, expected_tables=('configuration',))
        self.assertEqual(
            mirror.parse_confirmation(token, expected_tables=mirror.DIRECT_TABLES)[0],
            'c' * 64)

    def test_gamelog_must_be_empty_when_table_exists(self):
        with mock.patch.object(mirror, '_identity'), \
             mock.patch.object(mirror, '_present_tables', return_value=mirror.DIRECT_TABLES), \
             mock.patch.object(mirror, '_configuration_state', return_value=mirror.ConfigurationState('not_ready', (), (), (), False)), \
             mock.patch.object(mirror, '_schema_fingerprint', return_value='d' * 64), \
             mock.patch.object(mirror, '_count', side_effect=lambda _db, query, params=():
                              1 if 'FROM gamelog' in query else 0):
            with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'gamelog must be empty'):
                mirror._snapshot(FakeDatabase(), 'a' * 40, mirror.TARGET_GUILD_ID)

    def test_empty_source_graph_refuses_to_park_beta(self):
        with mock.patch.object(mirror, '_identity'), \
             mock.patch.object(mirror, '_present_tables', return_value=mirror.DIRECT_TABLES[:-1]), \
             mock.patch.object(mirror, '_configuration_state', return_value=mirror.ConfigurationState('not_ready', (), (), (), False)), \
             mock.patch.object(mirror, '_schema_fingerprint', return_value='d' * 64), \
             mock.patch.object(mirror, '_count', return_value=0):
            with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'no games'):
                mirror._snapshot(FakeDatabase(), 'a' * 40, mirror.TARGET_GUILD_ID)

    def test_post_remap_snapshot_allows_zero_source_graph(self):
        with mock.patch.object(mirror, '_identity'), \
             mock.patch.object(mirror, '_present_tables', return_value=mirror.DIRECT_TABLES[:-1]), \
             mock.patch.object(mirror, '_configuration_state', return_value=mirror.ConfigurationState('not_ready', (), (), (), False)), \
             mock.patch.object(mirror, '_schema_fingerprint', return_value='d' * 64), \
             mock.patch.object(mirror, '_count', return_value=0):
            result = mirror._snapshot(
                FakeDatabase(), 'a' * 40, mirror.TARGET_GUILD_ID,
                allow_parking=True, require_source=False)
        self.assertEqual(dict(result.source_counts)['game'], 0)

    def test_schema_fingerprint_includes_constraint_topology(self):
        queries = []

        def rows(_db, query, _params=()):
            queries.append(query)
            if 'table_constraints' in query:
                return [('FOREIGN KEY', 'game_host_fk', 'host_id', 'player', 'id')]
            return [('id', 'bigint', 'NO')]

        with mock.patch.object(mirror, '_table_exists', return_value=True), \
             mock.patch.object(mirror, '_rows', side_effect=rows):
            first = mirror._schema_fingerprint(FakeDatabase(), ('game',))
            second = mirror._schema_fingerprint(FakeDatabase(), ('game',))
        self.assertEqual(first, second)
        self.assertTrue(any('table_constraints' in query for query in queries))

    def test_parking_collision_fails_before_writes(self):
        with mock.patch.object(mirror, '_identity'), \
             mock.patch.object(mirror, '_present_tables', return_value=mirror.DIRECT_TABLES), \
             mock.patch.object(mirror, '_configuration_state', return_value=mirror.ConfigurationState('not_ready', (), (), (), False)), \
             mock.patch.object(mirror, '_schema_fingerprint', return_value='d' * 64), \
             mock.patch.object(mirror, '_count', side_effect=lambda _db, _query, params=():
                              1 if params and params[0] == mirror.PARKING_GUILD_ID else 0):
            with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'Parking sentinel'):
                mirror._snapshot(FakeDatabase(), 'a' * 40, mirror.TARGET_GUILD_ID)

    def test_write_parks_every_direct_table_before_remap(self):
        database = FakeDatabase()
        mirror._write(database, plan(), mirror.TARGET_GUILD_ID)
        updates = [statement for statement in database.statements if statement[0].startswith('UPDATE "')]
        self.assertEqual(len(updates), len(mirror.DIRECT_TABLES) * 2)
        self.assertTrue(all(statement[1] == (mirror.PARKING_GUILD_ID, mirror.TARGET_GUILD_ID)
                            for statement in updates[:len(mirror.DIRECT_TABLES)]))
        self.assertTrue(all(statement[1] == (mirror.TARGET_GUILD_ID, mirror.SOURCE_GUILD_ID)
                            for statement in updates[len(mirror.DIRECT_TABLES):]))

    def test_scrub_scope_is_target_graph_only(self):
        database = FakeDatabase()
        mirror._write(database, plan(tables=mirror.DIRECT_TABLES[:-1]), mirror.TARGET_GUILD_ID)
        sql = '\n'.join(statement[0] for statement in database.statements)
        self.assertIn('announcement_message = NULL', sql)
        self.assertIn('required_role_id = NULL', sql)
        self.assertIn('external_server = NULL', sql)
        self.assertIn('DELETE FROM apiapplication', sql)
        self.assertIn('polychamps_draft', sql)

    def test_profile_gate_refuses_non_development(self):
        with mock.patch.dict('os.environ', {'POLYBOT_ENV': 'production'}, clear=False), \
             self.assertRaisesRegex(mirror.HistoricalMirrorError, 'exactly development'):
            mirror.validate_profile(profile())

    def test_cli_boundary_refuses_dirty_tracked_checkout(self):
        from scripts import manage_historical_mirror as cli
        with mock.patch.object(cli.beta_operations, 'assert_clean_checkout',
                               side_effect=cli.beta_operations.BetaOperationsError('tracked changes')):
            with self.assertRaisesRegex(cli.beta_operations.BetaOperationsError, 'tracked changes'):
                cli._profile()

    def test_cli_boundary_refuses_dirty_untracked_checkout(self):
        from scripts import manage_historical_mirror as cli
        with mock.patch.object(cli.beta_operations, 'assert_clean_checkout',
                               side_effect=cli.beta_operations.BetaOperationsError('untracked files')):
            with self.assertRaisesRegex(cli.beta_operations.BetaOperationsError, 'untracked files'):
                cli._profile()

    def test_invariant_failure_reports_source_rows(self):
        database = FakeDatabase()
        with mock.patch.object(mirror, '_present_tables', return_value=('configuration',)), \
             mock.patch.object(mirror, '_count', return_value=1):
            with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'source rows remain'):
                mirror._verify_in_transaction(database, plan(tables=('configuration',)),
                                               mirror.TARGET_GUILD_ID)

    def test_verify_requires_complete_active_configuration(self):
        database = FakeDatabase()
        zero = replace(plan(tables=('configuration',)),
                       source_counts=(('configuration', 0),),
                       target_counts=(('configuration', 0),),
                       parking_counts=(('configuration', 0),))
        with mock.patch.object(mirror, '_present_tables', return_value=('configuration',)), \
             mock.patch.object(mirror, '_count', return_value=0), \
             mock.patch.object(mirror, '_diagnostics', return_value=[]), \
             mock.patch.object(mirror, '_configuration_state', return_value=mirror.ConfigurationState('not_ready', (), (), (), False)):
            with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'not ready'):
                mirror._verify_in_transaction(database, zero, mirror.TARGET_GUILD_ID,
                                               require_configuration=True)

    def test_partial_modern_configuration_topology_refuses(self):
        database = FakeDatabase()
        with mock.patch.object(mirror, '_table_exists', side_effect=lambda _db, table: table == mirror.MODERN_CONFIGURATION_TABLES[0]):
            with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'partially present'):
                mirror._configuration_state(database, mirror.TARGET_GUILD_ID)

    def test_absent_gamelog_is_not_written(self):
        database = FakeDatabase()
        mirror._write(database, plan(tables=mirror.DIRECT_TABLES[:-1]), mirror.TARGET_GUILD_ID)
        self.assertFalse(any('gamelog' in statement[0] for statement in database.statements))

    def test_apply_rejects_stale_confirmation_before_write(self):
        database = FakeDatabase()
        current = plan()
        with mock.patch.object(mirror, 'validate_profile', return_value=mirror.TARGET_GUILD_ID), \
             mock.patch.object(mirror, '_snapshot', return_value=current), \
             mock.patch.object(mirror, 'verify_database'), \
             self.assertRaisesRegex(mirror.HistoricalMirrorError, 'stale'):
            mirror.apply_database(
                profile(), current.confirmation.replace('c' * 64, 'e' * 64),
                checkpoint='a' * 40, database_factory=lambda _profile: database,
                writer_lock_factory=lambda _profile: mock.patch('builtins.open'),
            )
        self.assertEqual(database.statements, [])

    def test_transaction_failure_is_not_reported_as_commit(self):
        database = FakeDatabase(fail_on=1)
        current = plan()
        lock = mock.MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = False
        with mock.patch.object(mirror, 'validate_profile', return_value=mirror.TARGET_GUILD_ID), \
             mock.patch.object(mirror, '_snapshot', return_value=current), \
             mock.patch.object(mirror, '_write', side_effect=RuntimeError('boom')), \
             mock.patch.object(mirror, 'verify_database'), \
             mock.patch.object(mirror.beta_database_writer_lock, 'BetaDatabaseWriterLock', return_value=lock), \
             self.assertRaisesRegex(mirror.HistoricalMirrorError, 'rolled back'):
            mirror.apply_database(
                profile(), current.confirmation, checkpoint='a' * 40,
                database_factory=lambda _profile: database,
            )
        self.assertFalse(database.committed)

    def test_post_commit_failure_requires_reconciliation(self):
        database = FakeDatabase()
        current = plan()
        lock = mock.MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = False
        with mock.patch.object(mirror, 'validate_profile', return_value=mirror.TARGET_GUILD_ID), \
             mock.patch.object(mirror, '_snapshot', return_value=current), \
             mock.patch.object(mirror, '_write'), \
             mock.patch.object(mirror, '_verify_in_transaction'), \
             mock.patch.object(mirror, 'verify_database', side_effect=mirror.HistoricalMirrorError('bad')), \
             mock.patch.object(mirror.beta_database_writer_lock, 'BetaDatabaseWriterLock', return_value=lock), \
             self.assertRaises(mirror.HistoricalMirrorReconciliationRequired):
            mirror.apply_database(
                profile(), current.confirmation, checkpoint='a' * 40,
                database_factory=lambda _profile: database,
            )
        self.assertTrue(database.committed)

    def test_writer_lock_acquisition_failure_is_bounded(self):
        with mock.patch.object(mirror, 'validate_profile', return_value=mirror.TARGET_GUILD_ID), \
             self.assertRaisesRegex(mirror.HistoricalMirrorError, 'writer lock'):
            mirror.apply_database(
                profile(), plan().confirmation, checkpoint='a' * 40,
                database_factory=lambda _profile: FakeDatabase(),
                writer_lock_factory=lambda _profile: (_ for _ in ()).throw(
                    mirror.beta_database_writer_lock.BetaDatabaseWriterLockError('no lock')),
            )

    def test_post_commit_cancellation_requires_reconciliation_and_releases_lock(self):
        database = FakeDatabase()
        current = plan()
        lock = mock.MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = False
        with mock.patch.object(mirror, 'validate_profile', return_value=mirror.TARGET_GUILD_ID), \
             mock.patch.object(mirror, '_snapshot', return_value=current), \
             mock.patch.object(mirror, '_write'), \
             mock.patch.object(mirror, '_verify_in_transaction'), \
             mock.patch.object(mirror, 'verify_database', side_effect=KeyboardInterrupt()), \
             mock.patch.object(mirror.beta_database_writer_lock, 'BetaDatabaseWriterLock', return_value=lock), \
             self.assertRaises(mirror.HistoricalMirrorReconciliationRequired):
            mirror.apply_database(profile(), current.confirmation, checkpoint='a' * 40,
                                  database_factory=lambda _profile: database)
        lock.__exit__.assert_called_once()

    def test_post_commit_lock_release_failure_requires_reconciliation(self):
        database = FakeDatabase()
        current = plan()

        class ReleaseFailureLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                raise RuntimeError('release failed')

        with mock.patch.object(mirror, 'validate_profile', return_value=mirror.TARGET_GUILD_ID), \
             mock.patch.object(mirror, '_snapshot', return_value=current), \
             mock.patch.object(mirror, '_write'), \
             mock.patch.object(mirror, '_verify_in_transaction'), \
             mock.patch.object(mirror, 'verify_database'), \
             mock.patch.object(mirror.beta_database_writer_lock, 'BetaDatabaseWriterLock', return_value=ReleaseFailureLock()), \
             self.assertRaises(mirror.HistoricalMirrorReconciliationRequired):
            mirror.apply_database(profile(), current.confirmation, checkpoint='a' * 40,
                                  database_factory=lambda _profile: database)


if __name__ == '__main__':
    unittest.main()
