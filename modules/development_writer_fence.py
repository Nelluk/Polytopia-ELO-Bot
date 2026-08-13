"""Development-only schema and evidence authority for writer fencing."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import Any, Mapping


FENCE_TABLE = 'development_writer_fence'
FENCE_SCHEMA_VERSION = 1
DATABASE_WRITER_ADVISORY_LOCK_KEY = 0x506F6C7942657461
PERSONA_EVIDENCE_KEY = 'beta_lab_persona_database'
FENCE_INSTALL_TAKEOVER_GRACE_SECONDS = 1.0


class DevelopmentWriterFenceError(RuntimeError):
    """The development writer-fence schema or evidence is unsafe."""


@dataclass(frozen=True)
class WriterFenceTarget:
    environment: str
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    database_host: str | None
    database_port: int | None


CREATE_TABLE_SQL = f'''CREATE TABLE IF NOT EXISTS "{FENCE_TABLE}" (
    lock_key BIGINT PRIMARY KEY,
    schema_version SMALLINT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    evidence JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT development_writer_fence_key_ck
        CHECK (lock_key = {DATABASE_WRITER_ADVISORY_LOCK_KEY}),
    CONSTRAINT development_writer_fence_schema_ck
        CHECK (schema_version = {FENCE_SCHEMA_VERSION}),
    CONSTRAINT development_writer_fence_generation_ck
        CHECK (generation >= 0),
    CONSTRAINT development_writer_fence_evidence_ck
        CHECK (jsonb_typeof(evidence) = 'object')
)'''

INSERT_ROW_SQL = f'''INSERT INTO "{FENCE_TABLE}"
    (lock_key, schema_version, generation, evidence)
VALUES (%s, %s, 0, '{{}}'::jsonb)
ON CONFLICT (lock_key) DO NOTHING'''


def confirmation_token(target: WriterFenceTarget) -> str:
    return (
        'INSTALL DEVELOPMENT WRITER FENCE '
        f'{target.database_name} AS {target.database_user}'
    )


def validate_target(target: WriterFenceTarget) -> WriterFenceTarget:
    if not isinstance(target, WriterFenceTarget):
        raise DevelopmentWriterFenceError(
            'A frozen development writer-fence target is required.'
        )
    if (
        target.environment != 'development'
        or target.database_name != 'polytopia_dev'
        or target.database_user != 'polybot_dev'
    ):
        raise DevelopmentWriterFenceError(
            'Writer fencing is fixed to development/polytopia_dev/polybot_dev.'
        )
    if not target.database_password:
        raise DevelopmentWriterFenceError(
            'Writer fencing requires explicit database authentication.'
        )
    return target


def apply_schema(
    connection: Any,
    target: WriterFenceTarget,
    *,
    confirmation: str,
    takeover_grace_seconds: float = FENCE_INSTALL_TAKEOVER_GRACE_SECONDS,
) -> None:
    target = validate_target(target)
    expected = confirmation_token(target)
    if confirmation != expected:
        raise DevelopmentWriterFenceError(
            f'Writer-fence confirmation mismatch; expected {expected!r}.'
        )
    if takeover_grace_seconds < 0:
        raise DevelopmentWriterFenceError(
            'The writer-fence takeover grace cannot be negative.'
        )
    lock_acquired = False
    failure: BaseException | None = None
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT current_database(), current_user, '
                'pg_try_advisory_lock(%s)',
                (DATABASE_WRITER_ADVISORY_LOCK_KEY,),
            )
            if cursor.fetchone() != (
                target.database_name,
                target.database_user,
                True,
            ):
                raise DevelopmentWriterFenceError(
                    'Writer-fence database identity mismatch or another '
                    'writer holds the advisory lock.'
                )
            lock_acquired = True
        time.sleep(takeover_grace_seconds)
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT EXISTS (SELECT 1 FROM pg_locks '
                'WHERE pid = pg_backend_pid() '
                "AND locktype = 'advisory' AND granted AND objsubid = 1 "
                'AND ((classid::bigint << 32) | objid::bigint) = %s)',
                (DATABASE_WRITER_ADVISORY_LOCK_KEY,),
            )
            if cursor.fetchone() != (True,):
                raise DevelopmentWriterFenceError(
                    'Writer-fence install lock was lost during takeover grace.'
                )
            cursor.execute(CREATE_TABLE_SQL)
            cursor.execute(
                INSERT_ROW_SQL,
                (DATABASE_WRITER_ADVISORY_LOCK_KEY, FENCE_SCHEMA_VERSION),
            )
        verify_schema(connection, target)
        connection.commit()
    except DevelopmentWriterFenceError as exc:
        failure = exc
    except Exception as exc:
        failure = DevelopmentWriterFenceError(
            'The development writer-fence schema could not be installed.'
        )
        failure.__cause__ = exc
    except BaseException as exc:
        failure = exc
    try:
        connection.rollback()
    except Exception as exc:
        if failure is None:
            failure = DevelopmentWriterFenceError(
                'The writer-fence transaction cleanup failed.'
            )
            failure.__cause__ = exc
    if lock_acquired:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_advisory_unlock(%s)',
                    (DATABASE_WRITER_ADVISORY_LOCK_KEY,),
                )
                if cursor.fetchone() != (True,):
                    raise DevelopmentWriterFenceError(
                        'The writer-fence install lock was not released.'
                    )
            connection.commit()
        except Exception as exc:
            try:
                connection.close()
            except Exception:
                pass
            if failure is None:
                failure = DevelopmentWriterFenceError(
                    'The writer-fence install-lock cleanup failed.'
                )
                failure.__cause__ = exc
    if failure is not None:
        raise failure


def verify_schema(connection: Any, target: WriterFenceTarget) -> None:
    target = validate_target(target)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database(), current_user')
            if cursor.fetchone() != (target.database_name, target.database_user):
                raise DevelopmentWriterFenceError(
                    'Writer-fence database identity mismatch.'
                )
            cursor.execute(
                f'SELECT schema_version, generation, evidence '
                f'FROM "{FENCE_TABLE}" WHERE lock_key = %s',
                (DATABASE_WRITER_ADVISORY_LOCK_KEY,),
            )
            row = cursor.fetchone()
        if (
            row is None
            or row[0] != FENCE_SCHEMA_VERSION
            or isinstance(row[1], bool)
            or not isinstance(row[1], int)
            or row[1] < 0
            or not isinstance(row[2], Mapping)
        ):
            raise DevelopmentWriterFenceError(
                'The development writer-fence row has an invalid shape.'
            )
    except DevelopmentWriterFenceError:
        raise
    except Exception as exc:
        raise DevelopmentWriterFenceError(
            'The development writer-fence schema is missing or unreadable.'
        ) from exc


def canonical_document(value: Mapping[str, Any]) -> tuple[str, str]:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    )
    return payload, hashlib.sha256(payload.encode('utf-8')).hexdigest()


def load_evidence(database: Any, evidence_key: str) -> Mapping[str, Any] | None:
    cursor = database.execute_sql(
        f'SELECT evidence -> %s FROM "{FENCE_TABLE}" WHERE lock_key = %s',
        (evidence_key, DATABASE_WRITER_ADVISORY_LOCK_KEY),
    )
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    if not isinstance(row[0], Mapping):
        raise DevelopmentWriterFenceError(
            'The database writer evidence has an invalid shape.'
        )
    return dict(row[0])


def evidence_matches(
    entry: Mapping[str, Any] | None,
    *,
    evidence_key: str,
    document: Mapping[str, Any],
) -> bool:
    if not isinstance(entry, Mapping):
        return False
    payload, digest = canonical_document(document)
    del payload
    return bool(
        set(entry) == {
            'schema_version', 'evidence_key', 'fence_generation',
            'document_sha256', 'document',
        }
        and entry.get('schema_version') == FENCE_SCHEMA_VERSION
        and entry.get('evidence_key') == evidence_key
        and type(entry.get('fence_generation')) is int
        and entry['fence_generation'] > 0
        and entry.get('document_sha256') == digest
        and entry.get('document') == document
    )
