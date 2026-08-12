#!/usr/bin/env python3
"""Plan or execute a stopped-writer host-development logical export."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from modules import beta_operations  # noqa: E402
from modules.container_host_database_export import (  # noqa: E402
    HostDevelopmentExportError,
    build_plan,
    export_database,
    format_plan,
)
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Export only the stopped host polytopia_dev database for an isolated container restore drill.'
    )
    parser.add_argument('--confirm', help='exact confirmation printed by the plan')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if os.environ.get('POLYBOT_ENV') != 'development':
            raise HostDevelopmentExportError('POLYBOT_ENV must be exactly development.')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT, environ=os.environ, create_directories=False,
        )
        beta_operations.assert_clean_checkout(PROJECT_ROOT)
        checkpoint = beta_operations.current_checkpoint(PROJECT_ROOT)
        plan = build_plan(profile, checkpoint)
        print(format_plan(plan))
        if not args.confirm:
            print('Plan only; no database connection or output write was attempted.')
            return 0
        result = export_database(
            profile,
            plan,
            confirmation=args.confirm,
            timestamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        )
    except (HostDevelopmentExportError, RuntimeConfigurationError,
            beta_operations.BetaOperationsError) as exc:
        print(f'Host development export refused: {exc}', file=sys.stderr)
        return 2
    print('Host development database export complete.')
    print(f'archive: {result.archive.name}')
    print(f'bytes: {result.bytes_written}')
    print(f'sha256: {result.sha256}')
    print(f'session samples: before={result.sessions_before} after={result.sessions_after}')
    print('The session samples are observations, not proof that no transient session existed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
