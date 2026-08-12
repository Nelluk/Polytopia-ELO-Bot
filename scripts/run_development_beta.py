#!/usr/bin/env python3
"""Guarded entry point for the durable development beta service.

The user-level systemd unit and reviewed Compose bot invoke this script rather
than ``bot.py`` directly. It validates the exact development profile and
source provenance, holds the shared single-writer lock across ``exec``, and
then starts the bot with the only supported runtime flag.
"""

from __future__ import annotations

import os
from pathlib import Path
import select
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHARED_DEVELOPMENT_PYTHON = Path('/home/nelluk/PolyBot39-dev/.venv/bin/python')
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.beta_operations import (  # noqa: E402
    BetaWriterLock,
    assert_beta_profile,
    operation_paths,
    validate_beta_launch,
)
from runtime_config import load_runtime_profile  # noqa: E402


DATABASE_LOCK_KEEPER = (
    PROJECT_ROOT / 'scripts/hold_development_beta_database_lock.py'
)
LOCK_KEEPER_READY_TIMEOUT_SECONDS = 15


def _start_database_lock_keeper(python: Path) -> subprocess.Popen:
    """Start the database-scoped lock session before the bot/file lock."""

    read_fd, write_fd = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            (
                str(python),
                str(DATABASE_LOCK_KEEPER),
                '--parent-pid',
                str(os.getpid()),
                '--ready-fd',
                str(write_fd),
            ),
            close_fds=True,
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        write_fd = -1
        readable, _, _ = select.select(
            (read_fd,),
            (),
            (),
            LOCK_KEEPER_READY_TIMEOUT_SECONDS,
        )
        response = os.read(read_fd, 32) if readable else b''
        if response != b'READY\n' or process.poll() is not None:
            raise RuntimeError(
                'The development database writer lock keeper refused startup.'
            )
        return process
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def _stop_database_lock_keeper(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=True,
        )
        checkpoint = validate_beta_launch(
            profile,
            arguments,
            environ=os.environ,
        )
        # Keep this explicit in the launcher as well as the profile check so a
        # copied or hand-edited service cannot silently run a different mode.
        assert_beta_profile(
            profile,
            environ=os.environ,
            require_service_environment=True,
        )
        paths = operation_paths(profile, create=True)
        os.environ['POLYBOT_BETA_CHECKPOINT'] = checkpoint
        # Preserve the venv entry-point path across exec. Resolving this
        # symlink selects the base interpreter and loses its site-packages.
        supervisor = os.environ.get('POLYBOT_RESTART_SUPERVISOR', '').strip()
        python = (
            PROJECT_ROOT / '.venv/bin/python'
            if supervisor == 'compose'
            else SHARED_DEVELOPMENT_PYTHON
        )
        if not python.is_file() or not os.path.samefile(sys.executable, python):
            raise RuntimeError(
                'The durable beta must run with the reviewed development venv.'
            )
        database_lock_keeper = _start_database_lock_keeper(python)
        writer_lock = BetaWriterLock(paths.writer_lock)
        try:
            writer_lock.acquire()
        except BaseException:
            _stop_database_lock_keeper(database_lock_keeper)
            raise
        bot_path = (PROJECT_ROOT / 'bot.py').resolve()
        try:
            os.execv(
                str(python),
                [str(python), str(bot_path), '--skip_tasks'],
            )
        finally:
            # ``execv`` does not return in the real service.  The finally is
            # useful for offline tests and for an unexpected exec failure.
            writer_lock.release()
            _stop_database_lock_keeper(database_lock_keeper)
    except Exception as exc:
        print(f'Development beta launch refused: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
