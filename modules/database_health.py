"""Small, transaction-safe health checks for the event-loop database handle.

This module is intentionally synchronous.  It is used only by the Discord
event-loop thread, where the legacy shared Peewee connection is owned.  Worker
threads continue to open and close their own connections in their existing
``connection_context`` lifecycles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from peewee import InterfaceError, OperationalError


logger = logging.getLogger('polybot.' + __name__)

HEALTH_CHECK_SQL = 'SELECT 1'
WATCHDOG_INTERVAL_SECONDS = 30.0
WATCHDOG_FAILURE_THRESHOLD = 3
DATABASE_FAILURE_EXIT_STATUS = 75
CONNECTION_ERRORS = (OperationalError, InterfaceError)


def _default_database():
    # Keep ordinary bot construction/import free of the model/database module.
    from modules import models

    return models.db


def _probe(database: Any) -> None:
    """Run exactly one bounded probe and always close its cursor."""

    cursor = None
    try:
        cursor = database.execute_sql(HEALTH_CHECK_SQL)
    finally:
        if cursor is not None:
            cursor.close()


def ensure_connection(database: Any = None) -> bool:
    """Ensure one usable connection for this calling thread.

    A failed probe is retried only after Peewee's connection state is reset and
    a single reconnect is attempted.  No automatic reset is allowed while a
    transaction is active: the original connection exception propagates so a
    potentially committed write is never retried or discarded.
    """

    database = _default_database() if database is None else database
    if database.is_closed():
        database.connect(reuse_if_open=True)

    try:
        _probe(database)
    except CONNECTION_ERRORS:
        if database.in_transaction():
            raise
        # Peewee.close() resets its thread-local state in a finally block even
        # when closing a stale driver connection raises one of these errors.
        try:
            database.close()
        except CONNECTION_ERRORS:
            # A driver may report the stale close as an error after Peewee has
            # already reset its thread-local state.  The one recovery
            # reconnect remains safe; arbitrary close exceptions propagate.
            pass
        database.connect(reuse_if_open=True)
        _probe(database)
    return True


class DatabaseWatchdog:
    """Probe the event-loop connection and fail through the supervisor."""

    def __init__(
        self,
        bot: Any,
        *,
        database: Any = None,
        interval: float = WATCHDOG_INTERVAL_SECONDS,
        failure_threshold: int = WATCHDOG_FAILURE_THRESHOLD,
    ) -> None:
        if interval <= 0:
            raise ValueError('Watchdog interval must be positive.')
        if failure_threshold <= 0:
            raise ValueError('Watchdog failure threshold must be positive.')
        self.bot = bot
        self.database = database
        self.interval = float(interval)
        self.failure_threshold = int(failure_threshold)
        self.consecutive_failures = 0
        self._task: asyncio.Task | None = None
        self._shutdown_requested = False

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    def start(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(
            self.run(),
            name='polybot-database-watchdog',
        )
        return self._task

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _shutdown_for_health(self) -> None:
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        # 75 is the reviewed supervisor-visible failure status used by the
        # durable beta launcher and container contract.
        self.bot._restart_exit_status = DATABASE_FAILURE_EXIT_STATUS
        await self.bot.close()

    async def run(self) -> None:
        # Do not compete with startup schema/identity work.  The first command
        # preflight remains immediate; this infrastructure loop starts its
        # bounded periodic probes after the first interval.
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    ensure_connection(self.database)
                except CONNECTION_ERRORS as exc:
                    self.consecutive_failures += 1
                    logger.error(
                        'Database watchdog probe failed consecutive=%s/%s '
                        'error_type=%s',
                        self.consecutive_failures,
                        self.failure_threshold,
                        type(exc).__name__,
                    )
                    if self.consecutive_failures >= self.failure_threshold:
                        logger.critical(
                            'Database watchdog reached failure threshold; '
                            'closing for supervisor recovery.'
                        )
                        await self._shutdown_for_health()
                        return
                else:
                    if self.consecutive_failures:
                        logger.info(
                            'Database watchdog probe recovered after '
                            'consecutive_failures=%s',
                            self.consecutive_failures,
                        )
                    self.consecutive_failures = 0
        except asyncio.CancelledError:
            raise
