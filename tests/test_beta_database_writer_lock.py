"""Focused coverage for the database-scoped development writer boundary."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import beta_database_writer_lock, beta_wider_setup
from scripts import hold_development_beta_database_lock, run_development_beta


def profile():
    return SimpleNamespace(
        environment='development',
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
        elif 'FROM pg_locks' in query:
            self.result = (
                'polytopia_dev', 'polybot_dev', True,
            )
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
    def test_supported_out_of_process_writers_contend_on_shared_lock(self):
        root = Path(__file__).resolve().parents[1]
        required_sources = (
            'bot.py',
            'scripts/manage_dev_fixtures.py',
            'modules/beta_lab_personas.py',
            'modules/beta_wider_setup.py',
            'scripts/manage_guild_configuration_storage.py',
            'scripts/manage_guild_configuration_drafts.py',
            'scripts/manage_guild_configuration_delegation.py',
            'scripts/bootstrap_first_guild_configuration.py',
            'scripts/bootstrap_development_database.py',
            'scripts/migrate_player_timezone.py',
            'scripts/migrate_player_badges.py',
        )
        for relative in required_sources:
            with self.subTest(source=relative):
                source = (root / relative).read_text(encoding='utf-8')
                self.assertTrue(
                    'BetaDatabaseWriterLock' in source
                    or '_mutation_writer_scope' in source,
                    f'{relative} does not visibly enter the shared writer lock',
                )

    def test_exact_database_identity_and_session_lock_are_required(self):
        connection = Connection()
        lock = beta_database_writer_lock.BetaDatabaseWriterLock(
            profile(), connect=lambda **_kwargs: connection,
            takeover_grace_seconds=0,
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
            takeover_grace_seconds=0,
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

    def test_keeper_session_loss_closes_liveness_pipe_for_supervisor(self):
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
            kill.assert_not_called()
            lock.release.assert_called_once()
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_supervisor_stops_bot_when_keeper_dies_abruptly(self):
        for abrupt_signal in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=abrupt_signal):
                read_fd, write_fd = os.pipe()
                keeper = subprocess.Popen(
                    (sys.executable, '-c', 'import time; time.sleep(60)'),
                    pass_fds=(write_fd,),
                )
                os.close(write_fd)
                bot = subprocess.Popen(
                    (sys.executable, '-c', 'import time; time.sleep(60)'),
                )
                timer = threading.Timer(
                    0.1,
                    keeper.send_signal,
                    args=(abrupt_signal,),
                )
                successor_observation = []

                def acquire_successor():
                    while keeper.poll() is None:
                        threading.Event().wait(0.001)

                    def takeover_grace(_seconds):
                        deadline = threading.Event()
                        for _ in range(1000):
                            if bot.poll() is not None:
                                break
                            deadline.wait(0.001)

                    lock = beta_database_writer_lock.BetaDatabaseWriterLock(
                        profile(),
                        connect=lambda **_kwargs: Connection(),
                        takeover_grace_seconds=1,
                        sleep=takeover_grace,
                    )
                    lock.acquire()
                    successor_observation.append(bot.poll())
                    lock.release()

                successor = threading.Thread(target=acquire_successor)
                successor.start()
                timer.start()
                try:
                    self.assertEqual(
                        run_development_beta._supervise(
                            keeper, read_fd, bot,
                        ),
                        2,
                    )
                    self.assertIsNotNone(bot.poll())
                    successor.join(timeout=2)
                    self.assertFalse(successor.is_alive())
                    self.assertEqual(len(successor_observation), 1)
                    self.assertIsNotNone(successor_observation[0])
                finally:
                    timer.cancel()
                    run_development_beta._stop_process(bot)
                    run_development_beta._stop_process(keeper)
                    os.close(read_fd)


if __name__ == '__main__':
    unittest.main()
