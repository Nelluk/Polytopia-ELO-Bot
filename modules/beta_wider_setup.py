"""Exact-scope development team/house setup for WB1.3b.

This module is a synchronous CLI boundary.  It deliberately uses the
existing Peewee database object only as a connection/transaction owner and
executes bounded raw SQL, so importing the setup tool cannot run the legacy
model module's table-creation check.  It never imports Discord, commands, or
ELO code.

The tracked reviewed manifest is the policy input.  Database IDs are not
known until the seed transaction runs, so role bindings use ``source_id``
``null`` in the manifest and are recorded with their resulting team IDs in a
private ownership evidence file after commit.  Cleanup can delete only IDs
listed as owned by that evidence and rechecks every immutable identity and
usage condition in a new transaction.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from modules import beta_database_writer_lock, beta_operations, beta_readiness


SETUP_SCHEMA_VERSION = 1
SETUP_STATE_FILENAME = 'wb1-3b-setup.json'
SETUP_PENDING_STATE_FILENAME = 'wb1-3b-setup.pending.json'
DEFAULT_MANIFEST = 'readiness-manifests/wb1-3b-reviewed.json'

EXPECTED_HOUSES = (
    'Beta House Alpha',
    'Beta House Beta',
)
EXPECTED_TEAMS = (
    ('The Ronin', 'Beta House Alpha', 480350546172182530),
    ('The Jets', 'Beta House Alpha', 480350570717118465),
    ('The Sparkies', 'Beta House Beta', 481210095397634060),
)
# WB1.3b originally seeded these teams without league tiers. The wider-beta
# operator may later assign the reviewed showcase tiers through `/team tier`
# so `/leaderboard teams` is testable. Both the untouched seed state and this
# exact post-setup state remain compatible; no other tier drift is accepted.
EXPECTED_SHOWCASE_TEAM_TIERS = {
    'The Ronin': 1,
    'The Jets': 2,
    'The Sparkies': 3,
}
EXPECTED_TEAM_ROLE_IDS = {
    name: role_id for name, _house, role_id in EXPECTED_TEAMS
}
EXPECTED_CAPABILITIES = (
    'core_user',
    'elo_maintenance',
    'team',
    'tools_support',
)
EXPECTED_CURRENT_CAPABILITIES = (
    'core_user',
    'elo_maintenance',
    'team',
)
EXPECTED_TOOLS_SUPPORT_ROOTS = ('staffhelp',)
RESERVED_TOOLS_SUPPORT_ROOTS = ('about', 'guide', 'help', 'support', 'tools')


class WiderBetaSetupError(RuntimeError):
    """Base class for an expected WB1.3b refusal."""


class WiderBetaSetupSafetyError(WiderBetaSetupError):
    """The exact runtime, writer, or target safety gate failed."""


class WiderBetaSetupConflictError(WiderBetaSetupError):
    """An existing record is not compatible with the reviewed state."""


class WiderBetaSetupOwnershipError(WiderBetaSetupError):
    """Durable ownership evidence or cleanup identity is unsafe."""


class WiderBetaSetupConfirmationError(WiderBetaSetupError):
    """A destructive cleanup was not explicitly confirmed."""


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WiderBetaSetupOwnershipError(f'{field} must be a positive integer.')
    return value


def _bool(value: Any) -> bool:
    return bool(value)


def _manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def validate_reviewed_manifest(value: Any) -> dict[str, Any]:
    """Validate the exact reviewed WB1.3b desired state.

    The generic readiness schema remains useful for unresolved planning
    templates.  Seed and cleanup intentionally accept only the reviewed
    exact state below, so an operator cannot broaden the database scope by
    supplying a different repository JSON file.
    """

    manifest = beta_readiness.validate_readiness_manifest(value)
    capabilities = manifest['capabilities']
    if tuple(capabilities['current']) != EXPECTED_CURRENT_CAPABILITIES:
        raise WiderBetaSetupConflictError(
            'WB1.3b requires the current capabilities core_user, '
            'elo_maintenance, and team.'
        )
    if tuple(capabilities['proposed']) != EXPECTED_CAPABILITIES:
        raise WiderBetaSetupConflictError(
            'WB1.3b must propose tools_support with the current capabilities.'
        )
    optional = capabilities['optional']
    if len(optional) != 1:
        raise WiderBetaSetupConflictError(
            'WB1.3b requires one reviewed tools_support decision.'
        )
    decision = optional[0]
    reason = decision.get('reason', '')
    if (
            decision.get('capability') != 'tools_support'
            or decision.get('decision') != 'include'
            or not all(
                marker in reason
                for marker in (
                    '/staffhelp',
                    '/about',
                    '/guide',
                    '/help',
                    '/support',
                    '/tools',
                )
            )):
        raise WiderBetaSetupConflictError(
            'The tools_support review must enumerate /staffhelp and every '
            'reserved, currently-unloaded root.'
        )
    if any('tools_support' in item.casefold() for item in capabilities['unresolved']):
        raise WiderBetaSetupConflictError(
            'tools_support is resolved in the reviewed WB1.3b manifest.'
        )

    teams = tuple(
        (
            item['name'],
            item['house_name'],
            item['role_name'],
        )
        for item in manifest['database']['teams']['proposed']
    )
    expected_teams = tuple((name, house, name) for name, house, _role_id in EXPECTED_TEAMS)
    if teams != expected_teams:
        raise WiderBetaSetupConflictError(
            'The reviewed manifest must contain the three exact WB1.3b teams '
            'and approved house assignments.'
        )
    houses = tuple(
        (item['name'], item['role_name'])
        for item in manifest['database']['houses']['proposed']
    )
    if houses != tuple((name, None) for name in EXPECTED_HOUSES):
        raise WiderBetaSetupConflictError(
            'The reviewed manifest must contain the two exact houses with no '
            'house-role binding.'
        )
    bindings = tuple(
        (
            item['kind'],
            item['source_id'],
            item['role_name'],
            item['role_id'],
        )
        for item in manifest['database']['role_bindings']['proposed']
    )
    expected_bindings = tuple(
        ('team', None, name, role_id)
        for name, _house, role_id in EXPECTED_TEAMS
    )
    if bindings != expected_bindings:
        raise WiderBetaSetupConflictError(
            'The reviewed manifest must pin only the three existing team '
            'role IDs before database IDs exist.'
        )
    if manifest['database']['role_bindings']['unresolved']:
        raise WiderBetaSetupConflictError(
            'House/team role binding decisions are resolved in WB1.3b.'
        )
    if manifest['database']['fixtures']['cleanup']:
        raise WiderBetaSetupConflictError(
            'WB1.3b does not approve fixture cleanup.'
        )
    return manifest


def _state_path(profile: Any, *, create: bool) -> Path:
    try:
        paths = beta_operations.operation_paths(profile, create=create)
    except beta_operations.BetaOperationsError as exc:
        raise WiderBetaSetupSafetyError(str(exc)) from exc
    return paths.state_root / SETUP_STATE_FILENAME


def _pending_state_path(profile: Any, *, create: bool) -> Path:
    return _state_path(profile, create=create).with_name(SETUP_PENDING_STATE_FILENAME)


def _read_private_state_path(
        path: Path,
        *,
        label: str) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WiderBetaSetupOwnershipError(
            f'Could not inspect {label}.'
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WiderBetaSetupOwnershipError(
            f'{label} must be a regular non-symlink file.'
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise WiderBetaSetupOwnershipError(
            f'{label} permissions are too broad.'
        )
    if info.st_size > beta_readiness.MAX_MANIFEST_BYTES:
        raise WiderBetaSetupOwnershipError(
            f'{label} exceeds its bound.'
        )
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WiderBetaSetupOwnershipError(
            f'{label} is unreadable.'
        ) from exc
    if not isinstance(value, dict):
        raise WiderBetaSetupOwnershipError(
            f'{label} must be a JSON object.'
        )
    return value


def _read_state(profile: Any) -> dict[str, Any] | None:
    return _read_private_state_path(
        _state_path(profile, create=False),
        label='WB1.3b ownership evidence',
    )


def _read_pending_state(profile: Any) -> dict[str, Any] | None:
    return _read_private_state_path(
        _pending_state_path(profile, create=False),
        label='WB1.3b pending ownership evidence',
    )


def _write_state(profile: Any, value: Mapping[str, Any]) -> Path:
    """Write prepared evidence while the database transaction is open.

    The pending filename is intentionally not accepted by cleanup as
    authoritative.  It survives commit failure or process interruption and is
    promoted only after the database transaction has committed.
    """

    path = _pending_state_path(profile, create=True)
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8') + b'\n'
    if len(payload) > beta_readiness.MAX_MANIFEST_BYTES:
        raise WiderBetaSetupOwnershipError(
            'WB1.3b ownership evidence exceeds its bound.'
        )
    temporary_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f'.{SETUP_PENDING_STATE_FILENAME}.',
            suffix='.tmp',
            dir=path.parent,
        )
        temporary_path = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise WiderBetaSetupOwnershipError(
            'Could not atomically write WB1.3b ownership evidence.'
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return path


def _publish_state(profile: Any) -> Path:
    """Promote prepared evidence after a successful DB commit."""

    pending_path = _pending_state_path(profile, create=False)
    state_path = _state_path(profile, create=False)
    pending = _read_private_state_path(
        pending_path,
        label='WB1.3b pending ownership evidence',
    )
    if pending is None:
        raise WiderBetaSetupOwnershipError(
            'The setup committed without pending ownership evidence.'
        )
    try:
        os.replace(pending_path, state_path)
        directory_fd = os.open(
            state_path.parent,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise WiderBetaSetupOwnershipError(
            'The database transaction committed, but ownership publication '
            'could not be promoted. Pending evidence was retained; reconcile '
            'before retrying seed or cleanup.'
        ) from exc
    return state_path


def _remove_state(profile: Any) -> None:
    path = _state_path(profile, create=False)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WiderBetaSetupOwnershipError(
            'The setup committed, but ownership evidence could not be removed.'
        ) from exc


@contextmanager
def _held_beta_writer_lock(profile: Any):
    """Hold the launcher's guarded writer lock for one mutation operation."""

    try:
        paths = beta_operations.operation_paths(profile, create=True)
        lock = beta_operations.BetaWriterLock(paths.writer_lock)
        lock.acquire()
    except beta_operations.BetaOperationsError as exc:
        raise WiderBetaSetupSafetyError(
            'The durable beta writer is active or its guarded lock path is unsafe; '
            'stop it before seed or cleanup.'
        ) from exc
    try:
        yield lock
    finally:
        lock.release()


@contextmanager
def _mutation_writer_scope(
        profile: Any,
        *,
        writer_guard: Callable[[Any], Any] | None = None,
        writer_check: Callable[[Any], None] | None = None):
    """Run the complete mutation boundary while one writer lock is held.

    ``writer_guard`` is an internal test/integration seam.  The production
    default is always the same lock object used by the durable launcher.
    ``writer_check`` remains a compatibility preflight hook, but it executes
    inside the held boundary and cannot replace it.
    """

    guard = writer_guard(profile) if writer_guard is not None else _held_beta_writer_lock(profile)
    if guard is None:
        guard = nullcontext()
    with guard:
        try:
            database_guard = beta_database_writer_lock.BetaDatabaseWriterLock(
                profile
            )
            database_guard.acquire()
        except beta_database_writer_lock.BetaDatabaseWriterLockError as exc:
            raise WiderBetaSetupSafetyError(
                'Another process holds the development database writer lock; '
                'stop every beta writer before seed or cleanup.'
            ) from exc
        try:
            if writer_check is not None:
                writer_check(profile)
            yield
        finally:
            database_guard.release()


def assert_beta_writer_stopped(profile: Any) -> None:
    """Acquire and release both universal development writer locks once."""

    with _mutation_writer_scope(profile):
        return


def _validate_profile(profile: Any, guild_id: int) -> int:
    try:
        return beta_readiness.validate_database_profile(profile, guild_id)
    except beta_readiness.ReadinessError as exc:
        raise WiderBetaSetupSafetyError(str(exc)) from exc


def _validate_live_identity(database_name: Any, database_role: Any) -> None:
    try:
        beta_readiness.validate_live_database_identity(
            str(database_name), str(database_role)
        )
    except beta_readiness.ReadinessError as exc:
        raise WiderBetaSetupSafetyError(str(exc)) from exc


def _default_database_factory(profile: Any) -> Any:
    from playhouse.postgres_ext import PostgresqlExtDatabase

    settings: dict[str, Any] = {
        'user': profile.database_user,
        'password': profile.database_password,
        'autoconnect': False,
    }
    if profile.database_host:
        settings['host'] = profile.database_host
    if profile.database_port:
        settings['port'] = profile.database_port
    return PostgresqlExtDatabase(profile.database_name, **settings)


def _rows(database: Any, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    cursor = database.execute_sql(query, tuple(params))
    raw = cursor.fetchall()
    try:
        return [tuple(row) for row in raw]
    except TypeError as exc:
        raise WiderBetaSetupSafetyError('The setup query returned an invalid row shape.') from exc


def _one_count(database: Any, query: str, params: Sequence[Any]) -> int:
    found = _rows(database, query, params)
    if len(found) != 1 or len(found[0]) != 1:
        raise WiderBetaSetupSafetyError('The setup count query returned an invalid shape.')
    try:
        return int(found[0][0])
    except (TypeError, ValueError) as exc:
        raise WiderBetaSetupSafetyError('The setup count query was not an integer.') from exc


def _identity(database: Any) -> None:
    rows = _rows(database, 'SELECT current_database(), current_user')
    if len(rows) != 1 or len(rows[0]) != 2:
        raise WiderBetaSetupSafetyError('The live database identity query returned an invalid shape.')
    _validate_live_identity(rows[0][0], rows[0][1])


def _house(database: Any, name: str) -> list[dict[str, Any]]:
    output = []
    for row in _rows(
            database,
            'SELECT id, name, emoji, image_url, league_tokens '
            'FROM house WHERE name = %s ORDER BY id',
            (name,)):
        if len(row) != 5:
            raise WiderBetaSetupSafetyError('The house query returned an invalid shape.')
        house_id, house_name, emoji, image_url, tokens = row
        output.append({
            'id': int(house_id),
            'name': str(house_name),
            'emoji': str(emoji) if emoji is not None else None,
            'image_url': str(image_url) if image_url is not None else None,
            'league_tokens': int(tokens),
        })
    return output


def _team(database: Any, guild_id: int, name: str) -> list[dict[str, Any]]:
    output = []
    query = (
        'SELECT t.id, t.name, t.guild_id, t.house_id, h.name, '
        't.is_hidden, t.is_archived, t.league_tier, t.external_server, '
        't.elo, t.elo_alltime, t.emoji, t.image_url, t.pro_league '
        'FROM team AS t LEFT JOIN house AS h ON h.id = t.house_id '
        'WHERE t.guild_id = %s AND t.name = %s ORDER BY t.id'
    )
    for row in _rows(database, query, (guild_id, name)):
        if len(row) != 14:
            raise WiderBetaSetupSafetyError('The team query returned an invalid shape.')
        output.append({
            'id': int(row[0]),
            'name': str(row[1]),
            'guild_id': int(row[2]),
            'house_id': int(row[3]) if row[3] is not None else None,
            'house_name': str(row[4]) if row[4] is not None else None,
            'hidden': _bool(row[5]),
            'archived': _bool(row[6]),
            'league_tier': int(row[7]) if row[7] is not None else None,
            'external_server': int(row[8]) if row[8] is not None else None,
            'elo': int(row[9]),
            'elo_alltime': int(row[10]),
            'emoji': str(row[11]) if row[11] is not None else None,
            'image_url': str(row[12]) if row[12] is not None else None,
            'pro_league': _bool(row[13]),
        })
    return output


def _house_usage(database: Any, house_id: int) -> dict[str, Any]:
    attached = _rows(
        database,
        'SELECT id, name, guild_id FROM team WHERE house_id = %s ORDER BY id',
        (house_id,),
    )
    if any(len(row) != 3 for row in attached):
        raise WiderBetaSetupSafetyError('The house-team usage query returned an invalid shape.')
    return {
        'team_ids': [int(row[0]) for row in attached],
        'team_names': [str(row[1]) for row in attached],
        'team_guild_ids': [int(row[2]) for row in attached],
        'preference_count': _one_count(
            database,
            'SELECT COUNT(*) FROM playerhousepreference WHERE house_id = %s',
            (house_id,),
        ),
        'bid_count': _one_count(
            database,
            'SELECT COUNT(*) FROM bid WHERE house_id = %s',
            (house_id,),
        ),
    }


def _team_usage(database: Any, team_id: int) -> dict[str, int]:
    return {
        'player_count': _one_count(
            database,
            'SELECT COUNT(*) FROM player WHERE team_id = %s',
            (team_id,),
        ),
        'game_side_count': _one_count(
            database,
            'SELECT COUNT(*) FROM gameside WHERE team_id = %s',
            (team_id,),
        ),
    }


def _read_records(database: Any, guild_id: int) -> dict[str, Any]:
    houses: dict[str, list[dict[str, Any]]] = {
        name: _house(database, name) for name in EXPECTED_HOUSES
    }
    teams: dict[str, list[dict[str, Any]]] = {
        name: _team(database, guild_id, name)
        for name, _house_name, _role_id in EXPECTED_TEAMS
    }
    for name, records in houses.items():
        for record in records:
            record['usage'] = _house_usage(database, record['id'])
    for name, records in teams.items():
        for record in records:
            record['usage'] = _team_usage(database, record['id'])
    return {'houses': houses, 'teams': teams}


def _compatibility_issues(records: Mapping[str, Any], guild_id: int) -> list[str]:
    issues: list[str] = []
    houses = records['houses']
    teams = records['teams']
    for house_name, found in houses.items():
        if len(found) > 1:
            issues.append(f'house {house_name!r} has duplicate rows')
        if found:
            usage = found[0]['usage']
            other_guilds = sorted({
                value for value in usage['team_guild_ids']
                if value != guild_id
            })
            if other_guilds:
                issues.append(
                    f'house {house_name!r} is used by another guild: '
                    + ', '.join(str(value) for value in other_guilds)
                )
            if usage['preference_count'] or usage['bid_count']:
                issues.append(
                    f'house {house_name!r} has persisted player/bid usage'
                )
    for team_name, found in teams.items():
        if len(found) > 1:
            issues.append(f'team {team_name!r} has duplicate rows')
        if not found:
            continue
        record = found[0]
        expected_house = next(
            house_name for name, house_name, _role_id in EXPECTED_TEAMS
            if name == team_name
        )
        checks = (
            (record['guild_id'] == guild_id, 'guild'),
            (record['name'] == team_name, 'name'),
            (record['house_name'] == expected_house, 'house'),
            (not record['hidden'], 'hidden'),
            (not record['archived'], 'archived'),
            (
                record['league_tier'] in (
                    None, EXPECTED_SHOWCASE_TEAM_TIERS[team_name]
                ),
                'league_tier',
            ),
            (record['external_server'] is None, 'external_server'),
        )
        for valid, label in checks:
            if not valid:
                issues.append(f'team {team_name!r} has incompatible {label} state')
    return sorted(set(issues))


def _public_records(records: Mapping[str, Any], state: Mapping[str, Any] | None) -> dict[str, Any]:
    owned_houses = {
        item['name']: bool(item['owned'])
        for item in (state or {}).get('houses', [])
        if isinstance(item, Mapping)
    }
    owned_teams = {
        item['name']: bool(item['owned'])
        for item in (state or {}).get('teams', [])
        if isinstance(item, Mapping)
    }
    houses = []
    for name in EXPECTED_HOUSES:
        for record in records['houses'][name]:
            houses.append({
                'id': record['id'],
                'name': record['name'],
                'role_name': None,
                'owned': owned_houses.get(name, False),
                'usage': record['usage'],
            })
    teams = []
    for name, _house, role_id in EXPECTED_TEAMS:
        for record in records['teams'][name]:
            teams.append({
                'id': record['id'],
                'name': record['name'],
                'role_name': name,
                'guild_id': record['guild_id'],
                'house_id': record['house_id'],
                'house_name': record['house_name'],
                'hidden': record['hidden'],
                'archived': record['archived'],
                'league_tier': record['league_tier'],
                'external_server': record['external_server'],
                'role_id': role_id,
                'owned': owned_teams.get(name, False),
                'usage': record['usage'],
            })
    return {'houses': houses, 'teams': teams}


def _validate_state_shape(
        state: Mapping[str, Any],
        manifest: Mapping[str, Any],
        guild_id: int) -> None:
    if set(state) != {
            'schema_version', 'kind', 'manifest_fingerprint', 'target',
            'houses', 'teams', 'role_bindings', 'created_at'}:
        raise WiderBetaSetupOwnershipError(
            'WB1.3b ownership evidence has an unsupported shape.'
        )
    if state.get('schema_version') != SETUP_SCHEMA_VERSION or state.get('kind') != 'wb1_3b_setup_ownership':
        raise WiderBetaSetupOwnershipError('WB1.3b ownership evidence has an unsupported schema.')
    if state.get('manifest_fingerprint') != _manifest_fingerprint(manifest):
        raise WiderBetaSetupOwnershipError('WB1.3b ownership evidence does not match the reviewed manifest.')
    target = state.get('target')
    if target != {
        'environment': 'development',
        'guild_id': guild_id,
        'database': beta_readiness.BETA_DATABASE_NAME,
        'database_role': beta_readiness.BETA_DATABASE_ROLE,
    }:
        raise WiderBetaSetupOwnershipError('WB1.3b ownership evidence targets the wrong database scope.')
    for key, expected_names in (
            ('houses', EXPECTED_HOUSES),
            ('teams', tuple(item[0] for item in EXPECTED_TEAMS))):
        values = state.get(key)
        if not isinstance(values, list) or len(values) != len(expected_names):
            raise WiderBetaSetupOwnershipError(f'WB1.3b ownership evidence has an invalid {key} list.')
        names = []
        for item in values:
            if not isinstance(item, Mapping) or set(item) != {'id', 'name', 'owned', 'baseline'}:
                raise WiderBetaSetupOwnershipError(f'WB1.3b ownership evidence has an invalid {key} record.')
            names.append(str(item['name']))
            _positive_int(item['id'], f'{key}.id')
            if type(item['owned']) is not bool or not isinstance(item['baseline'], Mapping):
                raise WiderBetaSetupOwnershipError(f'WB1.3b ownership evidence has an invalid {key} baseline.')
            expected_baseline = (
                {'name', 'emoji', 'image_url', 'league_tokens'}
                if key == 'houses' else {
                    'name', 'guild_id', 'house_id', 'house_name', 'hidden',
                    'archived', 'league_tier', 'external_server', 'elo',
                    'elo_alltime', 'emoji', 'image_url', 'pro_league',
                }
            )
            if set(item['baseline']) != expected_baseline:
                raise WiderBetaSetupOwnershipError(
                    f'WB1.3b ownership evidence has an invalid {key} baseline shape.'
                )
        if tuple(sorted(names)) != tuple(sorted(expected_names)):
            raise WiderBetaSetupOwnershipError(f'WB1.3b ownership evidence has the wrong {key} names.')
    if not isinstance(state.get('created_at'), str) or not state['created_at']:
        raise WiderBetaSetupOwnershipError('WB1.3b ownership evidence has an invalid timestamp.')
    bindings = state.get('role_bindings')
    expected_bindings = {
        name: role_id for name, _house_name, role_id in EXPECTED_TEAMS
    }
    if not isinstance(bindings, list) or len(bindings) != len(expected_bindings):
        raise WiderBetaSetupOwnershipError(
            'WB1.3b ownership evidence has an invalid role-binding list.'
        )
    seen_bindings: set[str] = set()
    team_ids = {
        item['name']: int(item['id'])
        for item in state['teams']
    }
    for item in bindings:
        if not isinstance(item, Mapping) or set(item) != {
                'kind', 'source_id', 'role_name', 'role_id'}:
            raise WiderBetaSetupOwnershipError(
                'WB1.3b ownership evidence has an invalid role binding.'
            )
        role_name = item['role_name']
        if (
                item['kind'] != 'team'
                or role_name in seen_bindings
                or role_name not in expected_bindings
                or int(item['role_id']) != expected_bindings[role_name]
                or int(item['source_id']) != team_ids.get(role_name)
        ):
            raise WiderBetaSetupOwnershipError(
                'WB1.3b ownership evidence has an unexpected role binding.'
            )
        _positive_int(item['source_id'], 'role_bindings.source_id')
        _positive_int(item['role_id'], 'role_bindings.role_id')
        seen_bindings.add(role_name)
    if seen_bindings != set(expected_bindings):
        raise WiderBetaSetupOwnershipError(
            'WB1.3b ownership evidence does not cover every approved team role.'
        )


def _state_record(state: Mapping[str, Any], key: str, name: str) -> Mapping[str, Any] | None:
    for item in state.get(key, ()):
        if isinstance(item, Mapping) and item.get('name') == name:
            return item
    return None


def _baseline(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'name': record['name'],
        'guild_id': record.get('guild_id'),
        'house_id': record.get('house_id'),
        'house_name': record.get('house_name'),
        'hidden': record.get('hidden'),
        'archived': record.get('archived'),
        'league_tier': record.get('league_tier'),
        'external_server': record.get('external_server'),
        'elo': record.get('elo'),
        'elo_alltime': record.get('elo_alltime'),
        'emoji': record.get('emoji'),
        'image_url': record.get('image_url'),
        'pro_league': record.get('pro_league'),
    }


def _house_baseline(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'name': record['name'],
        'emoji': record.get('emoji'),
        'image_url': record.get('image_url'),
        'league_tokens': record.get('league_tokens'),
    }


def _validate_state_ids(
        state: Mapping[str, Any],
        records: Mapping[str, Any],
        manifest: Mapping[str, Any],
        guild_id: int) -> None:
    _validate_state_shape(state, manifest, guild_id)
    for key, record_map in (('houses', records['houses']), ('teams', records['teams'])):
        for item in state[key]:
            name = item['name']
            found = record_map.get(name, ())
            if len(found) != 1 or int(found[0]['id']) != int(item['id']):
                raise WiderBetaSetupOwnershipError(
                    f'WB1.3b ownership evidence for {key[:-1]} {name!r} no longer matches the database.'
                )


def _build_state(
        manifest: Mapping[str, Any],
        records: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
        guild_id: int,
        owned_house_names: Iterable[str],
        owned_team_names: Iterable[str],
        created_at: str | None = None) -> dict[str, Any]:
    previous_houses = {
        item['name']: item for item in (previous or {}).get('houses', [])
        if isinstance(item, Mapping)
    }
    previous_teams = {
        item['name']: item for item in (previous or {}).get('teams', [])
        if isinstance(item, Mapping)
    }
    owned_houses = set(owned_house_names)
    owned_teams = set(owned_team_names)
    houses = []
    for name in EXPECTED_HOUSES:
        found = records['houses'][name]
        if len(found) != 1:
            raise WiderBetaSetupOwnershipError(f'Cannot record house {name!r} without exactly one database row.')
        old = previous_houses.get(name)
        houses.append({
            'id': int(found[0]['id']),
            'name': name,
            'owned': bool(old['owned']) if old is not None else name in owned_houses,
            'baseline': _house_baseline(found[0]),
        })
    teams = []
    for name, _house, _role_id in EXPECTED_TEAMS:
        found = records['teams'][name]
        if len(found) != 1:
            raise WiderBetaSetupOwnershipError(f'Cannot record team {name!r} without exactly one database row.')
        old = previous_teams.get(name)
        teams.append({
            'id': int(found[0]['id']),
            'name': name,
            'owned': bool(old['owned']) if old is not None else name in owned_teams,
            'baseline': _baseline(found[0]),
        })
    role_bindings = [
        {
            'kind': 'team',
            'source_id': next(
                item['id'] for item in teams if item['name'] == name
            ),
            'role_name': name,
            'role_id': role_id,
        }
        for name, _house, role_id in EXPECTED_TEAMS
    ]
    return {
        'schema_version': SETUP_SCHEMA_VERSION,
        'kind': 'wb1_3b_setup_ownership',
        'manifest_fingerprint': _manifest_fingerprint(manifest),
        'target': {
            'environment': 'development',
            'guild_id': guild_id,
            'database': beta_readiness.BETA_DATABASE_NAME,
            'database_role': beta_readiness.BETA_DATABASE_ROLE,
        },
        'houses': houses,
        'teams': teams,
        'role_bindings': role_bindings,
        'created_at': created_at or datetime.now(timezone.utc).isoformat(),
    }


def _state_error_if_changed(
        state: Mapping[str, Any],
        records: Mapping[str, Any],
        manifest: Mapping[str, Any],
        guild_id: int) -> None:
    _validate_state_ids(state, records, manifest, guild_id)
    for item in state['houses']:
        found = records['houses'][item['name']][0]
        if _house_baseline(found) != dict(item['baseline']):
            raise WiderBetaSetupOwnershipError(
                f'House {item["name"]!r} changed after ownership was recorded.'
            )
    for item in state['teams']:
        found = records['teams'][item['name']][0]
        if _baseline(found) != dict(item['baseline']):
            raise WiderBetaSetupOwnershipError(
                f'Team {item["name"]!r} changed after ownership was recorded.'
            )


@contextmanager
def _database_scope(
        profile: Any,
        database_factory: Callable[[Any], Any] | None = None):
    try:
        database = database_factory(profile) if database_factory else _default_database_factory(profile)
    except WiderBetaSetupError:
        raise
    except Exception as exc:
        raise WiderBetaSetupSafetyError('The setup could not create its database connection.') from exc
    try:
        is_closed = getattr(database, 'is_closed', None)
        if callable(is_closed) and not is_closed():
            # A strictly gated integration harness may inject an already-open
            # connection inside its own rollback scope.  Do not close that
            # caller-owned transaction; normal setup calls receive a new
            # closed database and use the worker-local context below.
            yield database
        else:
            with database.connection_context():
                yield database
    except WiderBetaSetupError:
        raise
    except Exception as exc:
        raise WiderBetaSetupSafetyError('The setup database operation failed closed.') from exc


def _read_only_records(
        profile: Any,
        guild_id: int,
        database_factory: Callable[[Any], Any] | None) -> dict[str, Any]:
    with _database_scope(profile, database_factory) as database:
        with database.atomic():
            database.execute_sql('SET TRANSACTION READ ONLY')
            _identity(database)
            return _read_records(database, guild_id)


def _status_value(
        manifest: Mapping[str, Any],
        profile: Any,
        guild_id: int,
        records: Mapping[str, Any],
        state: Mapping[str, Any] | None,
        pending_state: Mapping[str, Any] | None,
        *,
        kind: str) -> dict[str, Any]:
    issues = _compatibility_issues(records, guild_id)
    if state is not None:
        _validate_state_ids(state, records, manifest, guild_id)
    public = _public_records(records, state)
    create_houses = [
        name for name in EXPECTED_HOUSES if not records['houses'][name]
    ]
    create_teams = [
        name for name, _house, _role_id in EXPECTED_TEAMS
        if not records['teams'][name]
    ]
    return {
        'schema_version': SETUP_SCHEMA_VERSION,
        'kind': kind,
        'target': manifest['target'],
        'manifest_fingerprint': _manifest_fingerprint(manifest),
        'state_file_present': state is not None,
        'pending_state_file_present': pending_state is not None,
        'houses': public['houses'],
        'teams': public['teams'],
        'conflicts': sorted(set(issues)),
        'plan': {
            'create_houses': create_houses,
            'create_teams': create_teams,
            'preserve_existing': sorted(
                [
                    *(
                        name for name in EXPECTED_HOUSES
                        if records['houses'][name]
                    ),
                    *(
                        name for name, _house, _role_id in EXPECTED_TEAMS
                        if records['teams'][name]
                    ),
                ]
            ),
            'league_tier': None,
            'discord_mutation_applied': False,
            'command_registration_applied': False,
            'game_or_elo_mutation_applied': False,
        },
        'ready_for_seed': not issues and pending_state is None,
        'ready_for_cleanup': bool(
            state is not None and not issues and pending_state is None
        ),
        'boundaries': {
            'discord_mutation_applied': False,
            'command_registration_applied': False,
            'game_mutation_applied': False,
            'elo_mutation_applied': False,
        },
    }


def status_wider_beta_setup(
        *,
        profile: Any,
        manifest: Mapping[str, Any],
        guild_id: int = beta_readiness.BETA_GUILD_ID,
        database_factory: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    reviewed = validate_reviewed_manifest(manifest)
    normalized_guild_id = _validate_profile(profile, guild_id)
    state = _read_state(profile)
    pending_state = _read_pending_state(profile)
    records = _read_only_records(profile, normalized_guild_id, database_factory)
    return _status_value(
        reviewed, profile, normalized_guild_id, records, state, pending_state,
        kind='wb1_3b_setup_status',
    )


def plan_wider_beta_setup(
        *,
        profile: Any,
        manifest: Mapping[str, Any],
        guild_id: int = beta_readiness.BETA_GUILD_ID,
        database_factory: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    reviewed = validate_reviewed_manifest(manifest)
    normalized_guild_id = _validate_profile(profile, guild_id)
    state = _read_state(profile)
    pending_state = _read_pending_state(profile)
    records = _read_only_records(profile, normalized_guild_id, database_factory)
    result = _status_value(
        reviewed, profile, normalized_guild_id, records, state, pending_state,
        kind='wb1_3b_setup_plan',
    )
    result['ready_for_live_apply'] = False
    result['ready_for_invitation'] = False
    result['boundaries']['live_apply'] = False
    result['boundaries']['tester_invitations_sent'] = False
    return result


def _insert_house(database: Any, name: str) -> int:
    cursor = database.execute_sql(
        'INSERT INTO house (name, emoji, image_url, league_tokens) '
        'VALUES (%s, %s, %s, %s) RETURNING id',
        (name, '', None, 0),
    )
    row = cursor.fetchone()
    if row is None or len(row) != 1:
        raise WiderBetaSetupSafetyError('House creation returned an invalid ID.')
    return _positive_int(int(row[0]), 'created house ID')


def _insert_team(database: Any, guild_id: int, name: str, house_id: int) -> int:
    cursor = database.execute_sql(
        'INSERT INTO team '
        '(guild_id, name, house_id, is_hidden, is_archived, league_tier, '
        'elo, elo_alltime, emoji, pro_league) '
        'VALUES (%s, %s, %s, FALSE, FALSE, NULL, %s, %s, %s, TRUE) '
        'RETURNING id',
        (guild_id, name, house_id, 1000, 1000, ''),
    )
    row = cursor.fetchone()
    if row is None or len(row) != 1:
        raise WiderBetaSetupSafetyError('Team creation returned an invalid ID.')
    return _positive_int(int(row[0]), 'created team ID')


def seed_wider_beta_setup(
        *,
        profile: Any,
        manifest: Mapping[str, Any],
        guild_id: int = beta_readiness.BETA_GUILD_ID,
        database_factory: Callable[[Any], Any] | None = None,
        writer_guard: Callable[[Any], Any] | None = None,
        writer_check: Callable[[Any], None] | None = None) -> dict[str, Any]:
    reviewed = validate_reviewed_manifest(manifest)
    normalized_guild_id = _validate_profile(profile, guild_id)
    with _mutation_writer_scope(
            profile,
            writer_guard=writer_guard,
            writer_check=writer_check):
        previous = _read_state(profile)
        pending = _read_pending_state(profile)
        if pending is not None:
            raise WiderBetaSetupOwnershipError(
                'Pending WB1.3b ownership evidence requires reconciliation; '
                'seed will not overwrite recoverable evidence.'
            )

        with _database_scope(profile, database_factory) as database:
            with database.atomic():
                _identity(database)
                records = _read_records(database, normalized_guild_id)
                issues = _compatibility_issues(records, normalized_guild_id)
                if issues:
                    raise WiderBetaSetupConflictError('; '.join(issues))
                if previous is not None:
                    _state_error_if_changed(
                        previous, records, reviewed, normalized_guild_id
                    )

                owned_houses: set[str] = set()
                created_team_names: set[str] = set()
                for house_name in EXPECTED_HOUSES:
                    if not records['houses'][house_name]:
                        _insert_house(database, house_name)
                        owned_houses.add(house_name)

                # Houses must be committed in the same transaction before any
                # team insert is attempted.
                records = _read_records(database, normalized_guild_id)
                for team_name, house_name, _role_id in EXPECTED_TEAMS:
                    found = records['teams'][team_name]
                    if found:
                        continue
                    house_rows = records['houses'][house_name]
                    if len(house_rows) != 1:
                        raise WiderBetaSetupConflictError(
                            f'Cannot create team {team_name!r} without exactly one house '
                            f'{house_name!r}.'
                        )
                    _insert_team(
                        database,
                        normalized_guild_id,
                        team_name,
                        int(house_rows[0]['id']),
                    )
                    created_team_names.add(team_name)

                records = _read_records(database, normalized_guild_id)
                issues = _compatibility_issues(records, normalized_guild_id)
                if issues:
                    raise WiderBetaSetupConflictError('; '.join(issues))
                state = _build_state(
                    reviewed,
                    records,
                    previous,
                    normalized_guild_id,
                    owned_houses,
                    created_team_names,
                    created_at=(
                        previous.get('created_at')
                        if previous is not None
                        else None
                    ),
                )
                # This publication is intentionally inside the open DB
                # transaction.  A filesystem fault rolls back all inserts;
                # commit failure leaves only non-authoritative pending proof.
                _write_state(profile, state)

        try:
            state_path = _publish_state(profile)
        except WiderBetaSetupError:
            raise
        result = {
            'schema_version': SETUP_SCHEMA_VERSION,
            'kind': 'wb1_3b_setup_seed_result',
            'status': 'idempotent' if previous is not None else 'seeded',
            'state_file': str(state_path),
            'state': state,
            'boundaries': {
                'discord_mutation_applied': False,
                'command_registration_applied': False,
                'game_mutation_applied': False,
                'elo_mutation_applied': False,
            },
        }
        return result


def _delete_exact(database: Any, table: str, record_id: int) -> None:
    if table not in {'team', 'house'}:
        raise WiderBetaSetupSafetyError('The setup cleanup table is not approved.')
    cursor = database.execute_sql(
        f'DELETE FROM {table} WHERE id = %s',
        (record_id,),
    )
    count = getattr(cursor, 'rowcount', None)
    if count is not None and int(count) != 1:
        raise WiderBetaSetupOwnershipError(
            f'Cleanup expected one {table} row for ID {record_id}, found {count}.'
        )


def cleanup_wider_beta_setup(
        *,
        profile: Any,
        manifest: Mapping[str, Any],
        guild_id: int = beta_readiness.BETA_GUILD_ID,
        confirmed: bool,
        database_factory: Callable[[Any], Any] | None = None,
        writer_guard: Callable[[Any], Any] | None = None,
        writer_check: Callable[[Any], None] | None = None) -> dict[str, Any]:
    if not confirmed:
        raise WiderBetaSetupConfirmationError(
            'WB1.3b cleanup requires the exact --confirm option.'
        )
    reviewed = validate_reviewed_manifest(manifest)
    normalized_guild_id = _validate_profile(profile, guild_id)
    with _mutation_writer_scope(
            profile,
            writer_guard=writer_guard,
            writer_check=writer_check):
        state = _read_state(profile)
        pending = _read_pending_state(profile)
        if pending is not None:
            raise WiderBetaSetupOwnershipError(
                'Pending WB1.3b ownership evidence requires reconciliation; '
                'cleanup will not act on it.'
            )
        if state is None:
            raise WiderBetaSetupOwnershipError(
                'WB1.3b cleanup requires durable ownership evidence from seed.'
            )

        with _database_scope(profile, database_factory) as database:
            with database.atomic():
                _identity(database)
                records = _read_records(database, normalized_guild_id)
                _state_error_if_changed(
                    state, records, reviewed, normalized_guild_id
                )
                issues = _compatibility_issues(records, normalized_guild_id)
                if issues:
                    raise WiderBetaSetupOwnershipError('; '.join(issues))

                owned_team_ids = {
                    int(item['id']) for item in state['teams'] if item['owned']
                }
                owned_house_ids = {
                    int(item['id']) for item in state['houses'] if item['owned']
                }
                for item in state['teams']:
                    if not item['owned']:
                        continue
                    record = records['teams'][item['name']][0]
                    usage = record['usage']
                    if usage['player_count'] or usage['game_side_count']:
                        raise WiderBetaSetupOwnershipError(
                            f'Team {item["name"]!r} is now used by players or game sides.'
                        )

                for item in state['houses']:
                    if not item['owned']:
                        continue
                    record = records['houses'][item['name']][0]
                    usage = record['usage']
                    attached = set(usage['team_ids'])
                    if attached - owned_team_ids:
                        raise WiderBetaSetupOwnershipError(
                            f'House {item["name"]!r} is now shared by an unowned team.'
                        )
                    if usage['preference_count'] or usage['bid_count']:
                        raise WiderBetaSetupOwnershipError(
                            f'House {item["name"]!r} has subsequent persisted usage.'
                        )

                # Delete children first.  These are the only two mutation tables
                # in this operation; game, gameside, lineup, player, and ELO rows
                # are never updated or deleted.
                for item in sorted(state['teams'], key=lambda value: int(value['id']), reverse=True):
                    if item['owned']:
                        _delete_exact(database, 'team', int(item['id']))
                for item in sorted(state['houses'], key=lambda value: int(value['id']), reverse=True):
                    if item['owned']:
                        _delete_exact(database, 'house', int(item['id']))

        # The DB delete is committed, but the lock remains held while stale
        # ownership evidence is removed.  If this fails, do not claim success;
        # reconciliation can verify the rows are gone and remove the evidence.
        _remove_state(profile)
        return {
            'schema_version': SETUP_SCHEMA_VERSION,
            'kind': 'wb1_3b_setup_cleanup_result',
            'status': 'cleaned',
            'removed_team_ids': sorted(owned_team_ids),
            'removed_house_ids': sorted(owned_house_ids),
            'boundaries': {
                'discord_mutation_applied': False,
                'command_registration_applied': False,
                'game_mutation_applied': False,
                'elo_mutation_applied': False,
            },
        }


def _validate_reconciliation_state(
        state: Mapping[str, Any],
        records: Mapping[str, Any],
        manifest: Mapping[str, Any],
        guild_id: int) -> None:
    """Allow evidence removal only after cleanup is externally verified."""

    _validate_state_shape(state, manifest, guild_id)
    for item in state['houses']:
        found = records['houses'][item['name']]
        if item['owned']:
            if found:
                raise WiderBetaSetupOwnershipError(
                    f'Owned house {item["name"]!r} still exists; evidence is not reconcilable.'
                )
            continue
        if len(found) != 1 or int(found[0]['id']) != int(item['id']):
            raise WiderBetaSetupOwnershipError(
                f'Unowned house {item["name"]!r} no longer matches evidence.'
            )
        if _house_baseline(found[0]) != dict(item['baseline']):
            raise WiderBetaSetupOwnershipError(
                f'Unowned house {item["name"]!r} changed after cleanup.'
            )
    for item in state['teams']:
        found = records['teams'][item['name']]
        if item['owned']:
            if found:
                raise WiderBetaSetupOwnershipError(
                    f'Owned team {item["name"]!r} still exists; evidence is not reconcilable.'
                )
            continue
        if len(found) != 1 or int(found[0]['id']) != int(item['id']):
            raise WiderBetaSetupOwnershipError(
                f'Unowned team {item["name"]!r} no longer matches evidence.'
            )
        if _baseline(found[0]) != dict(item['baseline']):
            raise WiderBetaSetupOwnershipError(
                f'Unowned team {item["name"]!r} changed after cleanup.'
            )


def reconcile_cleanup_evidence(
        *,
        profile: Any,
        manifest: Mapping[str, Any],
        guild_id: int = beta_readiness.BETA_GUILD_ID,
        confirmed: bool,
        database_factory: Callable[[Any], Any] | None = None,
        writer_guard: Callable[[Any], Any] | None = None,
        writer_check: Callable[[Any], None] | None = None) -> dict[str, Any]:
    """Clear stale cleanup evidence only after a read-only absence check."""

    if not confirmed:
        raise WiderBetaSetupConfirmationError(
            'WB1.3b evidence reconciliation requires the exact --confirm option.'
        )
    reviewed = validate_reviewed_manifest(manifest)
    normalized_guild_id = _validate_profile(profile, guild_id)
    with _mutation_writer_scope(
            profile,
            writer_guard=writer_guard,
            writer_check=writer_check):
        state = _read_state(profile)
        pending = _read_pending_state(profile)
        if pending is not None:
            raise WiderBetaSetupOwnershipError(
                'Pending seed evidence is not cleanup evidence; retain it for '
                'manual commit-failure reconciliation.'
            )
        if state is None:
            raise WiderBetaSetupOwnershipError(
                'No stale WB1.3b cleanup evidence exists to reconcile.'
            )
        with _database_scope(profile, database_factory) as database:
            with database.atomic():
                database.execute_sql('SET TRANSACTION READ ONLY')
                _identity(database)
                records = _read_records(database, normalized_guild_id)
                _validate_reconciliation_state(
                    state, records, reviewed, normalized_guild_id
                )
        _remove_state(profile)
        return {
            'schema_version': SETUP_SCHEMA_VERSION,
            'kind': 'wb1_3b_setup_cleanup_reconciliation_result',
            'status': 'reconciled',
            'state_file_removed': True,
            'boundaries': {
                'database_mutation_applied': False,
                'discord_mutation_applied': False,
                'command_registration_applied': False,
                'game_mutation_applied': False,
                'elo_mutation_applied': False,
            },
        }


__all__ = [
    'DEFAULT_MANIFEST',
    'EXPECTED_HOUSES',
    'EXPECTED_SHOWCASE_TEAM_TIERS',
    'EXPECTED_TEAM_ROLE_IDS',
    'EXPECTED_TEAMS',
    'SETUP_SCHEMA_VERSION',
    'SETUP_PENDING_STATE_FILENAME',
    'SETUP_STATE_FILENAME',
    'WiderBetaSetupConfirmationError',
    'WiderBetaSetupConflictError',
    'WiderBetaSetupError',
    'WiderBetaSetupOwnershipError',
    'WiderBetaSetupSafetyError',
    'assert_beta_writer_stopped',
    'cleanup_wider_beta_setup',
    'reconcile_cleanup_evidence',
    'plan_wider_beta_setup',
    'seed_wider_beta_setup',
    'status_wider_beta_setup',
    'validate_reviewed_manifest',
]
