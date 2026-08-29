"""Database-scoped exclusion for supported PolyBot writer processes."""

from __future__ import annotations

from contextlib import AbstractContextManager
import time
from typing import Any, Callable

import psycopg2

# ASCII ``PolyBeta`` as one positive signed 64-bit PostgreSQL advisory key.
DATABASE_WRITER_ADVISORY_LOCK_KEY = 0x506F6C7942657461
LOCK_APPLICATION_NAME = 'polybot-database-writer-lock'
# The supervisor polls keeper process/pipe state every 100 ms. A successor
# retains the advisory lock without touching application data for this longer
# interval, then revalidates its session and fence before returning.
FAILSTOP_TAKEOVER_GRACE_SECONDS = 1.0


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
        takeover_grace_seconds: float = FAILSTOP_TAKEOVER_GRACE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if (
            getattr(profile, 'environment', None) not in {
                'development', 'production'
            }
            or not isinstance(getattr(profile, 'database_name', None), str)
            or not profile.database_name.strip()
            or not isinstance(getattr(profile, 'database_user', None), str)
            or not profile.database_user.strip()
        ):
            raise BetaDatabaseWriterLockError(
                'The database writer lock requires an explicit supported '
                'runtime and database identity.'
            )
        if takeover_grace_seconds < 0:
            raise BetaDatabaseWriterLockError(
                'The writer takeover grace cannot be negative.'
            )
        self.profile = profile
        self._connect = connect
        self._takeover_grace_seconds = takeover_grace_seconds
        self._sleep = sleep
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
                    'Another process holds the database writer lock '
                    'or the connected database identity is wrong.'
                )
            self._connection = connection
            self._sleep(self._takeover_grace_seconds)
            self.check()
        except BetaDatabaseWriterLockError:
            self._connection = None
            if connection is not None:
                connection.close()
            raise
        except Exception as exc:
            self._connection = None
            if connection is not None:
                connection.close()
            raise BetaDatabaseWriterLockError(
                'The development database writer lock could not be acquired.'
            ) from exc

    def check(self) -> None:
        """Fail if the lock-holding PostgreSQL session is no longer usable."""

        if self._connection is None:
            raise BetaDatabaseWriterLockError(
                'The development database writer lock is not held.'
            )
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    'SELECT current_database(), current_user, EXISTS ('
                    'SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid() '
                    "AND locktype = 'advisory' AND granted "
                    'AND objsubid = 1 '
                    'AND ((classid::bigint << 32) | objid::bigint) = %s'
                    ')',
                    (DATABASE_WRITER_ADVISORY_LOCK_KEY,),
                )
                if cursor.fetchone() != (
                    self.profile.database_name,
                    self.profile.database_user,
                    True,
                ):
                    raise BetaDatabaseWriterLockError(
                        'The database writer lock session was lost.'
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
