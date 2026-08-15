"""Focused offline tests for the Discord-triggered local backup."""

import asyncio
from pathlib import Path
import unittest
from unittest import mock

from modules import backup_service


class FakeProcess:
    def __init__(self, returncode=0, release=None):
        self.returncode = returncode
        self.release = release

    async def wait(self):
        if self.release is not None:
            await self.release.wait()
        return self.returncode


class BackupServiceTests(unittest.IsolatedAsyncioTestCase):
    root = Path(__file__).resolve().parents[1]

    def test_obsolete_racknerd_path_is_not_used(self):
        sources = (
            self.root / 'modules/administration.py',
            self.root / 'modules/backup_service.py',
        )
        for source in sources:
            self.assertNotIn(
                '/home/nelluk/backup_db.sh',
                source.read_text(encoding='utf-8'),
            )

    async def test_exact_greencloud_wrapper_argv(self):
        process = FakeProcess()
        with mock.patch.object(
            asyncio,
            'create_subprocess_exec',
            new=mock.AsyncMock(return_value=process),
        ) as create_process:
            self.assertTrue(await backup_service.run_local_backup())

        create_process.assert_awaited_once_with(
            '/srv/polyelo/bin/polyelo-backup',
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def test_execution_does_not_block_the_event_loop(self):
        release = asyncio.Event()
        process = FakeProcess(release=release)
        with mock.patch.object(
            asyncio,
            'create_subprocess_exec',
            new=mock.AsyncMock(return_value=process),
        ):
            backup_task = asyncio.create_task(
                backup_service.run_local_backup()
            )
            await asyncio.sleep(0)
            self.assertFalse(backup_task.done())
            release.set()
            self.assertTrue(await backup_task)

    async def test_success_and_failure_messages_are_sanitized(self):
        secret_output = 'database-password /private/backup/path'
        self.assertEqual(
            backup_service.result_message(True),
            'Local database backup completed successfully.',
        )
        self.assertEqual(
            backup_service.result_message(False),
            'Local database backup failed. Check the protected server logs.',
        )
        self.assertNotIn(secret_output, backup_service.result_message(True))
        self.assertNotIn(secret_output, backup_service.result_message(False))

        for returncode, expected in ((0, True), (1, False)):
            with self.subTest(returncode=returncode), mock.patch.object(
                asyncio,
                'create_subprocess_exec',
                new=mock.AsyncMock(return_value=FakeProcess(returncode)),
            ):
                self.assertEqual(
                    await backup_service.run_local_backup(),
                    expected,
                )

    async def test_spawn_failure_is_sanitized(self):
        with mock.patch.object(
            asyncio,
            'create_subprocess_exec',
            new=mock.AsyncMock(side_effect=OSError('private host detail')),
        ):
            self.assertFalse(await backup_service.run_local_backup())
        self.assertNotIn(
            'private host detail',
            backup_service.result_message(False),
        )


if __name__ == '__main__':
    unittest.main()
