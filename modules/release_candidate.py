"""Strict, non-secret M7/R-002 release-candidate evidence validation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_BYTES = 65_536
BRANCH = 'codex/database-slash-modernization'
ROLLBACK_SHA = 'c35e2f1d0011709d233c0aa8afa258602b457635'
PRODUCTION_BOT_ID = 484067640302764042
PRODUCTION_DATABASE = 'polytopia2'
MAIN_GUILD_ID = 283436219780825088
POLYCHAMPIONS_GUILD_ID = 447883341463814144

REQUIRED_SOURCE_PATHS = (
    'uv.lock',
    'deploy/systemd/polytopia.service',
    'deploy/systemd/polytopia-modernization-canary.conf',
    'scripts/migrate_player_timezone_production.py',
    'scripts/manage_application_commands.py',
    'docs/MODERNIZATION_PRODUCTION_CUTOVER.md',
    'release-candidate-manifests/tester-instructions-draft.md',
)
REQUIRED_FINDINGS = (
    'B1', 'B2', 'B3',
    'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'H7', 'H8',
    'M1', 'M2', 'M3', 'M4', 'M5', 'M6',
    'L1', 'N1', 'N2',
)
REQUIRED_GATES = (
    'cutover_review',
    'offline_suite',
    'development_database_suite',
    'bounded_beta_matrix',
)
_SHA = re.compile(r'^[0-9a-f]{40}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_RELEASE_ID = re.compile(r'^[a-z0-9][a-z0-9-]{2,63}$')
_SECRET_MARKERS = (
    'postgresql://',
    'postgres://',
    'begin private key',
    'discord_key=',
    'psql_password=',
    'token=',
)


class ReleaseCandidateError(ValueError):
    """The release-candidate record is incomplete, stale, or unsafe."""


@dataclass(frozen=True)
class GateEvidence:
    status: str
    candidate_sha: str
    command: str
    total: int
    passed: int
    skipped: int
    failures: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseCandidateManifest:
    release_id: str
    candidate_sha: str
    rollback_sha: str
    source_digests: Mapping[str, str]
    finding_checkpoints: Mapping[str, tuple[str, ...]]
    gates: Mapping[str, GateEvidence]

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            f'{name} is {gate.status}'
            for name, gate in self.gates.items()
            if gate.status != 'pass'
        )


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ReleaseCandidateError(
            f'{label} must contain only the reviewed fields.'
        )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseCandidateError(f'{label} must be a JSON object.')
    return value


def _positive_or_zero(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReleaseCandidateError(f'{label} must be a non-negative integer.')
    return value


def _strings(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReleaseCandidateError(f'{label} must be a JSON list.')
    normalized = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in normalized):
        raise ReleaseCandidateError(f'{label} entries must be nonempty strings.')
    if not allow_empty and not normalized:
        raise ReleaseCandidateError(f'{label} must not be empty.')
    return normalized


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ReleaseCandidateError(f'{label} must be a full lowercase Git SHA.')
    return value


def _reject_secret_material(value: Any) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_secret_material(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_material(item)
    elif isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            raise ReleaseCandidateError(
                'The release-candidate record appears to contain secret material.'
            )


def _production_plan(value: Any) -> None:
    plan = _mapping(value, 'production_plan')
    _exact_keys(plan, {
        'expected_bot_id', 'database', 'api_enabled',
        'global_commands_expected_empty', 'guilds', 'omitted_capabilities',
    }, 'production_plan')
    if plan['expected_bot_id'] != PRODUCTION_BOT_ID:
        raise ReleaseCandidateError('production_plan targets the wrong bot.')
    if plan['database'] != PRODUCTION_DATABASE:
        raise ReleaseCandidateError('production_plan targets the wrong database.')
    if plan['api_enabled'] is not False:
        raise ReleaseCandidateError('The production API must remain disabled.')
    if plan['global_commands_expected_empty'] is not True:
        raise ReleaseCandidateError('The global command tree must be expected empty.')
    if plan['omitted_capabilities'] != [
            'beta_testing', 'elo_maintenance', 'operator']:
        raise ReleaseCandidateError('The omitted production capabilities changed.')

    guilds = plan['guilds']
    if not isinstance(guilds, list) or len(guilds) != 2:
        raise ReleaseCandidateError('Exactly two production guild routes are required.')
    expected = {
        MAIN_GUILD_ID: {
            'guild_id': MAIN_GUILD_ID,
            'name': 'Polytopia Main',
            'staff_help_channel': 742857671237042176,
            'first_helper_role': 'ELO-Helper',
            'capabilities': ['tools_support'],
        },
        POLYCHAMPIONS_GUILD_ID: {
            'guild_id': POLYCHAMPIONS_GUILD_ID,
            'name': 'PolyChampions',
            'staff_help_channel': 742832436047511572,
            'first_helper_role': 'Helper',
            'capabilities': [
                'core_user', 'house', 'league', 'squad', 'team', 'tools_support',
            ],
        },
    }
    observed: dict[int, Mapping[str, Any]] = {}
    for entry in guilds:
        route = _mapping(entry, 'production_plan guild route')
        _exact_keys(route, {
            'guild_id', 'name', 'staff_help_channel', 'first_helper_role',
            'capabilities',
        }, 'production_plan guild route')
        guild_id = route.get('guild_id')
        if isinstance(guild_id, bool) or not isinstance(guild_id, int):
            raise ReleaseCandidateError('Production guild IDs must be integers.')
        if guild_id in observed:
            raise ReleaseCandidateError('Production guild routes must be unique.')
        observed[guild_id] = route
    if observed != expected:
        raise ReleaseCandidateError(
            'Production guild routes differ from the reviewed support/canary plan.'
        )


def _findings(value: Any) -> Mapping[str, tuple[str, ...]]:
    raw = value
    if not isinstance(raw, list):
        raise ReleaseCandidateError('adversarial_findings must be a JSON list.')
    observed: dict[str, tuple[str, ...]] = {}
    for item in raw:
        finding = _mapping(item, 'adversarial finding')
        _exact_keys(finding, {'id', 'status', 'checkpoints'}, 'adversarial finding')
        finding_id = finding.get('id')
        if finding_id not in REQUIRED_FINDINGS or finding_id in observed:
            raise ReleaseCandidateError('Adversarial finding IDs are missing or duplicated.')
        if finding.get('status') != 'resolved':
            raise ReleaseCandidateError(f'Finding {finding_id} is not resolved.')
        checkpoints = _strings(finding.get('checkpoints'), f'{finding_id} checkpoints')
        observed[finding_id] = tuple(
            _sha(checkpoint, f'{finding_id} checkpoint') for checkpoint in checkpoints
        )
    if set(observed) != set(REQUIRED_FINDINGS):
        raise ReleaseCandidateError('The release record omits a reviewed finding.')
    return observed


def _gates(value: Any, candidate_sha: str) -> Mapping[str, GateEvidence]:
    raw = _mapping(value, 'gates')
    if set(raw) != set(REQUIRED_GATES):
        raise ReleaseCandidateError('The release record omits a required R-002 gate.')
    gates: dict[str, GateEvidence] = {}
    for name in REQUIRED_GATES:
        gate = _mapping(raw[name], f'{name} gate')
        _exact_keys(gate, {
            'status', 'candidate_sha', 'command', 'total', 'passed', 'skipped',
            'failures', 'evidence',
        }, f'{name} gate')
        status = gate.get('status')
        if status not in {'pass', 'pending', 'blocked'}:
            raise ReleaseCandidateError(f'{name} has an unsupported status.')
        if _sha(gate.get('candidate_sha'), f'{name} candidate_sha') != candidate_sha:
            raise ReleaseCandidateError(f'{name} refers to another candidate.')
        command = gate.get('command')
        if not isinstance(command, str) or not command.strip():
            raise ReleaseCandidateError(f'{name} must record its exact command or action.')
        total = _positive_or_zero(gate.get('total'), f'{name} total')
        passed = _positive_or_zero(gate.get('passed'), f'{name} passed')
        skipped = _positive_or_zero(gate.get('skipped'), f'{name} skipped')
        failures = _strings(gate.get('failures'), f'{name} failures', allow_empty=True)
        evidence = _strings(gate.get('evidence'), f'{name} evidence')
        if total != passed + skipped + len(failures):
            raise ReleaseCandidateError(f'{name} counts do not reconcile.')
        if status == 'pass' and failures:
            raise ReleaseCandidateError(f'{name} cannot pass with failures.')
        if status == 'pass' and passed == 0:
            raise ReleaseCandidateError(f'{name} cannot pass without evidence items.')
        gates[name] = GateEvidence(
            status=status,
            candidate_sha=candidate_sha,
            command=command,
            total=total,
            passed=passed,
            skipped=skipped,
            failures=failures,
            evidence=evidence,
        )
    return gates


def validate(value: Mapping[str, Any]) -> ReleaseCandidateManifest:
    _reject_secret_material(value)
    _exact_keys(value, {
        'schema_version', 'release_id', 'candidate_sha', 'rollback_sha',
        'branch', 'source_digests', 'production_plan', 'adversarial_findings',
        'gates',
    }, 'release-candidate record')
    if value.get('schema_version') != SCHEMA_VERSION:
        raise ReleaseCandidateError('Unsupported release-candidate schema version.')
    release_id = value.get('release_id')
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise ReleaseCandidateError('release_id must be a bounded lowercase slug.')
    candidate_sha = _sha(value.get('candidate_sha'), 'candidate_sha')
    rollback_sha = _sha(value.get('rollback_sha'), 'rollback_sha')
    if rollback_sha != ROLLBACK_SHA or rollback_sha == candidate_sha:
        raise ReleaseCandidateError('rollback_sha is not the reviewed production baseline.')
    if value.get('branch') != BRANCH:
        raise ReleaseCandidateError('The release record names the wrong branch.')

    digests = _mapping(value.get('source_digests'), 'source_digests')
    if set(digests) != set(REQUIRED_SOURCE_PATHS):
        raise ReleaseCandidateError('source_digests must cover the exact critical files.')
    if any(not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
           for digest in digests.values()):
        raise ReleaseCandidateError('Every source digest must be lowercase SHA-256.')
    _production_plan(value.get('production_plan'))
    findings = _findings(value.get('adversarial_findings'))
    gates = _gates(value.get('gates'), candidate_sha)
    return ReleaseCandidateManifest(
        release_id=release_id,
        candidate_sha=candidate_sha,
        rollback_sha=rollback_sha,
        source_digests=dict(digests),
        finding_checkpoints=findings,
        gates=gates,
    )


def load(path: Path) -> ReleaseCandidateManifest:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReleaseCandidateError('The release-candidate record could not be read.') from exc
    if len(raw) > MAX_BYTES:
        raise ReleaseCandidateError('The release-candidate record exceeds its size bound.')
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseCandidateError('The release-candidate record is not valid JSON.') from exc
    return validate(_mapping(value, 'release-candidate record'))


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ('git', *args),
        cwd=root,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ReleaseCandidateError('Git could not verify release-candidate evidence.')
    return result.stdout


def verify_repository(manifest: ReleaseCandidateManifest, root: Path) -> None:
    project_root = root.resolve()
    for checkpoint in (manifest.candidate_sha, manifest.rollback_sha):
        _git(project_root, 'cat-file', '-e', f'{checkpoint}^{{commit}}')
    _git(
        project_root,
        'merge-base', '--is-ancestor', manifest.rollback_sha, manifest.candidate_sha,
    )
    head = _git(project_root, 'rev-parse', 'HEAD').decode().strip()
    _git(project_root, 'merge-base', '--is-ancestor', manifest.candidate_sha, head)

    for path, expected_digest in manifest.source_digests.items():
        content = _git(project_root, 'show', f'{manifest.candidate_sha}:{path}')
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise ReleaseCandidateError(f'Candidate digest mismatch for {path}.')
    for finding_id, checkpoints in manifest.finding_checkpoints.items():
        for checkpoint in checkpoints:
            try:
                _git(project_root, 'merge-base', '--is-ancestor', checkpoint,
                     manifest.candidate_sha)
            except ReleaseCandidateError as exc:
                raise ReleaseCandidateError(
                    f'Finding {finding_id} cites evidence outside the candidate.'
                ) from exc


def summary(manifest: ReleaseCandidateManifest) -> dict[str, Any]:
    return {
        'release_id': manifest.release_id,
        'candidate_sha': manifest.candidate_sha,
        'rollback_sha': manifest.rollback_sha,
        'ready': not manifest.blockers,
        'blockers': list(manifest.blockers),
        'gates': {
            name: gate.status for name, gate in manifest.gates.items()
        },
    }
