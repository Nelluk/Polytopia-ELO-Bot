"""Focused coverage for the database-scoped development writer boundary."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
import signal
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import beta_database_writer_lock, beta_wider_setup
from scripts import hold_development_beta_database_lock


def profile():
    return SimpleNamespace(
        database_name='polytopia_dev',
        database_user='polybot_dev',
        database_password='private',
        database_host='postgres',
        database_port=5432,
    )


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params=None):
        self.connection.queries.append((query, params))
        if 'pg_try_advisory_lock' in query:
            self.result = self.connection.acquire_result
        elif 'pg_advisory_unlock' in query:
            self.result = (True,)
        else:
            self.result = (1,)

    def fetchone(self):
        return self.result


class Connection:
    def __init__(self, *, acquire=True):
        self.acquire_result = (
            'polytopia_dev', 'polybot_dev', acquire,
        )
        self.queries = []
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


class BetaDatabaseWriterLockTests(unittest.TestCase):
    def test_exact_database_identity_and_session_lock_are_required(self):
        connection = Connection()
        lock = beta_database_writer_lock.BetaDatabaseWriterLock(
            profile(), connect=lambda **_kwargs: connection,
        )

        lock.acquire()
        self.assertTrue(connection.autocommit)
        lock.check()
        lock.release()

        self.assertTrue(connection.closed)
        self.assertIn(
            'pg_try_advisory_lock',
            connection.queries[0][0],
        )
        self.assertIn(
            'pg_advisory_unlock',
            connection.queries[-1][0],
        )

    def test_competing_database_session_refuses(self):
        connection = Connection(acquire=False)
        lock = beta_database_writer_lock.BetaDatabaseWriterLock(
            profile(), connect=lambda **_kwargs: connection,
        )

        with self.assertRaisesRegex(
            beta_database_writer_lock.BetaDatabaseWriterLockError,
            'Another process',
        ):
            lock.acquire()
        self.assertTrue(connection.closed)

    def test_mutation_scope_holds_file_and_database_locks_through_publish(self):
        events = []

        @contextmanager
        def file_guard(_profile):
            events.append('file-enter')
            try:
                yield
            finally:
                events.append('file-exit')

        database_lock = mock.Mock()
        database_lock.acquire.side_effect = lambda: events.append('database-enter')
        database_lock.release.side_effect = lambda: events.append('database-exit')
        with mock.patch.object(
            beta_wider_setup.beta_database_writer_lock,
            'BetaDatabaseWriterLock',
            return_value=database_lock,
        ):
            with beta_wider_setup._mutation_writer_scope(
                profile(), writer_guard=file_guard,
            ):
                events.append('final-proof')
                events.append('publish')

        self.assertEqual(events, [
            'file-enter', 'database-enter', 'final-proof', 'publish',
            'database-exit', 'file-exit',
        ])

    def test_database_lock_refusal_is_a_fail_closed_setup_error(self):
        database_lock = mock.Mock()
        database_lock.acquire.side_effect = (
            beta_database_writer_lock.BetaDatabaseWriterLockError('held')
        )
        with mock.patch.object(
            beta_wider_setup.beta_database_writer_lock,
            'BetaDatabaseWriterLock',
            return_value=database_lock,
        ), self.assertRaisesRegex(
            beta_wider_setup.WiderBetaSetupSafetyError,
            'Another process',
        ):
            with beta_wider_setup._mutation_writer_scope(
                profile(), writer_guard=lambda _profile: nullcontext(),
            ):
                self.fail('mutation body must not run')

    def test_keeper_initial_refusal_reports_without_signalling_parent(self):
        read_fd, write_fd = os.pipe()
        lock = mock.Mock()
        lock.acquire.side_effect = (
            beta_database_writer_lock.BetaDatabaseWriterLockError('held')
        )
        try:
            with mock.patch.object(
                hold_development_beta_database_lock,
                'load_runtime_profile',
                return_value=profile(),
            ), mock.patch.object(
                hold_development_beta_database_lock,
                'assert_beta_profile',
            ), mock.patch.object(
                hold_development_beta_database_lock,
                'BetaDatabaseWriterLock',
                return_value=lock,
            ), mock.patch.object(
                hold_development_beta_database_lock.os,
                'kill',
            ) as kill:
                result = hold_development_beta_database_lock.main([
                    '--parent-pid', '42',
                    '--ready-fd', str(write_fd),
                ])
            write_fd = -1
            self.assertEqual(result, 2)
            self.assertEqual(os.read(read_fd, 32), b'REFUSED\n')
            kill.assert_not_called()
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_keeper_session_loss_fail_stops_parent(self):
        read_fd, write_fd = os.pipe()
        lock = mock.Mock()
        lock.check.side_effect = (
            beta_database_writer_lock.BetaDatabaseWriterLockError('lost')
        )
        parent_pid = 42
        try:
            with mock.patch.object(
                hold_development_beta_database_lock,
                'load_runtime_profile',
                return_value=profile(),
            ), mock.patch.object(
                hold_development_beta_database_lock,
                'assert_beta_profile',
            ), mock.patch.object(
                hold_development_beta_database_lock,
                'BetaDatabaseWriterLock',
                return_value=lock,
            ), mock.patch.object(
                hold_development_beta_database_lock,
                '_parent_exists',
                return_value=True,
            ), mock.patch.object(
                hold_development_beta_database_lock.time,
                'sleep',
            ), mock.patch.object(
                hold_development_beta_database_lock.os,
                'kill',
            ) as kill:
                result = hold_development_beta_database_lock.main([
                    '--parent-pid', str(parent_pid),
                    '--ready-fd', str(write_fd),
                ])
            write_fd = -1
            self.assertEqual(result, 2)
            self.assertEqual(os.read(read_fd, 32), b'READY\n')
            kill.assert_called_once_with(parent_pid, signal.SIGTERM)
            lock.release.assert_called_once()
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


if __name__ == '__main__':
    unittest.main()
