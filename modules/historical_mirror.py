"""Development-only PolyChampions historical guild remap.

This is intentionally a small, model-free SQL tool.  The production partial
dump is restored by the operator first; this module only parks the existing
beta graph, remaps the PolyChampions direct guild rows, scrubs Discord object
references, and proves the resulting graph.  It never opens a production
connection and it never rewrites global identities or indirect rows.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from typing import Any, Callable, Iterable

from modules import beta_database_writer_lock, beta_operations, beta_readiness


SOURCE_GUILD_ID = 447883341463814144
TARGET_GUILD_ID = 478571892832206869
# Deliberately outside the Discord snowflake range and below BIGINT max.
PARKING_GUILD_ID = 9223372036854770000

DIRECT_TABLES = (
    'configuration', 'team', 'player', 'game', 'squad', 'gamelog',
)
REQUIRED_DIRECT_TABLES = DIRECT_TABLES[:-1]
MODERN_CONFIGURATION_TABLES = (
    'guild_configuration_registry',
    'guild_configuration_revision',
    'guild_configuration_audit',
    'guild_configuration_draft',
    'guild_configuration_delegation',
)
MAX_DIAGNOSTICS = 32
_HEX = set('0123456789abcdef')


class HistoricalMirrorError(RuntimeError):
    """The historical remap was refused or failed closed."""


class HistoricalMirrorReconciliationRequired(HistoricalMirrorError):
    """The transaction committed but a post-commit verification needs review."""


@dataclass(frozen=True)
class ConfigurationState:
    status: str
    present_tables: tuple[str, ...]
    row_counts: tuple[tuple[str, int], ...]
    non_target_rows: tuple[tuple[str, int], ...]
    active_target: bool


@dataclass(frozen=True)
class MirrorPlan:
    checkpoint: str
    present_tables: tuple[str, ...]
    source_counts: tuple[tuple[str, int], ...]
    target_counts: tuple[tuple[str, int], ...]
    parking_counts: tuple[tuple[str, int], ...]
    scrub_counts: tuple[tuple[str, int], ...]
    schema_fingerprint: str
    configuration: ConfigurationState
    digest: str

    @property
    def confirmation(self) -> str:
        return (
            f'HISTORICAL MIRROR APPLY {self.digest} '
            f'{_counts_token(self.source_counts)} '
            f'{_counts_token(self.target_counts)} '
            f'{_counts_token(self.parking_counts)}'
        )


@dataclass(frozen=True)
class MirrorResult:
    plan: MirrorPlan
    committed: bool
    verification: str


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _counts_token(counts: tuple[tuple[str, int], ...]) -> str:
    return ','.join(f'{table}={count}' for table, count in counts) or '-'


def _parse_counts(value: str) -> tuple[tuple[str, int], ...]:
    if value == '-':
        return ()
    result = []
    for item in value.split(','):
        try:
            table, raw_count = item.split('=', 1)
            count = int(raw_count)
        except (ValueError, TypeError):
            raise HistoricalMirrorError('Malformed confirmation counts.')
        if table not in DIRECT_TABLES or count < 0:
            raise HistoricalMirrorError('Malformed confirmation counts.')
        result.append((table, count))
    if len({table for table, _ in result}) != len(result):
        raise HistoricalMirrorError('Duplicate confirmation count table.')
    return tuple(result)


def parse_confirmation(value: str, *, expected_tables: tuple[str, ...] | None = None) -> tuple[str, tuple[tuple[str, int], ...],
                                             tuple[tuple[str, int], ...],
                                             tuple[tuple[str, int], ...]]:
    parts = str(value or '').split()
    if len(parts) != 7 or parts[:3] != ['HISTORICAL', 'MIRROR', 'APPLY']:
        raise HistoricalMirrorError('Historical mirror confirmation is invalid.')
    digest = parts[3]
    if len(digest) != 64 or any(char not in _HEX for char in digest):
        raise HistoricalMirrorError('Historical mirror confirmation digest is invalid.')
    source = _parse_counts(parts[4])
    target = _parse_counts(parts[5])
    parking = _parse_counts(parts[6])
    if expected_tables is not None:
        for label, counts in (('source', source), ('target', target), ('parking', parking)):
            if tuple(table for table, _ in counts) != tuple(expected_tables):
                raise HistoricalMirrorError(
                    f'{label} confirmation tables do not match the live direct-table sequence.')
    return digest, source, target, parking


def validate_profile(profile: Any, *, target_guild_id: int = TARGET_GUILD_ID) -> int:
    """Validate the complete profile before importing any DB-backed module."""

    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise HistoricalMirrorError('POLYBOT_ENV must be exactly development.')
    try:
        beta_readiness.validate_database_profile(profile, target_guild_id)
        beta_operations.assert_beta_profile(profile)
    except Exception as exc:
        raise HistoricalMirrorError(str(exc)) from exc
    allowed = tuple(int(value) for value in profile.allowed_guild_ids)
    if allowed != (int(target_guild_id),):
        raise HistoricalMirrorError('The profile must contain one exact target guild.')
    if int(target_guild_id) == SOURCE_GUILD_ID:
        raise HistoricalMirrorError('Source and target guilds must differ.')
    if int(PARKING_GUILD_ID) <= 0 or int(PARKING_GUILD_ID) >= 9223372036854775807:
        raise HistoricalMirrorError('The parking guild sentinel is not a BIGINT.')
    return int(target_guild_id)


def _database_factory(profile: Any) -> Any:
    from modules import models
    return models.db


def _row(database: Any, query: str, params: Iterable[Any] = ()) -> tuple[Any, ...]:
    cursor = database.execute_sql(query, tuple(params))
    value = cursor.fetchone()
    if value is None:
        raise HistoricalMirrorError('A bounded database query returned no row.')
    return tuple(value)


def _rows(database: Any, query: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
    cursor = database.execute_sql(query, tuple(params))
    return [tuple(value) for value in cursor.fetchall()]


def _count(database: Any, query: str, params: Iterable[Any] = ()) -> int:
    value = _row(database, query, params)
    try:
        count = int(value[0])
    except (IndexError, TypeError, ValueError) as exc:
        raise HistoricalMirrorError('A database count was not an integer.') from exc
    if count < 0:
        raise HistoricalMirrorError('A database count was negative.')
    return count


def _table_exists(database: Any, table: str) -> bool:
    value = _row(database, 'SELECT to_regclass(%s)', (f'public.{table}',))[0]
    return value is not None


def _identity(database: Any) -> None:
    database_name, database_user = _row(
        database, 'SELECT current_database(), current_user')
    beta_readiness.validate_live_database_identity(
        str(database_name), str(database_user))


def _present_tables(database: Any) -> tuple[str, ...]:
    present = tuple(table for table in DIRECT_TABLES if _table_exists(database, table))
    missing = tuple(table for table in REQUIRED_DIRECT_TABLES if table not in present)
    if missing:
        raise HistoricalMirrorError(
            'Required direct tables are missing: ' + ', '.join(missing))
    return present


def _configuration_state(database: Any, target: int) -> ConfigurationState:
    present = tuple(table for table in MODERN_CONFIGURATION_TABLES
                    if _table_exists(database, table))
    if not present:
        return ConfigurationState('not_ready', (), (), (), False)
    if len(present) != len(MODERN_CONFIGURATION_TABLES):
        raise HistoricalMirrorError(
            'Modern guild configuration schema is partially present: '
            + ', '.join(present))
    counts = tuple((table, _count(database,
        f'SELECT COUNT(*) FROM "{table}"')) for table in MODERN_CONFIGURATION_TABLES)
    non_target = tuple((table, _count(database,
        f'SELECT COUNT(*) FROM "{table}" WHERE guild_id <> %s', (target,)))
                       for table in MODERN_CONFIGURATION_TABLES)
    active_rows = _rows(database, (
        'SELECT guild_id, enrollment_state, active_revision, generation '
        'FROM "guild_configuration_registry" ORDER BY guild_id LIMIT 33'))
    active_target = (
        len(active_rows) == 1 and int(active_rows[0][0]) == target
        and str(active_rows[0][1]) == 'active'
        and active_rows[0][2] is not None and int(active_rows[0][3]) > 0)
    return ConfigurationState(
        'ready', present, counts, non_target, active_target)


def _schema_fingerprint(database: Any, tables: tuple[str, ...]) -> str:
    columns = []
    for table in (*tables, 'gameside', 'lineup', 'squadmember',
                  'team_server_broadcast_message', 'apiapplication'):
        if not _table_exists(database, table):
            columns.append((table, None))
            continue
        rows = _rows(database, (
            'SELECT column_name, data_type, is_nullable '
            'FROM information_schema.columns '
            'WHERE table_schema = current_schema() AND table_name = %s '
            'ORDER BY ordinal_position LIMIT 128'), (table,))
        constraints = _rows(database, (
            'SELECT tc.constraint_type, tc.constraint_name, kcu.column_name, '
            'ccu.table_name, ccu.column_name '
            'FROM information_schema.table_constraints tc '
            'LEFT JOIN information_schema.key_column_usage kcu '
            'ON kcu.constraint_schema=tc.constraint_schema '
            'AND kcu.constraint_name=tc.constraint_name '
            'AND kcu.table_name=tc.table_name '
            'LEFT JOIN information_schema.constraint_column_usage ccu '
            'ON ccu.constraint_schema=tc.constraint_schema '
            'AND ccu.constraint_name=tc.constraint_name '
            'WHERE tc.table_schema=current_schema() AND tc.table_name=%s '
            "AND tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY','UNIQUE') "
            'ORDER BY tc.constraint_name, kcu.ordinal_position LIMIT 256'), (table,))
        columns.append((table, tuple(rows), tuple(constraints)))
    return _digest(columns)


def _snapshot(database: Any, checkpoint: str, target: int, *,
              allow_parking: bool = False, require_source: bool = True) -> MirrorPlan:
    _identity(database)
    present = _present_tables(database)
    source = tuple((table, _count(database,
        f'SELECT COUNT(*) FROM "{table}" WHERE guild_id = %s', (SOURCE_GUILD_ID,)))
                   for table in present)
    target_counts = tuple((table, _count(database,
        f'SELECT COUNT(*) FROM "{table}" WHERE guild_id = %s', (target,)))
                          for table in present)
    parking = tuple((table, _count(database,
        f'SELECT COUNT(*) FROM "{table}" WHERE guild_id = %s', (PARKING_GUILD_ID,)))
                    for table in present)
    if any(count for _, count in parking) and not allow_parking:
        raise HistoricalMirrorError(
            f'Parking sentinel {PARKING_GUILD_ID} is already in use.')
    scrub_values = [
        ('games_with_discord_refs', _count(database, (
            'SELECT COUNT(*) FROM game WHERE guild_id = %s AND '
            '(announcement_message IS NOT NULL OR announcement_channel IS NOT NULL '
            'OR game_chan IS NOT NULL)'), (SOURCE_GUILD_ID,))),
        ('gamesides_with_discord_refs', _count(database, (
            'SELECT COUNT(*) FROM gameside s JOIN game g ON g.id=s.game_id '
            'WHERE g.guild_id = %s AND (s.required_role_id IS NOT NULL OR '
            's.team_chan IS NOT NULL OR s.team_chan_external_server IS NOT NULL)'),
            (SOURCE_GUILD_ID,))),
        ('teams_with_external_server', _count(database,
            'SELECT COUNT(*) FROM team WHERE guild_id = %s AND external_server IS NOT NULL',
            (SOURCE_GUILD_ID,))),
        ('broadcasts', _count(database, (
            'SELECT COUNT(*) FROM team_server_broadcast_message b JOIN game g '
            'ON g.id=b.game_id WHERE g.guild_id = %s'), (SOURCE_GUILD_ID,))),
        ('api_applications', _count(database, 'SELECT COUNT(*) FROM apiapplication')),
        ('legacy_drafts', _count(database, (
            'SELECT COUNT(*) FROM configuration WHERE guild_id = %s '
            'AND polychamps_draft IS NOT NULL'), (SOURCE_GUILD_ID,))),
        ('legacy_non_object_drafts', _count(database, (
            'SELECT COUNT(*) FROM configuration WHERE guild_id = %s '
            "AND polychamps_draft IS NOT NULL AND jsonb_typeof(polychamps_draft) <> 'object'"),
            (SOURCE_GUILD_ID,))),
    ]
    if 'gamelog' in present:
        gamelog_total = _count(database, 'SELECT COUNT(*) FROM gamelog')
        if gamelog_total:
            raise HistoricalMirrorError(
                'gamelog must be empty; production GameLog content is not imported.')
        scrub_values.append(('gamelog_total', gamelog_total))
    scrub = tuple(scrub_values)
    non_object = dict(scrub)['legacy_non_object_drafts']
    if non_object:
        raise HistoricalMirrorError(
            'Legacy polychamps_draft contains non-object JSON; refusing to write.')
    if require_source and dict(source).get('game', 0) <= 0:
        raise HistoricalMirrorError(
            'Source historical graph has no games; refusing to park the beta graph.')
    configuration = _configuration_state(database, target)
    schema_fingerprint = _schema_fingerprint(database, present)
    payload = {
        'checkpoint': checkpoint,
        'source_guild': SOURCE_GUILD_ID,
        'target_guild': target,
        'parking_guild': PARKING_GUILD_ID,
        'present_tables': present,
        'source_counts': source,
        'target_counts': target_counts,
        'parking_counts': parking,
        'scrub_counts': scrub,
        'schema_fingerprint': schema_fingerprint,
        'configuration': {
            'status': configuration.status,
            'present_tables': configuration.present_tables,
            'row_counts': configuration.row_counts,
            'non_target_rows': configuration.non_target_rows,
            'active_target': configuration.active_target,
        },
    }
    return MirrorPlan(
        checkpoint=checkpoint, present_tables=present, source_counts=source,
        target_counts=target_counts, parking_counts=parking,
        scrub_counts=scrub, schema_fingerprint=schema_fingerprint,
        configuration=configuration, digest=_digest(payload),
    )


def plan_database(profile: Any, *, checkpoint: str,
                  database_factory: Callable[[Any], Any] | None = None,
                  target_guild_id: int = TARGET_GUILD_ID) -> MirrorPlan:
    target = validate_profile(profile, target_guild_id=target_guild_id)
    database = (database_factory or _database_factory)(profile)
    try:
        with database.connection_context():
            with database.atomic():
                database.execute_sql('SET TRANSACTION READ ONLY')
                return _snapshot(database, checkpoint, target)
    except HistoricalMirrorError:
        raise
    except Exception as exc:
        raise HistoricalMirrorError('Historical mirror plan failed closed.') from exc


def verification_plan(profile: Any, confirmation: str, *, checkpoint: str,
                      database_factory: Callable[[Any], Any] | None = None,
                      target_guild_id: int = TARGET_GUILD_ID) -> MirrorPlan:
    """Recover the planned count baseline carried by a confirmation token."""

    target = validate_profile(profile, target_guild_id=target_guild_id)
    digest, source, target_counts, parking = parse_confirmation(confirmation)
    database = (database_factory or _database_factory)(profile)
    try:
        with database.connection_context():
            with database.atomic():
                database.execute_sql('SET TRANSACTION READ ONLY')
                current = _snapshot(database, checkpoint, target, allow_parking=True,
                                    require_source=False)
        # The operator-carried token must describe exactly the tables present
        # in this database; accepting a subset would make the evidence
        # ambiguous when optional gamelog is bootstrapped later.
        digest, source, target_counts, parking = parse_confirmation(
            confirmation, expected_tables=current.present_tables)
        return replace(current, source_counts=source, target_counts=target_counts,
                       parking_counts=parking, digest=digest)
    except HistoricalMirrorError:
        raise
    except Exception as exc:
        raise HistoricalMirrorError('Historical mirror verification plan failed closed.') from exc


def _assert_counts(actual: tuple[tuple[str, int], ...], expected: tuple[tuple[str, int], ...],
                  label: str) -> None:
    if actual != expected:
        raise HistoricalMirrorError(
            f'{label} counts changed; confirmation is stale (expected {expected}, got {actual}).')


def _write(database: Any, plan: MirrorPlan, target: int) -> None:
    # Parking must precede remapping so no pre-existing target graph is deleted
    # or accidentally merged with a source graph.
    for table in plan.present_tables:
        database.execute_sql(
            f'UPDATE "{table}" SET guild_id = %s WHERE guild_id = %s',
            (PARKING_GUILD_ID, target))
    for table in plan.present_tables:
        database.execute_sql(
            f'UPDATE "{table}" SET guild_id = %s WHERE guild_id = %s',
            (target, SOURCE_GUILD_ID))
    database.execute_sql(
        'UPDATE game SET announcement_message = NULL, announcement_channel = NULL, '
        'game_chan = NULL WHERE guild_id = %s', (target,))
    database.execute_sql(
        'UPDATE gameside SET required_role_id = NULL, team_chan = NULL, '
        'team_chan_external_server = NULL WHERE game_id IN '
        '(SELECT id FROM game WHERE guild_id = %s)', (target,))
    database.execute_sql(
        'UPDATE team SET external_server = NULL WHERE guild_id = %s', (target,))
    database.execute_sql(
        'DELETE FROM team_server_broadcast_message WHERE game_id IN '
        '(SELECT id FROM game WHERE guild_id = %s)', (target,))
    database.execute_sql('DELETE FROM apiapplication')
    # Keep other useful draft state.  A non-object was rejected in _snapshot;
    # NULL stays NULL and object values retain every key except these fields.
    database.execute_sql(
        "UPDATE configuration SET polychamps_draft = jsonb_set("
        "jsonb_set(polychamps_draft, '{announcement_message}', 'null'::jsonb, true), "
        "'{announcement_channel}', 'null'::jsonb, true) "
        'WHERE guild_id = %s AND polychamps_draft IS NOT NULL', (target,))


def _diagnostics(database: Any, target: int) -> list[str]:
    checks = (
        ('player/team guild mismatch',
         'SELECT p.id,t.id FROM player p JOIN team t ON t.id=p.team_id '
         'WHERE p.guild_id=%s AND t.guild_id<>%s LIMIT %s', (target, target, MAX_DIAGNOSTICS)),
        ('game/host guild mismatch',
         'SELECT g.id,p.id FROM game g JOIN player p ON p.id=g.host_id '
         'WHERE g.guild_id=%s AND p.guild_id<>%s LIMIT %s', (target, target, MAX_DIAGNOSTICS)),
        ('game/lineup/player guild mismatch',
         'SELECT l.id,p.id FROM lineup l JOIN game g ON g.id=l.game_id '
         'JOIN player p ON p.id=l.player_id WHERE g.guild_id=%s AND p.guild_id<>%s LIMIT %s',
         (target, target, MAX_DIAGNOSTICS)),
        ('lineup/gameside game mismatch',
         'SELECT l.id,s.id FROM lineup l JOIN game g ON g.id=l.game_id '
         'JOIN gameside s ON s.id=l.gameside_id WHERE g.guild_id=%s '
         'AND s.game_id<>g.id LIMIT %s', (target, MAX_DIAGNOSTICS)),
        ('gameside/team guild mismatch',
         'SELECT s.id,t.id FROM gameside s JOIN game g ON g.id=s.game_id '
         'JOIN team t ON t.id=s.team_id WHERE g.guild_id=%s AND t.guild_id<>%s LIMIT %s',
         (target, target, MAX_DIAGNOSTICS)),
        ('gameside/squad guild mismatch',
         'SELECT s.id,q.id FROM gameside s JOIN game g ON g.id=s.game_id '
         'JOIN squad q ON q.id=s.squad_id WHERE g.guild_id=%s AND q.guild_id<>%s LIMIT %s',
         (target, target, MAX_DIAGNOSTICS)),
        ('squad/member guild mismatch',
         'SELECT m.id,p.id FROM squadmember m JOIN squad q ON q.id=m.squad_id '
         'JOIN player p ON p.id=m.player_id WHERE q.guild_id=%s AND p.guild_id<>%s LIMIT %s',
         (target, target, MAX_DIAGNOSTICS)),
        ('winner outside game',
         'SELECT g.id,g.winner_id FROM game g JOIN gameside s ON s.id=g.winner_id '
         'WHERE g.guild_id=%s AND s.game_id<>g.id LIMIT %s', (target, MAX_DIAGNOSTICS)),
    )
    result = []
    for label, query, params in checks:
        rows = _rows(database, query, params)
        if rows:
            result.append(f'{label}: {rows[:MAX_DIAGNOSTICS]}')
    return result


def _verify_in_transaction(database: Any, plan: MirrorPlan, target: int,
                           *, require_configuration: bool = False) -> None:
    present = _present_tables(database)
    if present != plan.present_tables:
        raise HistoricalMirrorError('Direct table topology changed during apply.')
    for table in present:
        source_left = _count(database, f'SELECT COUNT(*) FROM "{table}" WHERE guild_id=%s',
                             (SOURCE_GUILD_ID,))
        if source_left:
            raise HistoricalMirrorError(f'Direct source rows remain in {table}.')
        target_count = _count(database, f'SELECT COUNT(*) FROM "{table}" WHERE guild_id=%s',
                              (target,))
        parking_count = _count(database, f'SELECT COUNT(*) FROM "{table}" WHERE guild_id=%s',
                               (PARKING_GUILD_ID,))
        expected_source = dict(plan.source_counts).get(table, 0)
        expected_target = dict(plan.target_counts).get(table, 0)
        if target_count != expected_source or parking_count != expected_target:
            raise HistoricalMirrorError(
                f'{table} counts do not match the planned remap: '
                f'target={target_count}/{expected_source}, parking={parking_count}/{expected_target}.')
    scrubbed = (
        _count(database, 'SELECT COUNT(*) FROM game WHERE guild_id=%s AND '
               '(announcement_message IS NOT NULL OR announcement_channel IS NOT NULL OR game_chan IS NOT NULL)', (target,)),
        _count(database, 'SELECT COUNT(*) FROM gameside s JOIN game g ON g.id=s.game_id '
               'WHERE g.guild_id=%s AND (s.required_role_id IS NOT NULL OR s.team_chan IS NOT NULL OR '
               's.team_chan_external_server IS NOT NULL)', (target,)),
        _count(database, 'SELECT COUNT(*) FROM team WHERE guild_id=%s AND external_server IS NOT NULL', (target,)),
        _count(database, 'SELECT COUNT(*) FROM team_server_broadcast_message b JOIN game g ON g.id=b.game_id WHERE g.guild_id=%s', (target,)),
        _count(database, 'SELECT COUNT(*) FROM apiapplication'),
    )
    if any(scrubbed):
        raise HistoricalMirrorError(f'Scrubbed references remain: {scrubbed}.')
    if 'gamelog' in present and _count(database, 'SELECT COUNT(*) FROM gamelog'):
        raise HistoricalMirrorError('gamelog is non-empty after mirror.')
    diagnostics = _diagnostics(database, target)
    if diagnostics:
        raise HistoricalMirrorError('Historical cross-guild graph anomaly: ' + '; '.join(diagnostics))
    configuration = _configuration_state(database, target)
    if require_configuration and configuration.status != 'ready':
        raise HistoricalMirrorError(
            'Modern guild configuration is not ready; restore all five '
            'development configuration tables before apply.')
    if configuration.status == 'ready' and (not configuration.active_target or any(count for _, count in configuration.non_target_rows)):
        raise HistoricalMirrorError('Modern guild configuration is not active and target-only.')


def apply_database(profile: Any, confirmation: str, *, checkpoint: str,
                   database_factory: Callable[[Any], Any] | None = None,
                   writer_lock_factory: Callable[[Any], Any] | None = None,
                   target_guild_id: int = TARGET_GUILD_ID) -> MirrorResult:
    target = validate_profile(profile, target_guild_id=target_guild_id)
    digest, expected_source, expected_target, expected_parking = parse_confirmation(confirmation)
    lock = None
    database = None
    plan: MirrorPlan | None = None
    committed = False
    try:
        lock = (writer_lock_factory or beta_database_writer_lock.BetaDatabaseWriterLock)(profile)
        database = (database_factory or _database_factory)(profile)
        with lock:
            with database.connection_context():
                with database.atomic():
                    plan = _snapshot(database, checkpoint, target)
                    digest, expected_source, expected_target, expected_parking = parse_confirmation(
                        confirmation, expected_tables=plan.present_tables)
                    if plan.digest != digest or plan.confirmation != confirmation:
                        raise HistoricalMirrorError('Historical mirror confirmation is stale.')
                    _assert_counts(plan.source_counts, expected_source, 'Source')
                    _assert_counts(plan.target_counts, expected_target, 'Target')
                    _assert_counts(plan.parking_counts, expected_parking, 'Parking')
                    _write(database, plan, target)
                    _verify_in_transaction(database, plan, target, require_configuration=True)
            committed = True
            try:
                verify_database(profile, plan, database_factory=database_factory,
                                target_guild_id=target_guild_id)
            except BaseException as exc:
                raise HistoricalMirrorReconciliationRequired(
                    'Historical mirror committed, but post-commit verification or '
                    'lock release failed; reconciliation is required and no inverse '
                    'rollback was attempted.') from exc
    except HistoricalMirrorReconciliationRequired:
        raise
    except beta_database_writer_lock.BetaDatabaseWriterLockError as exc:
        if committed:
            raise HistoricalMirrorReconciliationRequired(
                'Historical mirror committed, but writer-lock acquisition/release '
                'failed; reconciliation is required.') from exc
        raise HistoricalMirrorError('Historical mirror writer lock was not held.') from exc
    except HistoricalMirrorError as exc:
        if committed:
            raise HistoricalMirrorReconciliationRequired(
                'Historical mirror committed, but post-commit supervision failed; '
                'reconciliation is required.') from exc
        raise
    except BaseException as exc:
        if committed:
            raise HistoricalMirrorReconciliationRequired(
                'Historical mirror committed, but post-commit supervision failed; '
                'reconciliation is required.') from exc
        raise HistoricalMirrorError('Historical mirror transaction rolled back.') from exc
    assert plan is not None
    return MirrorResult(plan, True, 'verified')


def verify_database(profile: Any, plan: MirrorPlan, *,
                    database_factory: Callable[[Any], Any] | None = None,
                    target_guild_id: int = TARGET_GUILD_ID) -> None:
    target = validate_profile(profile, target_guild_id=target_guild_id)
    database = (database_factory or _database_factory)(profile)
    try:
        with database.connection_context():
            with database.atomic():
                database.execute_sql('SET TRANSACTION READ ONLY')
                _verify_in_transaction(database, plan, target, require_configuration=True)
    except HistoricalMirrorError:
        raise
    except Exception as exc:
        raise HistoricalMirrorError('Historical mirror verification failed closed.') from exc
