#!/usr/bin/env python3
"""Guarded entry point for the direct-Compose development bot."""

from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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


def _start_database_lock_keeper(
    python: Path,
) -> tuple[subprocess.Popen, int]:
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
        return process, read_fd
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise
    finally:
        if process is None or process.poll() is not None:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def _stop_process(
    process: subprocess.Popen | None,
    *,
    first_signal: int = signal.SIGTERM,
) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(first_signal)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _normalized_returncode(returncode: int | None) -> int:
    if returncode is None:
        return 2
    return 128 + abs(returncode) if returncode < 0 else returncode


def _supervise(
    keeper: subprocess.Popen,
    keeper_fd: int,
    bot: subprocess.Popen,
) -> int:
    """Never permit the bot to outlive its database-lock keeper."""

    forwarded_signal: int | None = None

    def forward(signum, _frame):
        nonlocal forwarded_signal
        forwarded_signal = signum
        if bot.poll() is None:
            bot.send_signal(signum)

    previous_handlers = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while True:
            keeper_status = keeper.poll()
            if keeper_status is not None:
                _stop_process(bot)
                return 2
            readable, _, _ = select.select((keeper_fd,), (), (), 0.1)
            if readable and os.read(keeper_fd, 1) == b'':
                _stop_process(bot)
                return 2
            bot_status = bot.poll()
            if bot_status is not None:
                return _normalized_returncode(bot_status)
            if forwarded_signal is not None and bot.poll() is None:
                # The handler already forwarded it. Keep supervising until the
                # bot finishes its normal signal cleanup or the keeper fails.
                time.sleep(0)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=True,
        )
        validate_beta_launch(
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
        # Preserve the venv entry-point path across exec. Resolving this
        # symlink selects the base interpreter and loses its site-packages.
        python = PROJECT_ROOT / '.venv/bin/python'
        if not python.is_file() or not os.path.samefile(sys.executable, python):
            raise RuntimeError(
                'The durable beta must run with the reviewed development venv.'
            )
        database_lock_keeper, keeper_fd = _start_database_lock_keeper(python)
        writer_lock = BetaWriterLock(paths.writer_lock)
        try:
            writer_lock.acquire()
        except BaseException:
            _stop_process(database_lock_keeper)
            os.close(keeper_fd)
            raise
        bot_path = (PROJECT_ROOT / 'bot.py').resolve()
        try:
            bot = subprocess.Popen(
                (str(python), str(bot_path), '--skip_tasks'),
                close_fds=True,
            )
            return _supervise(database_lock_keeper, keeper_fd, bot)
        finally:
            _stop_process(locals().get('bot'))
            writer_lock.release()
            _stop_process(database_lock_keeper)
            os.close(keeper_fd)
    except Exception as exc:
        print(f'Development beta launch refused: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
