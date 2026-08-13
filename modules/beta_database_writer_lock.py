"""Database-scoped exclusion for every supported development beta writer."""

from __future__ import annotations

from contextlib import AbstractContextManager
import json
import time
from typing import Any, Callable

import psycopg2

from modules import development_writer_fence


# ASCII ``PolyBeta`` as one positive signed 64-bit PostgreSQL advisory key.
DATABASE_WRITER_ADVISORY_LOCK_KEY = (
    development_writer_fence.DATABASE_WRITER_ADVISORY_LOCK_KEY
)
LOCK_APPLICATION_NAME = 'polybot-development-beta-writer-lock'
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
            getattr(profile, 'environment', None) != 'development'
            or profile.database_name != 'polytopia_dev'
            or profile.database_user != 'polybot_dev'
        ):
            raise BetaDatabaseWriterLockError(
                'The database writer lock is fixed to the exact development '
                'database identity.'
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
        self._generation: int | None = None

    @property
    def generation(self) -> int:
        if self._generation is None:
            raise BetaDatabaseWriterLockError(
                'The development database writer fence is not held.'
            )
        return self._generation

    def acquire(self) -> None:
        if self._connection is not None:
            raise BetaDatabaseWriterLockError(
                'The database writer lock is already held by this object.'
            )
        connection = None
        try:
            connection = self._connect(**_connection_kwargs(self.profile))
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
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE "{development_writer_fence.FENCE_TABLE}" '
                    'SET generation = generation + 1, '
                    'updated_at = CURRENT_TIMESTAMP '
                    'WHERE lock_key = %s AND schema_version = %s '
                    'RETURNING generation',
                    (
                        DATABASE_WRITER_ADVISORY_LOCK_KEY,
                        development_writer_fence.FENCE_SCHEMA_VERSION,
                    ),
                )
                generation_row = cursor.fetchone()
            if (
                generation_row is None
                or type(generation_row[0]) is not int
                or generation_row[0] <= 0
            ):
                raise BetaDatabaseWriterLockError(
                    'The development database writer-fence row is missing.'
                )
            connection.commit()
            connection.autocommit = True
            self._connection = connection
            self._generation = int(generation_row[0])
            self._sleep(self._takeover_grace_seconds)
            self.check()
        except BetaDatabaseWriterLockError:
            self._connection = None
            self._generation = None
            if connection is not None:
                connection.close()
            raise
        except Exception as exc:
            self._connection = None
            self._generation = None
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
                    '), (SELECT generation FROM '
                    f'"{development_writer_fence.FENCE_TABLE}" '
                    'WHERE lock_key = %s)',
                    (
                        DATABASE_WRITER_ADVISORY_LOCK_KEY,
                        DATABASE_WRITER_ADVISORY_LOCK_KEY,
                    ),
                )
                if cursor.fetchone() != (
                    self.profile.database_name,
                    self.profile.database_user,
                    True,
                    self.generation,
                ):
                    raise BetaDatabaseWriterLockError(
                        'The database writer lock or fence generation was lost.'
                    )
        except BetaDatabaseWriterLockError:
            raise
        except Exception as exc:
            raise BetaDatabaseWriterLockError(
                'The database writer lock session was lost.'
            ) from exc

    def publish_evidence(
        self,
        evidence_key: str,
        document: Any,
    ) -> dict[str, Any]:
        """Atomically bind evidence authority to this fenced lock session."""

        if not isinstance(evidence_key, str) or not evidence_key:
            raise BetaDatabaseWriterLockError('The evidence key is invalid.')
        if not isinstance(document, dict):
            raise BetaDatabaseWriterLockError(
                'Database writer evidence must be one JSON object.'
            )
        connection = self._connection
        if connection is None:
            raise BetaDatabaseWriterLockError(
                'The development database writer lock is not held.'
            )
        self.check()
        payload, digest = development_writer_fence.canonical_document(document)
        entry = {
            'schema_version': development_writer_fence.FENCE_SCHEMA_VERSION,
            'evidence_key': evidence_key,
            'fence_generation': self.generation,
            'document_sha256': digest,
            'document': document,
        }
        try:
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE "{development_writer_fence.FENCE_TABLE}" '
                    'SET evidence = jsonb_set('
                    'evidence, ARRAY[%s], %s::jsonb, true), '
                    'updated_at = CURRENT_TIMESTAMP '
                    'WHERE lock_key = %s AND generation = %s '
                    'RETURNING evidence -> %s',
                    (
                        evidence_key,
                        json.dumps(entry, sort_keys=True, separators=(',', ':')),
                        DATABASE_WRITER_ADVISORY_LOCK_KEY,
                        self.generation,
                        evidence_key,
                    ),
                )
                row = cursor.fetchone()
            if row is None or row[0] != entry:
                raise BetaDatabaseWriterLockError(
                    'The database writer fence changed before evidence publication.'
                )
            connection.commit()
        except BetaDatabaseWriterLockError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            raise BetaDatabaseWriterLockError(
                'Database writer evidence could not be published.'
            ) from exc
        finally:
            try:
                connection.autocommit = True
            except Exception:
                pass
        return entry

    def release(self) -> None:
        connection = self._connection
        self._connection = None
        self._generation = None
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
