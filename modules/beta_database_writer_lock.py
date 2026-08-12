"""Database-scoped exclusion for every supported development beta writer."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Callable

import psycopg2


# ASCII ``PolyBeta`` as one positive signed 64-bit PostgreSQL advisory key.
DATABASE_WRITER_ADVISORY_LOCK_KEY = 0x506F6C7942657461
LOCK_APPLICATION_NAME = 'polybot-development-beta-writer-lock'


class BetaDatabaseWriterLockError(RuntimeError):
    """The database-wide development writer lease could not be proven."""


def _connection_kwargs(profile: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        'dbname': profile.database_name,
        'user': profile.database_user,
        'password': profile.database_password,
        'host': profile.database_host,
        'connect_timeout': 10,
        'application_name': LOCK_APPLICATION_NAME,
        'keepalives': 1,
        'keepalives_idle': 5,
        'keepalives_interval': 2,
        'keepalives_count': 3,
        'options': '-c statement_timeout=5000',
    }
    if profile.database_port is not None:
        values['port'] = int(profile.database_port)
    return values


class BetaDatabaseWriterLock(AbstractContextManager['BetaDatabaseWriterLock']):
    """Hold one PostgreSQL session advisory lock through a writer boundary."""

    def __init__(
        self,
        profile: Any,
        *,
        connect: Callable[..., Any] = psycopg2.connect,
    ):
        self.profile = profile
        self._connect = connect
        self._connection: Any | None = None

    def acquire(self) -> None:
        if self._connection is not None:
            raise BetaDatabaseWriterLockError(
                'The database writer lock is already held by this object.'
            )
        connection = None
        try:
            connection = self._connect(**_connection_kwargs(self.profile))
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT current_database(), current_user, '
                    'pg_try_advisory_lock(%s)',
                    (DATABASE_WRITER_ADVISORY_LOCK_KEY,),
                )
                row = cursor.fetchone()
            if row != (
                self.profile.database_name,
                self.profile.database_user,
                True,
            ):
                raise BetaDatabaseWriterLockError(
                    'Another process holds the development database writer lock '
                    'or the connected database identity is wrong.'
                )
        except BetaDatabaseWriterLockError:
            if connection is not None:
                connection.close()
            raise
        except Exception as exc:
            if connection is not None:
                connection.close()
            raise BetaDatabaseWriterLockError(
                'The development database writer lock could not be acquired.'
            ) from exc
        self._connection = connection

    def check(self) -> None:
        """Fail if the lock-holding PostgreSQL session is no longer usable."""

        if self._connection is None:
            raise BetaDatabaseWriterLockError(
                'The development database writer lock is not held.'
            )
        try:
            with self._connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                if cursor.fetchone() != (1,):
                    raise BetaDatabaseWriterLockError(
                        'The database writer lock health check was invalid.'
                    )
        except BetaDatabaseWriterLockError:
            raise
        except Exception as exc:
            raise BetaDatabaseWriterLockError(
                'The database writer lock session was lost.'
            ) from exc

    def release(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_advisory_unlock(%s)',
                    (DATABASE_WRITER_ADVISORY_LOCK_KEY,),
                )
                if cursor.fetchone() != (True,):
                    raise BetaDatabaseWriterLockError(
                        'The development database writer lock was not held at release.'
                    )
        finally:
            connection.close()

    def __enter__(self) -> 'BetaDatabaseWriterLock':
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()
