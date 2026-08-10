#!/usr/bin/env python3
"""Create a read-only reporting snapshot of PolyBot's PostgreSQL data."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Final

import duckdb
import psycopg2
from psycopg2 import sql


EXPORT_FORMAT_VERSION: Final = '1'

# This is intentionally a column allowlist. New production columns must never
# become public merely because they were added to an existing table.
REPORTING_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    'auction': ('id', 'date', 'ongoing', 'r1_done', 'r2_done'),
    'bid': (
        'id', 'auction_id', 'amount', 'player_id', 'bidder_id', 'house_id',
        'time',
    ),
    'discordmember': (
        'id', 'discord_id', 'name', 'elo', 'polytopia_id',
        'polytopia_name', 'elo_max', 'is_banned', 'timezone_offset',
        'name_steam', 'boost_level', 'elo_alltime', 'elo_max_alltime',
        'elo_moonrise', 'elo_max_moonrise', 'trophies',
    ),
    'game': (
        'id', 'is_completed', 'is_confirmed', 'date', 'completed_ts', 'name',
        'guild_id', 'winner_id', 'is_pending', 'host_id', 'is_ranked',
        'size', 'is_mobile', 'league_tier', 'league_season',
        'league_playoff', 'map_type',
    ),
    'gameside': (
        'id', 'game_id', 'squad_id', 'team_id', 'elo_change_squad',
        'elo_change_team', 'sidename', 'size', 'position', 'win_confirmed',
        'elo_change_team_alltime', 'team_elo_after_game',
        'team_elo_after_game_alltime',
    ),
    'house': ('id', 'emoji', 'league_tokens', 'name'),
    'lineup': (
        'id', 'game_id', 'gameside_id', 'player_id', 'elo_change_player',
        'elo_change_discordmember', 'elo_after_game', 'tribe_id',
        'elo_after_game_global', 'elo_change_player_alltime',
        'elo_change_discordmember_alltime', 'elo_after_game_alltime',
        'elo_after_game_global_alltime', 'elo_change_player_moonrise',
        'elo_change_discordmember_moonrise', 'elo_after_game_moonrise',
        'elo_after_game_global_moonrise',
    ),
    'player': (
        'id', 'discord_member_id', 'guild_id', 'nick', 'name', 'team_id',
        'elo', 'elo_max', 'is_banned', 'elo_alltime', 'elo_max_alltime',
        'elo_moonrise', 'elo_max_moonrise', 'trophies',
    ),
    'playerhousepreference': ('id', 'player_id', 'house_id'),
    'squad': ('id', 'elo', 'guild_id', 'name'),
    'squadmember': ('id', 'player_id', 'squad_id'),
    'team': (
        'id', 'name', 'elo', 'emoji', 'guild_id', 'is_hidden', 'elo_alltime',
        'pro_league', 'external_server', 'is_archived', 'house_id',
        'league_tier',
    ),
    'tribe': ('id', 'name', 'emoji'),
}

FORBIDDEN_SOURCE_TABLES: Final = frozenset({
    'apiapplication',
    'configuration',
    'gamelog',
    'team_server_broadcast_message',
})

POSTGRES_TO_DUCKDB: Final[dict[tuple[str, str], str]] = {
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


def quote_duckdb_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def duckdb_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def postgres_type_to_duckdb(data_type: str, udt_name: str) -> str:
    try:
        return POSTGRES_TO_DUCKDB[(data_type, udt_name)]
    except KeyError as exc:
        raise ValueError(
            f'unsupported PostgreSQL type {data_type!r} ({udt_name!r})'
        ) from exc


def get_column_types(cursor, table: str) -> list[tuple[str, str, str]]:
    requested = REPORTING_COLUMNS[table]
    cursor.execute(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = ANY(%s)
        """,
        (table, list(requested)),
    )
    found = {name: (data_type, udt_name)
             for name, data_type, udt_name in cursor.fetchall()}
    missing = [name for name in requested if name not in found]
    if missing:
        raise RuntimeError(
            f'{table} is missing allowlisted columns: {", ".join(missing)}'
        )
    return [(name, *found[name]) for name in requested]


def build_copy_select(
        table: str,
        columns: list[tuple[str, str, str]],
) -> sql.Composed:
    expressions = []
    for name, data_type, _udt_name in columns:
        identifier = sql.Identifier(name)
        if data_type == 'ARRAY':
            expressions.append(
                sql.SQL('array_to_json({})::text AS {}').format(
                    identifier,
                    identifier,
                )
            )
        else:
            expressions.append(identifier)
    return sql.SQL('SELECT {} FROM {}.{}').format(
        sql.SQL(', ').join(expressions),
        sql.Identifier('public'),
        sql.Identifier(table),
    )


def create_duckdb_table(
        connection: duckdb.DuckDBPyConnection,
        table: str,
        columns: list[tuple[str, str, str]],
) -> None:
    definitions = ', '.join(
        f'{quote_duckdb_identifier(name)} '
        f'{postgres_type_to_duckdb(data_type, udt_name)}'
        for name, data_type, udt_name in columns
    )
    connection.execute(
        f'CREATE TABLE {quote_duckdb_identifier(table)} ({definitions})'
    )


def copy_table(
        pg_cursor,
        duck_connection: duckdb.DuckDBPyConnection,
        temporary_directory: Path,
        table: str,
        columns: list[tuple[str, str, str]],
) -> int:
    csv_path = temporary_directory / f'{table}.csv'
    select_query = build_copy_select(table, columns)
    copy_query = sql.SQL(
        "COPY ({}) TO STDOUT WITH (FORMAT CSV, NULL '\\N')"
    ).format(select_query)
    with csv_path.open('w', encoding='utf-8', newline='') as output:
        pg_cursor.copy_expert(copy_query.as_string(pg_cursor), output)

    create_duckdb_table(duck_connection, table, columns)
    duck_connection.execute(
        f'COPY {quote_duckdb_identifier(table)} '
        f'FROM {duckdb_string_literal(str(csv_path))} '
        "(FORMAT CSV, HEADER FALSE, DELIMITER ',', QUOTE '\"', "
        "ESCAPE '\"', NULL '\\N', AUTO_DETECT FALSE)"
    )
    return duck_connection.execute(
        f'SELECT count(*) FROM {quote_duckdb_identifier(table)}'
    ).fetchone()[0]


def validate_artifact(
        path: Path,
        expected_counts: dict[str, int],
) -> None:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        actual_tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                  AND table_type = 'BASE TABLE'
                """
            ).fetchall()
        }
        expected_tables = set(expected_counts) | {
            'reporting_metadata', 'reporting_row_counts',
        }
        if actual_tables != expected_tables:
            raise RuntimeError(
                f'unexpected artifact tables: {sorted(actual_tables)}'
            )
        if actual_tables & FORBIDDEN_SOURCE_TABLES:
            raise RuntimeError('artifact contains a forbidden source table')

        recorded_counts = dict(connection.execute(
            'SELECT table_name, row_count FROM reporting_row_counts'
        ).fetchall())
        if recorded_counts != expected_counts:
            raise RuntimeError('recorded row counts do not match export counts')

        for table, expected in expected_counts.items():
            actual = connection.execute(
                f'SELECT count(*) FROM {quote_duckdb_identifier(table)}'
            ).fetchone()[0]
            if actual != expected:
                raise RuntimeError(
                    f'{table} has {actual} rows; expected {expected}'
                )
        connection.execute('PRAGMA database_size').fetchall()
    finally:
        connection.close()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def export_reporting_database(
        output_path: Path,
        connect_kwargs: dict[str, object],
        replace: bool,
        lock_path: Path | None = None,
) -> tuple[dict[str, int], str]:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not replace:
        raise FileExistsError(
            f'{output_path} exists; pass --replace to atomically replace it'
        )

    if lock_path is None:
        lock_path = output_path.with_suffix(output_path.suffix + '.lock')
    else:
        lock_path = lock_path.resolve()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('w', encoding='utf-8') as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f'another reporting export holds {lock_path}'
            ) from exc

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{output_path.name}.tmp.',
            dir=output_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        temporary_path.unlink()
        try:
            counts: dict[str, int] = {}
            with tempfile.TemporaryDirectory(
                    prefix='polybot-reporting-'
            ) as temp_directory:
                with psycopg2.connect(**connect_kwargs) as pg_connection:
                    pg_connection.set_session(
                        isolation_level='REPEATABLE READ',
                        readonly=True,
                    )
                    with pg_connection.cursor() as pg_cursor:
                        pg_cursor.execute(
                            """
                            SELECT transaction_timestamp() AT TIME ZONE 'UTC',
                                   current_setting('server_version')
                            """
                        )
                        generated_at, postgres_version = pg_cursor.fetchone()

                        duck_connection = duckdb.connect(str(temporary_path))
                        try:
                            for table in REPORTING_COLUMNS:
                                column_types = get_column_types(
                                    pg_cursor,
                                    table,
                                )
                                copied = copy_table(
                                    pg_cursor,
                                    duck_connection,
                                    Path(temp_directory),
                                    table,
                                    column_types,
                                )
                                pg_cursor.execute(
                                    sql.SQL(
                                        'SELECT count(*) FROM {}.{}'
                                    ).format(
                                        sql.Identifier('public'),
                                        sql.Identifier(table),
                                    )
                                )
                                source_count = pg_cursor.fetchone()[0]
                                if copied != source_count:
                                    raise RuntimeError(
                                        f'{table} copied {copied} rows; '
                                        f'source snapshot has {source_count}'
                                    )
                                counts[table] = copied

                            duck_connection.execute(
                                """
                                CREATE TABLE reporting_metadata (
                                    key VARCHAR PRIMARY KEY,
                                    value VARCHAR NOT NULL
                                )
                                """
                            )
                            duck_connection.executemany(
                                'INSERT INTO reporting_metadata VALUES (?, ?)',
                                [
                                    ('export_format_version',
                                     EXPORT_FORMAT_VERSION),
                                    ('duckdb_version', duckdb.__version__),
                                    ('generated_at_utc',
                                     generated_at.isoformat() + 'Z'),
                                    ('postgres_version', postgres_version),
                                ],
                            )
                            duck_connection.execute(
                                """
                                CREATE TABLE reporting_row_counts (
                                    table_name VARCHAR PRIMARY KEY,
                                    row_count BIGINT NOT NULL
                                )
                                """
                            )
                            duck_connection.executemany(
                                'INSERT INTO reporting_row_counts VALUES (?, ?)',
                                list(counts.items()),
                            )
                            duck_connection.execute('CHECKPOINT')
                        finally:
                            duck_connection.close()

            os.chmod(temporary_path, 0o600)
            validate_artifact(temporary_path, counts)
            digest = sha256_file(temporary_path)
            os.replace(temporary_path, output_path)
            return counts, digest
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Create an atomic DuckDB reporting snapshot.',
    )
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--database', default='polytopia2')
    parser.add_argument('--user', default=None)
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument(
        '--lock-file',
        type=Path,
        help=(
            'Persistent lock file path. Defaults beside the output; use a '
            'private path when the output directory is published.'
        ),
    )
    parser.add_argument(
        '--password-env',
        help='Name of an environment variable containing the DB password.',
    )
    parser.add_argument('--replace', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    connect_kwargs: dict[str, object] = {
        'dbname': args.database,
        'application_name': 'polybot-reporting-export',
    }
    for key in ('user', 'host', 'port'):
        value = getattr(args, key)
        if value is not None:
            connect_kwargs[key] = value
    if args.password_env:
        try:
            connect_kwargs['password'] = os.environ[args.password_env]
        except KeyError as exc:
            raise SystemExit(
                f'environment variable {args.password_env!r} is not set'
            ) from exc

    counts, digest = export_reporting_database(
        args.output,
        connect_kwargs,
        args.replace,
        args.lock_file,
    )
    print(f'Reporting export successful: {args.output.resolve()}')
    print(f'  tables: {len(counts)}')
    print(f'  rows:   {sum(counts.values())}')
    print(f'  sha256: {digest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
