"""Asynchronous runner for the GreenCloud local backup wrapper."""

import asyncio


BACKUP_COMMAND = ('/srv/polyelo/bin/polyelo-backup',)
BACKUP_STARTED_MESSAGE = 'Starting the local database backup.'
BACKUP_SUCCESS_MESSAGE = 'Local database backup completed successfully.'
BACKUP_FAILURE_MESSAGE = (
    'Local database backup failed. Check the protected server logs.'
)


async def run_local_backup() -> bool:
    """Run the fixed local wrapper without blocking Discord's event loop."""

    try:
        process = await asyncio.create_subprocess_exec(
            *BACKUP_COMMAND,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    return await process.wait() == 0


def result_message(success: bool) -> str:
    """Return a fixed Discord-safe result without process output."""

    return BACKUP_SUCCESS_MESSAGE if success else BACKUP_FAILURE_MESSAGE
