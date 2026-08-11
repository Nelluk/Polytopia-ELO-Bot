"""Strict repository-backed configuration for self-service Beta Lab lanes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from modules import beta_readiness


SCHEMA_VERSION = 1
DEFAULT_PATH = Path('data/development/beta_lab_manifest.json')
MAX_BYTES = 8_192


class BetaLabManifestError(ValueError):
    """The tracked self-service manifest is missing or unsafe."""


@dataclass(frozen=True)
class BetaLabManifest:
    guild_id: int
    tester_role_id: int
    opponent_user_ids: tuple[int, ...]
    maximum_active_game_lanes: int
    lease_minutes: int


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BetaLabManifestError(f'{field} must be a positive integer.')
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise BetaLabManifestError(
            f'{field} must be a positive integer.'
        ) from exc
    if normalized <= 0:
        raise BetaLabManifestError(f'{field} must be a positive integer.')
    return normalized


def validate(value: Mapping[str, Any]) -> BetaLabManifest:
    expected = {
        'schema_version',
        'guild_id',
        'tester_role_id',
        'opponent_user_ids',
        'maximum_active_game_lanes',
        'lease_minutes',
    }
    if set(value) != expected:
        raise BetaLabManifestError(
            'The Beta Lab manifest must contain only the reviewed fields.'
        )
    if value.get('schema_version') != SCHEMA_VERSION:
        raise BetaLabManifestError('Unsupported Beta Lab manifest version.')
    guild_id = _positive(value.get('guild_id'), 'guild_id')
    tester_role_id = _positive(value.get('tester_role_id'), 'tester_role_id')
    if guild_id != beta_readiness.BETA_GUILD_ID:
        raise BetaLabManifestError('The Beta Lab manifest targets the wrong guild.')
    if tester_role_id != beta_readiness.BETA_PINNED_TESTER_ROLE_ID:
        raise BetaLabManifestError(
            'The Beta Lab manifest must use the pinned testers role.'
        )
    raw_opponents = value.get('opponent_user_ids')
    if not isinstance(raw_opponents, list):
        raise BetaLabManifestError('opponent_user_ids must be a JSON list.')
    opponents = tuple(
        _positive(item, 'opponent_user_ids entry') for item in raw_opponents
    )
    if len(opponents) != 2 or len(set(opponents)) != 2:
        raise BetaLabManifestError(
            'Exactly two distinct fallback opponents are required.'
        )
    maximum = _positive(
        value.get('maximum_active_game_lanes'),
        'maximum_active_game_lanes',
    )
    if maximum > 3:
        raise BetaLabManifestError(
            'The first self-service release supports at most three lanes.'
        )
    lease_minutes = _positive(value.get('lease_minutes'), 'lease_minutes')
    if not 10 <= lease_minutes <= 60:
        raise BetaLabManifestError('lease_minutes must be between 10 and 60.')
    return BetaLabManifest(
        guild_id=guild_id,
        tester_role_id=tester_role_id,
        opponent_user_ids=opponents,
        maximum_active_game_lanes=maximum,
        lease_minutes=lease_minutes,
    )


def load(
    project_root: Path,
    relative_path: Path = DEFAULT_PATH,
) -> BetaLabManifest:
    root = project_root.resolve()
    path = (root / relative_path).resolve()
    if root not in path.parents:
        raise BetaLabManifestError('The Beta Lab manifest escapes the project root.')
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BetaLabManifestError('The Beta Lab manifest could not be read.') from exc
    if len(raw) > MAX_BYTES:
        raise BetaLabManifestError('The Beta Lab manifest exceeds its size bound.')
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BetaLabManifestError('The Beta Lab manifest is not valid JSON.') from exc
    if not isinstance(value, Mapping):
        raise BetaLabManifestError('The Beta Lab manifest must be a JSON object.')
    return validate(value)
