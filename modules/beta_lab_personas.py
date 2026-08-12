"""Exactly owned Discord/database personas for guided Beta Lab sessions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

import discord

from modules import (
    beta_lab_persona_manifest,
    beta_operations,
    beta_readiness,
    beta_wider_setup,
)


ROLE_STATE_FILENAME = 'beta-lab-persona-roles.json'
DATABASE_STATE_FILENAME = 'beta-lab-persona-database.json'
DATABASE_PENDING_STATE_FILENAME = 'beta-lab-persona-database.pending.json'
DATABASE_EVIDENCE_SCHEMA_VERSION = 2
DATABASE_EVIDENCE_KIND = 'beta_lab_persona_database'
DATABASE_EVIDENCE_ORIGINS = frozenset({'created', 'adopted'})


class BetaLabPersonaError(RuntimeError):
    """Expected fail-closed persona setup or reconciliation refusal."""


@dataclass(frozen=True)
class PersonaRoleBinding:
    team_role_id: int
    staff_role_id: int


@dataclass(frozen=True)
class PersonaStatus:
    ready: bool
    detail: str
    team_role_id: int | None
    staff_role_id: int | None


@dataclass(frozen=True)
class PersonaDatabaseStatus:
    ready: bool
    detail: str
    team_id: int | None
    house_id: int | None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def manifest() -> beta_lab_persona_manifest.BetaLabPersonaManifest:
    try:
        return beta_lab_persona_manifest.load(_project_root())
    except beta_lab_persona_manifest.BetaLabPersonaManifestError as exc:
        raise BetaLabPersonaError(str(exc)) from exc


def _state_path(profile: Any, filename: str, *, create: bool) -> Path:
    try:
        return beta_operations.operation_paths(profile, create=create).state_root / filename
    except beta_operations.BetaOperationsError as exc:
        raise BetaLabPersonaError(str(exc)) from exc


def _read_state(profile: Any, filename: str) -> Mapping[str, Any] | None:
    try:
        value = beta_operations._load_json_file(
            _state_path(profile, filename, create=False),
            absent=None,
            label=filename,
            require_private=True,
        )
    except beta_operations.BetaOperationsError as exc:
        raise BetaLabPersonaError(str(exc)) from exc
    if value is not None and not isinstance(value, dict):
        raise BetaLabPersonaError(f'{filename} has an invalid shape.')
    return value


def _write_state(profile: Any, filename: str, value: Mapping[str, Any]) -> None:
    try:
        beta_operations._write_json(
            _state_path(profile, filename, create=True),
            value,
        )
    except beta_operations.BetaOperationsError as exc:
        raise BetaLabPersonaError(str(exc)) from exc


def _remove_state(profile: Any, filename: str) -> None:
    path = _state_path(profile, filename, create=False)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BetaLabPersonaError(
            f'The owned persona state {filename} could not be removed.'
        ) from exc


def _publish_database_state(
    profile: Any,
    *,
    replace_state: Mapping[str, Any] | None = None,
) -> None:
    pending_path = _state_path(
        profile, DATABASE_PENDING_STATE_FILENAME, create=False,
    )
    state_path = _state_path(profile, DATABASE_STATE_FILENAME, create=False)
    pending = _read_state(profile, DATABASE_PENDING_STATE_FILENAME)
    current = _read_state(profile, DATABASE_STATE_FILENAME)
    if pending is None:
        raise BetaLabPersonaError(
            'The persona database transaction committed without pending ownership evidence.'
        )
    if replace_state is None and current is not None:
        raise BetaLabPersonaError(
            'Published and pending persona ownership evidence conflict; '
            'automatic publication is refused.'
        )
    if replace_state is not None and current != replace_state:
        raise BetaLabPersonaError(
            'Published persona ownership evidence changed during reconciliation.'
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
        raise BetaLabPersonaError(
            'The persona database transaction committed, but ownership evidence '
            'could not be published. Pending evidence was retained; reconcile it '
            'before retrying.'
        ) from exc


def _role_by_id(guild: Any, role_id: int) -> Any | None:
    getter = getattr(guild, 'get_role', None)
    if callable(getter):
        return getter(int(role_id))
    return next(
        (role for role in getattr(guild, 'roles', ()) if int(role.id) == int(role_id)),
        None,
    )


def _role_matches(role: Any, *, role_id: int, name: str) -> bool:
    permissions = getattr(getattr(role, 'permissions', None), 'value', None)
    return bool(
        role is not None
        and int(getattr(role, 'id', 0)) == int(role_id)
        and str(getattr(role, 'name', '')) == name
        and not bool(getattr(role, 'managed', False))
        and int(permissions if permissions is not None else -1) == 0
        and not bool(getattr(role, 'hoist', False))
        and not bool(getattr(role, 'mentionable', False))
    )


def _role_is_assignable(role: Any) -> bool:
    checker = getattr(role, 'is_assignable', None)
    return not callable(checker) or bool(checker())


def load_role_binding(profile: Any, guild: Any) -> PersonaRoleBinding:
    policy = manifest()
    if int(getattr(guild, 'id', 0)) != policy.guild_id:
        raise BetaLabPersonaError('The persona roles target another guild.')
    state = _read_state(profile, ROLE_STATE_FILENAME)
    expected_keys = {
        'schema_version', 'guild_id', 'team_role_id', 'team_role_name',
        'staff_role_id', 'staff_role_name',
    }
    if state is None or set(state) != expected_keys:
        raise BetaLabPersonaError('The owned Beta Lab persona roles have not been prepared.')
    if (
        state['schema_version'] != 1
        or state['guild_id'] != policy.guild_id
        or state['team_role_name'] != policy.team_name
        or state['staff_role_name'] != policy.staff_role_name
    ):
        raise BetaLabPersonaError('The persona role ownership evidence is incompatible.')
    try:
        binding = PersonaRoleBinding(
            team_role_id=int(state['team_role_id']),
            staff_role_id=int(state['staff_role_id']),
        )
    except (TypeError, ValueError) as exc:
        raise BetaLabPersonaError('The persona role ownership IDs are invalid.') from exc
    team_role = _role_by_id(guild, binding.team_role_id)
    staff_role = _role_by_id(guild, binding.staff_role_id)
    if not _role_matches(team_role, role_id=binding.team_role_id, name=policy.team_name):
        raise BetaLabPersonaError('The owned Beta Lab Team role is missing or changed.')
    if not _role_matches(staff_role, role_id=binding.staff_role_id, name=policy.staff_role_name):
        raise BetaLabPersonaError('The owned Beta Lab Staff role is missing or changed.')
    if not _role_is_assignable(team_role) or not _role_is_assignable(staff_role):
        raise BetaLabPersonaError(
            'The bot cannot assign the owned Beta Lab persona roles; review role order.'
        )
    return binding


def role_status(profile: Any, guild: Any) -> PersonaStatus:
    try:
        binding = load_role_binding(profile, guild)
    except BetaLabPersonaError as exc:
        return PersonaStatus(False, str(exc), None, None)
    return PersonaStatus(
        True,
        'The dedicated zero-permission Team and staff-persona roles are ready.',
        binding.team_role_id,
        binding.staff_role_id,
    )


def _role_state_for_database(profile: Any) -> Mapping[str, Any]:
    policy = manifest()
    state = _read_state(profile, ROLE_STATE_FILENAME)
    expected = {
        'schema_version', 'guild_id', 'team_role_id', 'team_role_name',
        'staff_role_id', 'staff_role_name',
    }
    try:
        valid = bool(
            state is not None
            and set(state) == expected
            and state['schema_version'] == 1
            and state['guild_id'] == policy.guild_id
            and state['team_role_name'] == policy.team_name
            and state['staff_role_name'] == policy.staff_role_name
            and int(state['team_role_id']) > 0
            and int(state['staff_role_id']) > 0
            and int(state['team_role_id']) != int(state['staff_role_id'])
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise BetaLabPersonaError(
            'Prepare the exact owned Discord roles before seeding the '
            'database fixture.'
        )
    return state


async def setup_roles(profile: Any, guild: Any) -> PersonaRoleBinding:
    """Create exactly two zero-permission roles and publish ownership evidence."""

    policy = manifest()
    beta_operations.assert_beta_profile(
        profile,
        require_service_environment=False,
    )
    if int(getattr(guild, 'id', 0)) != policy.guild_id:
        raise BetaLabPersonaError('The persona role setup targets another guild.')
    existing_state = _read_state(profile, ROLE_STATE_FILENAME)
    if existing_state is not None:
        return load_role_binding(profile, guild)
    roles = tuple(getattr(guild, 'roles', ()) or ())
    conflicts = tuple(
        role for role in roles
        if str(getattr(role, 'name', '')) in {policy.team_name, policy.staff_role_name}
    )
    if conflicts:
        raise BetaLabPersonaError(
            'A persona role name already exists without ownership evidence; it will not be adopted.'
        )
    created: list[Any] = []
    try:
        for name in (policy.team_name, policy.staff_role_name):
            role = await guild.create_role(
                name=name,
                permissions=discord.Permissions.none(),
                colour=discord.Colour.default(),
                hoist=False,
                mentionable=False,
                reason='Owned PolyBot development Beta Lab persona setup',
            )
            created.append(role)
        if any(
            not _role_matches(role, role_id=int(role.id), name=name)
            or not _role_is_assignable(role)
            for role, name in zip(
                created, (policy.team_name, policy.staff_role_name), strict=True,
            )
        ):
            raise BetaLabPersonaError(
                'A created persona role is not an assignable zero-permission role.'
            )
        value = {
            'schema_version': 1,
            'guild_id': policy.guild_id,
            'team_role_id': int(created[0].id),
            'team_role_name': policy.team_name,
            'staff_role_id': int(created[1].id),
            'staff_role_name': policy.staff_role_name,
        }
        _write_state(profile, ROLE_STATE_FILENAME, value)
        return PersonaRoleBinding(
            team_role_id=int(created[0].id),
            staff_role_id=int(created[1].id),
        )
    except Exception as exc:
        cleanup_errors = []
        for role in reversed(created):
            try:
                await role.delete(reason='Compensating failed Beta Lab persona setup')
            except Exception as cleanup_exc:
                cleanup_errors.append(type(cleanup_exc).__name__)
        if isinstance(exc, BetaLabPersonaError):
            raise
        detail = (
            ' Persona role compensation also failed: ' + ', '.join(cleanup_errors)
            if cleanup_errors else ''
        )
        raise BetaLabPersonaError('The owned persona roles could not be prepared.' + detail) from exc


async def reconcile_roles(profile: Any, guild: Any) -> PersonaRoleBinding:
    """Adopt only one exact unused zero-permission pair after explicit review."""

    policy = manifest()
    beta_operations.assert_beta_profile(
        profile,
        require_service_environment=False,
    )
    if int(getattr(guild, 'id', 0)) != policy.guild_id:
        raise BetaLabPersonaError('The persona role reconciliation targets another guild.')
    if _read_state(profile, ROLE_STATE_FILENAME) is not None:
        return load_role_binding(profile, guild)

    roles = tuple(getattr(guild, 'roles', ()) or ())
    selected: list[Any] = []
    for name in (policy.team_name, policy.staff_role_name):
        matches = tuple(
            role for role in roles
            if str(getattr(role, 'name', '')) == name
        )
        if len(matches) != 1:
            raise BetaLabPersonaError(
                f'Persona role reconciliation requires exactly one {name!r} role.'
            )
        role = matches[0]
        if (
                not _role_matches(role, role_id=int(role.id), name=name)
                or not _role_is_assignable(role)):
            raise BetaLabPersonaError(
                f'The existing {name!r} role is not an assignable '
                'zero-permission persona role.'
            )
        if tuple(getattr(role, 'members', ()) or ()):
            raise BetaLabPersonaError(
                f'The existing {name!r} role has members and cannot be adopted.'
            )
        selected.append(role)

    value = {
        'schema_version': 1,
        'guild_id': policy.guild_id,
        'team_role_id': int(selected[0].id),
        'team_role_name': policy.team_name,
        'staff_role_id': int(selected[1].id),
        'staff_role_name': policy.staff_role_name,
    }
    _write_state(profile, ROLE_STATE_FILENAME, value)
    return load_role_binding(profile, guild)


def _roles(binding: PersonaRoleBinding, guild: Any) -> tuple[Any, Any]:
    return (
        _role_by_id(guild, binding.team_role_id),
        _role_by_id(guild, binding.staff_role_id),
    )


async def set_member_active(profile: Any, guild: Any, member: Any, *, active: bool) -> None:
    binding = load_role_binding(profile, guild)
    roles = _roles(binding, guild)
    if any(role is None for role in roles):
        raise BetaLabPersonaError('The owned persona roles are unavailable.')
    member_roles = {int(role.id) for role in getattr(member, 'roles', ())}
    if active:
        missing = tuple(role for role in roles if int(role.id) not in member_roles)
        if missing:
            try:
                await member.add_roles(
                    *missing,
                    reason='Active owned PolyBot Beta Lab guided session',
                    atomic=True,
                )
            except Exception as exc:
                raise BetaLabPersonaError('The guided-session roles could not be assigned.') from exc
    else:
        present = tuple(role for role in roles if int(role.id) in member_roles)
        if present:
            try:
                await member.remove_roles(
                    *present,
                    reason='Finished owned PolyBot Beta Lab guided session',
                    atomic=True,
                )
            except Exception as exc:
                raise BetaLabPersonaError('The guided-session roles could not be removed.') from exc


async def reconcile_members(
    profile: Any,
    guild: Any,
    *,
    active_owner_ids: Iterable[int],
) -> int:
    """Remove exact persona roles from members without an active owned session."""

    binding = load_role_binding(profile, guild)
    allowed = {int(value) for value in active_owner_ids}
    role_members: dict[int, Any] = {}
    for role in _roles(binding, guild):
        for member in tuple(getattr(role, 'members', ()) or ()):
            role_members[int(member.id)] = member
    if len(role_members) > 24:
        raise BetaLabPersonaError('The persona-role member bound was exceeded.')
    for member_id, member in role_members.items():
        if member_id not in allowed:
            await set_member_active(profile, guild, member, active=False)
    return sum(member_id not in allowed for member_id in role_members)


async def revoke_members_on_startup(profile: Any, guild: Any) -> int:
    """Revoke all owned personas; active sessions reauthorize in the panel."""

    if _read_state(profile, ROLE_STATE_FILENAME) is None:
        return 0
    return await reconcile_members(profile, guild, active_owner_ids=())


def _database_rows(database: Any, policy: beta_lab_persona_manifest.BetaLabPersonaManifest):
    houses = beta_wider_setup._rows(
        database,
        'SELECT id, name FROM house WHERE name = %s ORDER BY id',
        (policy.house_name,),
    )
    teams = beta_wider_setup._rows(
        database,
        'SELECT id, name, guild_id, house_id, is_hidden, is_archived, league_tier '
        'FROM team WHERE guild_id = %s AND name = %s ORDER BY id',
        (policy.guild_id, policy.team_name),
    )
    return houses, teams


def _database_rows_by_evidence_ids(
    database: Any,
    evidence: Mapping[str, Any],
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Look up pending creation identities without relying on mutable names."""

    try:
        house_id = int(evidence['house_id'])
        team_id = int(evidence['team_id'])
    except (KeyError, TypeError, ValueError) as exc:
        raise BetaLabPersonaError(
            'Pending persona evidence has invalid database identities.'
        ) from exc
    if house_id <= 0 or team_id <= 0:
        raise BetaLabPersonaError(
            'Pending persona evidence has invalid database identities.'
        )
    houses = beta_wider_setup._rows(
        database,
        'SELECT id FROM house WHERE id = %s',
        (house_id,),
    )
    teams = beta_wider_setup._rows(
        database,
        'SELECT id FROM team WHERE id = %s',
        (team_id,),
    )
    return tuple(houses), tuple(teams)


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def _database_baseline(
    database: Any,
    policy: beta_lab_persona_manifest.BetaLabPersonaManifest,
) -> dict[str, Any]:
    """Read and validate the complete pristine and unused fixture state."""

    houses = beta_wider_setup._house(database, policy.house_name)
    teams = beta_wider_setup._team(database, policy.guild_id, policy.team_name)
    if len(houses) != 1 or len(teams) != 1:
        raise BetaLabPersonaError(
            'Database reconciliation requires exactly one persona House and Team.'
        )
    house = houses[0]
    team = teams[0]
    house_usage = beta_wider_setup._house_usage(database, house['id'])
    team_usage = beta_wider_setup._team_usage(database, team['id'])
    try:
        exact = bool(
            int(house['id']) > 0
            and house['name'] == policy.house_name
            and house['emoji'] == ''
            and house['image_url'] is None
            and house['league_tokens'] == 0
            and int(team['id']) > 0
            and team['name'] == policy.team_name
            and team['guild_id'] == policy.guild_id
            and team['house_id'] == house['id']
            and team['house_name'] == policy.house_name
            and not team['hidden']
            and not team['archived']
            and team['league_tier'] == 1
            and team['external_server'] is None
            and team['elo'] == 1000
            and team['elo_alltime'] == 1000
            and team['emoji'] == ''
            and team['image_url'] is None
            and team['pro_league']
            and house_usage['team_ids'] == [team['id']]
            and house_usage['team_names'] == [policy.team_name]
            and house_usage['team_guild_ids'] == [policy.guild_id]
            and house_usage['preference_count'] == 0
            and house_usage['bid_count'] == 0
            and team_usage['player_count'] == 0
            and team_usage['game_side_count'] == 0
        )
    except (KeyError, TypeError, ValueError):
        exact = False
    if not exact:
        raise BetaLabPersonaError(
            'The existing persona database fixture is not exact, pristine, and unused.'
        )
    return {
        'house': {
            'id': int(house['id']),
            'name': house['name'],
            'emoji': house['emoji'],
            'image_url': house['image_url'],
            'league_tokens': int(house['league_tokens']),
            'usage': {
                'team_ids': [int(value) for value in house_usage['team_ids']],
                'team_names': list(house_usage['team_names']),
                'team_guild_ids': [
                    int(value) for value in house_usage['team_guild_ids']
                ],
                'preference_count': int(house_usage['preference_count']),
                'bid_count': int(house_usage['bid_count']),
            },
        },
        'team': {
            'id': int(team['id']),
            'name': team['name'],
            'guild_id': int(team['guild_id']),
            'house_id': int(team['house_id']),
            'house_name': team['house_name'],
            'hidden': bool(team['hidden']),
            'archived': bool(team['archived']),
            'league_tier': int(team['league_tier']),
            'external_server': team['external_server'],
            'elo': int(team['elo']),
            'elo_alltime': int(team['elo_alltime']),
            'emoji': team['emoji'],
            'image_url': team['image_url'],
            'pro_league': bool(team['pro_league']),
            'usage': {
                'player_count': int(team_usage['player_count']),
                'game_side_count': int(team_usage['game_side_count']),
            },
        },
    }


def _evidence_from_baseline(
    baseline: Mapping[str, Any],
    policy: beta_lab_persona_manifest.BetaLabPersonaManifest,
    *,
    origin: str,
) -> dict[str, Any]:
    if origin not in DATABASE_EVIDENCE_ORIGINS:
        raise BetaLabPersonaError('The persona database evidence origin is invalid.')
    copied = json.loads(json.dumps(baseline, sort_keys=True))
    return {
        'schema_version': DATABASE_EVIDENCE_SCHEMA_VERSION,
        'kind': DATABASE_EVIDENCE_KIND,
        'origin': origin,
        'guild_id': policy.guild_id,
        'house_id': int(copied['house']['id']),
        'house_name': policy.house_name,
        'team_id': int(copied['team']['id']),
        'team_name': policy.team_name,
        'baseline_sha256': _canonical_digest(copied),
        'baseline': copied,
    }


def _database_evidence_matches(
    evidence: Mapping[str, Any] | None,
    baseline: Mapping[str, Any],
    policy: beta_lab_persona_manifest.BetaLabPersonaManifest,
) -> bool:
    try:
        if not isinstance(evidence, Mapping):
            return False
        origin = evidence['origin']
        if origin not in DATABASE_EVIDENCE_ORIGINS:
            return False
        return dict(evidence) == _evidence_from_baseline(
            baseline,
            policy,
            origin=origin,
        )
    except (KeyError, TypeError, ValueError, BetaLabPersonaError):
        return False


def _legacy_database_evidence_matches(
    evidence: Mapping[str, Any] | None,
    policy: beta_lab_persona_manifest.BetaLabPersonaManifest,
    baseline: Mapping[str, Any] | None = None,
) -> bool:
    try:
        valid = bool(
            isinstance(evidence, Mapping)
            and set(evidence) == {
                'schema_version', 'guild_id', 'house_id', 'house_name',
                'team_id', 'team_name',
            }
            and evidence['schema_version'] == 1
            and evidence['guild_id'] == policy.guild_id
            and evidence['house_name'] == policy.house_name
            and evidence['team_name'] == policy.team_name
            and int(evidence['house_id']) > 0
            and int(evidence['team_id']) > 0
        )
        if valid and baseline is not None:
            valid = bool(
                int(evidence['house_id']) == int(baseline['house']['id'])
                and int(evidence['team_id']) == int(baseline['team']['id'])
            )
        return valid
    except (KeyError, TypeError, ValueError):
        return False


def _read_only_database_baseline(
    database: Any,
    policy: beta_lab_persona_manifest.BetaLabPersonaManifest,
) -> dict[str, Any]:
    with database.atomic():
        database.execute_sql('SET TRANSACTION READ ONLY')
        beta_wider_setup._identity(database)
        return _database_baseline(database, policy)


def _database_adoption_evidence(
    database: Any,
    policy: beta_lab_persona_manifest.BetaLabPersonaManifest,
) -> dict[str, Any]:
    """Return evidence only for one exact pristine and unused fixture pair."""

    return _evidence_from_baseline(
        _database_baseline(database, policy),
        policy,
        origin='adopted',
    )


def database_status(profile: Any) -> PersonaDatabaseStatus:
    policy = manifest()
    try:
        beta_readiness.validate_database_profile(profile, policy.guild_id)
        state = _read_state(profile, DATABASE_STATE_FILENAME)
        pending = _read_state(profile, DATABASE_PENDING_STATE_FILENAME)
        if state is not None and pending is not None:
            return PersonaDatabaseStatus(
                False,
                'Published and pending persona ownership evidence conflict.',
                None,
                None,
            )
        if pending is not None:
            return PersonaDatabaseStatus(
                False,
                'Pending persona database evidence requires exact reconciliation.',
                None,
                None,
            )
        if state is None:
            return PersonaDatabaseStatus(
                False,
                'The owned Beta Lab House/Team fixture is not prepared.',
                None,
                None,
            )
        database = beta_wider_setup._default_database_factory(profile)
        with database.connection_context():
            baseline = _read_only_database_baseline(database, policy)
    except Exception as exc:
        if isinstance(exc, BetaLabPersonaError):
            return PersonaDatabaseStatus(False, str(exc), None, None)
        return PersonaDatabaseStatus(
            False,
            'The Beta Lab persona database fixture could not be verified.',
            None,
            None,
        )
    if _legacy_database_evidence_matches(state, policy, baseline):
        return PersonaDatabaseStatus(
            False,
            'Legacy persona database evidence requires explicit exact reconciliation.',
            None,
            None,
        )
    if not _database_evidence_matches(state, baseline, policy):
        return PersonaDatabaseStatus(
            False,
            'The owned Beta Lab House/Team fixture conflicts with its evidence.',
            None,
            None,
        )
    return PersonaDatabaseStatus(
        True,
        'The owned Beta Lab House/Team database fixture is ready.',
        int(baseline['team']['id']),
        int(baseline['house']['id']),
    )


def seed_database(profile: Any) -> PersonaDatabaseStatus:
    """Create the exact House/Team fixture while the durable writer is stopped."""

    policy = manifest()
    beta_readiness.validate_database_profile(profile, policy.guild_id)
    _role_state_for_database(profile)
    with beta_wider_setup._mutation_writer_scope(profile):
        if _read_state(profile, DATABASE_PENDING_STATE_FILENAME) is not None:
            raise BetaLabPersonaError(
                'Pending persona database evidence requires reconciliation; '
                'seed will not overwrite it.'
            )
        database = beta_wider_setup._default_database_factory(profile)
        with database.connection_context():
            with database.atomic():
                beta_wider_setup._identity(database)
                houses, teams = _database_rows(database, policy)
                prior = _read_state(profile, DATABASE_STATE_FILENAME)
                if prior is None and (houses or teams):
                    raise BetaLabPersonaError(
                        'Persona fixture names already exist without ownership evidence; they will not be adopted.'
                    )
                if prior is not None:
                    baseline = _database_baseline(database, policy)
                    if _legacy_database_evidence_matches(prior, policy, baseline):
                        raise BetaLabPersonaError(
                            'Legacy persona database evidence requires reconciliation '
                            'before seed can be retried.'
                        )
                    if not _database_evidence_matches(prior, baseline, policy):
                        raise BetaLabPersonaError(
                            'The persona database fixture conflicts with its ownership evidence.'
                        )
                    return PersonaDatabaseStatus(
                        True,
                        'The owned Beta Lab House/Team database fixture is ready.',
                        int(baseline['team']['id']),
                        int(baseline['house']['id']),
                    )
                house_id = beta_wider_setup._insert_house(database, policy.house_name)
                team_id = beta_wider_setup._insert_team(
                    database,
                    policy.guild_id,
                    policy.team_name,
                    house_id,
                )
                database.execute_sql(
                    'UPDATE team SET league_tier = %s WHERE id = %s',
                    (1, team_id),
                )
                evidence = _evidence_from_baseline(
                    _database_baseline(database, policy),
                    policy,
                    origin='created',
                )
                _write_state(
                    profile,
                    DATABASE_PENDING_STATE_FILENAME,
                    evidence,
                )
            committed = _read_only_database_baseline(database, policy)
            if not _database_evidence_matches(evidence, committed, policy):
                raise BetaLabPersonaError(
                    'The committed persona fixture changed before ownership '
                    'evidence could be published.'
                )
        _publish_database_state(profile)
    return database_status(profile)


def reconcile_pending_database(profile: Any) -> PersonaDatabaseStatus:
    """Reconcile pending evidence or adopt one exact pristine unused pair."""

    policy = manifest()
    beta_readiness.validate_database_profile(profile, policy.guild_id)
    _role_state_for_database(profile)
    with beta_wider_setup._mutation_writer_scope(profile):
        state = _read_state(profile, DATABASE_STATE_FILENAME)
        pending = _read_state(profile, DATABASE_PENDING_STATE_FILENAME)
        if state is not None and pending is not None:
            raise BetaLabPersonaError(
                'Published and pending persona ownership evidence conflict; '
                'automatic reconciliation is refused.'
            )
        database = beta_wider_setup._default_database_factory(profile)
        with database.connection_context():
            replace_state = None
            if state is not None:
                baseline = _read_only_database_baseline(database, policy)
                if _database_evidence_matches(state, baseline, policy):
                    return PersonaDatabaseStatus(
                        True,
                        'The owned Beta Lab House/Team database fixture is ready.',
                        int(baseline['team']['id']),
                        int(baseline['house']['id']),
                    )
                if not _legacy_database_evidence_matches(state, policy, baseline):
                    raise BetaLabPersonaError(
                        'Published persona ownership evidence does not exactly '
                        'match the database; manual review is required.'
                    )
                evidence = _evidence_from_baseline(
                    baseline,
                    policy,
                    origin='adopted',
                )
                replace_state = state
            elif pending is not None:
                with database.atomic():
                    database.execute_sql('SET TRANSACTION READ ONLY')
                    beta_wider_setup._identity(database)
                    houses, teams = _database_rows(database, policy)
                    if not houses and not teams:
                        rolled_back_creation = (
                            _legacy_database_evidence_matches(pending, policy)
                            or (
                                isinstance(pending, Mapping)
                                and pending.get('origin') == 'created'
                                and _database_evidence_matches(
                                    pending,
                                    pending.get('baseline', {}),
                                    policy,
                                )
                            )
                        )
                        if rolled_back_creation:
                            recorded_houses, recorded_teams = (
                                _database_rows_by_evidence_ids(database, pending)
                            )
                            if recorded_houses or recorded_teams:
                                raise BetaLabPersonaError(
                                    'Pending persona database identities still '
                                    'exist with changed discovery fields; manual '
                                    'review is required.'
                                )
                            _remove_state(
                                profile,
                                DATABASE_PENDING_STATE_FILENAME,
                            )
                            return PersonaDatabaseStatus(
                                False,
                                'The rolled-back persona seed was reconciled; '
                                'seed may be retried.',
                                None,
                                None,
                            )
                        raise BetaLabPersonaError(
                            'Pending adoption evidence has no exact database '
                            'fixture; manual review is required.'
                        )
                    baseline = _database_baseline(database, policy)
                if _database_evidence_matches(pending, baseline, policy):
                    evidence = dict(pending)
                elif _legacy_database_evidence_matches(pending, policy, baseline):
                    evidence = _evidence_from_baseline(
                        baseline,
                        policy,
                        origin='adopted',
                    )
                else:
                    raise BetaLabPersonaError(
                        'The pending persona evidence does not exactly match the '
                        'database; manual review is required.'
                    )
            else:
                evidence = _evidence_from_baseline(
                    _read_only_database_baseline(database, policy),
                    policy,
                    origin='adopted',
                )

            if pending != evidence:
                _write_state(
                    profile,
                    DATABASE_PENDING_STATE_FILENAME,
                    evidence,
                )
            final_baseline = _read_only_database_baseline(database, policy)
            if not _database_evidence_matches(evidence, final_baseline, policy):
                raise BetaLabPersonaError(
                    'The persona fixture changed between reconciliation proof '
                    'and publication; pending evidence was retained.'
                )
        _publish_database_state(profile, replace_state=replace_state)
    return database_status(profile)
