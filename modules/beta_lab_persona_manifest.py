"""Strict repository policy for the development-only Beta Lab persona."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


RELATIVE_PATH = Path('data/development/beta_lab_persona_manifest.json')
MAX_BYTES = 2048
EXPECTED_GUILD_ID = 478571892832206869
EXPECTED_TESTER_ROLE_ID = 480905534019731476


class BetaLabPersonaManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class BetaLabPersonaManifest:
    guild_id: int
    tester_role_id: int
    house_name: str
    team_name: str
    staff_role_name: str


def validate(value: Any) -> BetaLabPersonaManifest:
    if not isinstance(value, dict) or set(value) != {
        'schema_version', 'guild_id', 'tester_role_id', 'house_name',
        'team_name', 'staff_role_name',
    }:
        raise BetaLabPersonaManifestError('The Beta Lab persona manifest has an invalid shape.')
    if value['schema_version'] != 1:
        raise BetaLabPersonaManifestError('The Beta Lab persona manifest version is unsupported.')
    if value['guild_id'] != EXPECTED_GUILD_ID or value['tester_role_id'] != EXPECTED_TESTER_ROLE_ID:
        raise BetaLabPersonaManifestError('The Beta Lab persona manifest targets are immutable.')
    expected_names = {
        'house_name': 'Beta Lab House',
        'team_name': 'Beta Lab Team',
        'staff_role_name': 'Beta Lab Staff',
    }
    for field, expected in expected_names.items():
        if value[field] != expected:
            raise BetaLabPersonaManifestError(f'{field} must be exactly {expected!r}.')
    return BetaLabPersonaManifest(
        guild_id=value['guild_id'],
        tester_role_id=value['tester_role_id'],
        house_name=value['house_name'],
        team_name=value['team_name'],
        staff_role_name=value['staff_role_name'],
    )


def load(project_root: Path) -> BetaLabPersonaManifest:
    path = Path(project_root) / RELATIVE_PATH
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size > MAX_BYTES:
            raise BetaLabPersonaManifestError('The Beta Lab persona manifest path is unsafe.')
        value = json.loads(path.read_text(encoding='utf-8'))
    except BetaLabPersonaManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BetaLabPersonaManifestError('The Beta Lab persona manifest is unreadable.') from exc
    return validate(value)
