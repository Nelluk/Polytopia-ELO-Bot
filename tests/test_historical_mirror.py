from __future__ import annotations

from contextlib import contextmanager
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

    def test_invariant_failure_reports_source_rows(self):
        database = FakeDatabase()
        with mock.patch.object(mirror, '_present_tables', return_value=('configuration',)), \
             mock.patch.object(mirror, '_count', return_value=1):
            with self.assertRaisesRegex(mirror.HistoricalMirrorError, 'source rows remain'):
                mirror._verify_in_transaction(database, plan(tables=('configuration',)),
                                               mirror.TARGET_GUILD_ID)

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
                profile(), 'HISTORICAL MIRROR APPLY ' + 'e' * 64 + ' - - -',
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


if __name__ == '__main__':
    unittest.main()
