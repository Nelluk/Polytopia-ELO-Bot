import hashlib
from pathlib import Path
import tempfile
import unittest

import duckdb

from scripts import export_reporting_duckdb as reporting


class ReportingExportTests(unittest.TestCase):
    def test_reporting_schema_is_an_explicit_safe_allowlist(self):
        self.assertTrue(reporting.REPORTING_COLUMNS)
        self.assertFalse(
            set(reporting.REPORTING_COLUMNS)
            & reporting.FORBIDDEN_SOURCE_TABLES
        )
        exported_columns = {
            column
            for columns in reporting.REPORTING_COLUMNS.values()
            for column in columns
        }
        self.assertNotIn('token', exported_columns)
        self.assertNotIn('message', exported_columns)
        self.assertNotIn('notes', exported_columns)
        self.assertNotIn('announcement_message', exported_columns)

    def test_supported_postgresql_types_have_deterministic_duckdb_types(self):
        expected = {
            ('smallint', 'int2'): 'SMALLINT',
            ('integer', 'int4'): 'INTEGER',
            ('bigint', 'int8'): 'BIGINT',
            ('boolean', 'bool'): 'BOOLEAN',
            ('text', 'text'): 'VARCHAR',
            ('character varying', 'varchar'): 'VARCHAR',
            ('timestamp without time zone', 'timestamp'): 'TIMESTAMP',
            ('date', 'date'): 'DATE',
            ('jsonb', 'jsonb'): 'JSON',
            ('ARRAY', '_int2'): 'SMALLINT[]',
        }
        for postgres_type, duckdb_type in expected.items():
            with self.subTest(postgres_type=postgres_type):
                self.assertEqual(
                    reporting.postgres_type_to_duckdb(*postgres_type),
                    duckdb_type,
                )

        with self.assertRaisesRegex(ValueError, 'unsupported PostgreSQL type'):
            reporting.postgres_type_to_duckdb('numeric', 'numeric')

    def test_validation_reopens_complete_artifact_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reporting.duckdb'
            connection = duckdb.connect(str(path))
            try:
                for table in reporting.REPORTING_COLUMNS:
                    connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
                connection.execute(
                    'CREATE TABLE reporting_metadata '
                    '(key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)'
                )
                connection.execute(
                    'CREATE TABLE reporting_row_counts '
                    '(table_name VARCHAR PRIMARY KEY, '
                    'row_count BIGINT NOT NULL)'
                )
                connection.executemany(
                    'INSERT INTO reporting_row_counts VALUES (?, 0)',
                    [(table,) for table in reporting.REPORTING_COLUMNS],
                )
                connection.execute('CHECKPOINT')
            finally:
                connection.close()

            expected = {
                table: 0 for table in reporting.REPORTING_COLUMNS
            }
            reporting.validate_artifact(path, expected)

            connection = duckdb.connect(str(path))
            try:
                connection.execute('INSERT INTO auction VALUES (1)')
                connection.execute('CHECKPOINT')
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, 'auction has 1 rows'):
                reporting.validate_artifact(path, expected)

    def test_sha256_file_streams_expected_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample'
            path.write_bytes(b'PolyBot reporting snapshot')
            self.assertEqual(
                reporting.sha256_file(path),
                hashlib.sha256(b'PolyBot reporting snapshot').hexdigest(),
            )

    def test_postgresql_csv_quote_convention_loads_in_duckdb(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / 'quoted.csv'
            csv_path.write_text(
                '35863,"Cold Throne ""Who Will Get It Worm?"\n',
                encoding='utf-8',
            )
            connection = duckdb.connect(':memory:')
            try:
                connection.execute(
                    'CREATE TABLE game (id INTEGER, name VARCHAR)'
                )
                connection.execute(
                    f"COPY game FROM "
                    f"{reporting.duckdb_string_literal(str(csv_path))} "
                    "(FORMAT CSV, HEADER FALSE, DELIMITER ',', QUOTE '\"', "
                    "ESCAPE '\"', NULL '\\N', AUTO_DETECT FALSE)"
                )
                self.assertEqual(
                    connection.execute(
                        'SELECT id, name FROM game'
                    ).fetchall(),
                    [(35863, 'Cold Throne "Who Will Get It Worm?')],
                )
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
