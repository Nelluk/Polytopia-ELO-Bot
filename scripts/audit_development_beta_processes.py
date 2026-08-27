#!/usr/bin/env python3
"""Read-only host-wide pre-activation audit for development beta writers.

This utility deliberately does not stop processes.  It inspects every visible
Linux process under ``/proc`` and reports ``bot.py --skip_tasks`` candidates,
including candidates running from Codex task worktrees.  An operator must
review the PID, ancestry, user, working directory, and command, then stop only
an authorized development beta before rerunning the clear audit.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Iterable


PRODUCTION_ROOT = Path('/srv/polyelo/PolyBot39')
DEVELOPMENT_ROOTS = (
    Path('/home/nelluk/PolyBot39-beta'),
    Path('/app'),
)


@dataclass(frozen=True, slots=True)
class BetaProcessCandidate:
    pid: int
    ppid: int | None
    uid: int | None
    classification: str
    cwd: str
    command: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value['command'] = list(self.command)
        return value


@dataclass(frozen=True, slots=True)
class ProcessAudit:
    candidates: tuple[BetaProcessCandidate, ...]
    unreadable: tuple[str, ...]


def _path_is_under(value: str, root: Path) -> bool:
    normalized = os.path.normpath(value)
    root_value = os.path.normpath(str(root))
    return normalized == root_value or normalized.startswith(root_value + os.sep)


def _is_development_path(value: str) -> bool:
    normalized = os.path.normpath(value)
    if any(_path_is_under(normalized, root) for root in DEVELOPMENT_ROOTS):
        return True
    return any(
        part.startswith('PolyBot39-beta')
        for part in Path(normalized).parts
    )


def _classify(command: Iterable[str], cwd: str) -> str:
    path_values = [argument for argument in command if argument.endswith('/bot.py')]
    path_values.append(cwd)
    if any(_path_is_under(value, PRODUCTION_ROOT) for value in path_values):
        return 'production'
    if any(_is_development_path(value) for value in path_values):
        return 'development'
    return 'unknown'


def _read_ppid(stat_path: Path) -> int | None:
    try:
        value = stat_path.read_text(encoding='utf-8')
    except (FileNotFoundError, OSError):
        return None
    close_paren = value.rfind(')')
    if close_paren < 0:
        return None
    fields = value[close_paren + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _read_uid(status_path: Path) -> int | None:
    try:
        for line in status_path.read_text(encoding='utf-8').splitlines():
            if line.startswith('Uid:'):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, IndexError, ValueError):
        pass
    return None


def _read_candidate(process_path: Path) -> BetaProcessCandidate | None:
    raw_command = (process_path / 'cmdline').read_bytes()
    command = tuple(
        value.decode('utf-8', errors='replace')
        for value in raw_command.split(b'\0')
        if value
    )
    if '--skip_tasks' not in command or not any(
            argument == 'bot.py' or argument.endswith('/bot.py')
            for argument in command):
        return None
    cwd = os.readlink(process_path / 'cwd')
    return BetaProcessCandidate(
        pid=int(process_path.name),
        ppid=_read_ppid(process_path / 'stat'),
        uid=_read_uid(process_path / 'status'),
        classification=_classify(command, cwd),
        cwd=cwd,
        command=command,
    )


def audit_processes(proc_root: Path = Path('/proc')) -> ProcessAudit:
    """Return all visible beta-writer candidates without changing processes."""

    candidates: list[BetaProcessCandidate] = []
    unreadable: list[str] = []
    try:
        process_paths = sorted(
            (path for path in proc_root.iterdir() if path.name.isdigit()),
            key=lambda path: int(path.name),
        )
    except OSError as exc:
        return ProcessAudit((), (f'{proc_root}: {exc}',))
    for process_path in process_paths:
        try:
            candidate = _read_candidate(process_path)
        except FileNotFoundError:
            # A process exited during the read-only scan.
            continue
        except OSError as exc:
            unreadable.append(f'{process_path.name}: {exc}')
            continue
        if candidate is not None:
            candidates.append(candidate)
    return ProcessAudit(tuple(candidates), tuple(unreadable))


def _emit(audit: ProcessAudit, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({
            'candidates': [candidate.as_dict() for candidate in audit.candidates],
            'unreadable': list(audit.unreadable),
        }, ensure_ascii=True, sort_keys=True))
        return
    if audit.unreadable:
        print('Process audit could not inspect every process:', file=sys.stderr)
        for item in audit.unreadable:
            print(f'  {item}', file=sys.stderr)
    if audit.candidates:
        print('Development beta activation is blocked by these bot writers:')
        for candidate in audit.candidates:
            command = ' '.join(candidate.command)
            print(
                f'  pid={candidate.pid} ppid={candidate.ppid} uid={candidate.uid} '
                f'class={candidate.classification} cwd={candidate.cwd} command={command}'
            )
    elif not audit.unreadable:
        print('No bot.py --skip_tasks beta-writer process was found.')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Read-only host-wide development-beta writer audit.',
    )
    parser.add_argument('--json', action='store_true', help='emit JSON output')
    parser.add_argument(
        '--require-clear',
        action='store_true',
        help='return nonzero when a candidate or unreadable process exists',
    )
    args = parser.parse_args(argv)
    audit = audit_processes()
    _emit(audit, as_json=args.json)
    if args.require_clear and (audit.candidates or audit.unreadable):
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
