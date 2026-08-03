#!/usr/bin/env python3
"""Guarded entry point for the durable development beta service.

The user-level systemd unit invokes this script rather than ``bot.py``
directly.  It validates the exact development profile, requires a clean
reviewed checkout, holds the single-writer lock across ``exec``, and then
starts the bot with the only supported runtime flag.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

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
        writer_lock = BetaWriterLock(paths.writer_lock)
        writer_lock.acquire()
        os.environ['POLYBOT_BETA_CHECKPOINT'] = checkpoint
        # Preserve the venv entry-point path across exec. Resolving this
        # symlink selects the base interpreter and loses the venv's
        # site-packages (including discord.py).
        python = PROJECT_ROOT / '.venv' / 'bin' / 'python'
        if not python.is_file() or not os.path.samefile(sys.executable, python):
            raise RuntimeError(
                'The durable beta must run with the reviewed development venv.'
            )
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
    except Exception as exc:
        print(f'Development beta launch refused: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
