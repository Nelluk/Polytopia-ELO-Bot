#!/usr/bin/env python3
"""Hold the database-wide beta writer lock and fail-stop its parent on loss."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.beta_database_writer_lock import (  # noqa: E402
    BetaDatabaseWriterLock,
)
from modules.beta_operations import assert_beta_profile  # noqa: E402
from runtime_config import load_runtime_profile  # noqa: E402


CHECK_INTERVAL_SECONDS = 0.5


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-pid', required=True, type=int)
    parser.add_argument('--ready-fd', required=True, type=int)
    return parser


def _write_ready(file_descriptor: int, value: str) -> None:
    os.write(file_descriptor, f'{value}\n'.encode('ascii'))
    os.close(file_descriptor)


def _parent_exists(parent_pid: int) -> bool:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.parent_pid <= 1 or args.ready_fd < 3:
        return 2
    writer_lock = None
    ready_sent = False
    try:
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        assert_beta_profile(
            profile,
            environ=os.environ,
            require_service_environment=True,
        )
        writer_lock = BetaDatabaseWriterLock(profile)
        writer_lock.acquire()
        _write_ready(args.ready_fd, 'READY')
        ready_sent = True
        while _parent_exists(args.parent_pid):
            time.sleep(CHECK_INTERVAL_SECONDS)
            writer_lock.check()
        return 0
    except BaseException:
        try:
            _write_ready(args.ready_fd, 'REFUSED')
        except OSError:
            pass
        if ready_sent and _parent_exists(args.parent_pid):
            try:
                os.kill(args.parent_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return 2
    finally:
        if writer_lock is not None:
            try:
                writer_lock.release()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
