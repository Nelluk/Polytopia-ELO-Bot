"""Process-wide ownership for active guild-authority mutations.

Database advisory locks serialize the transaction itself.  This coordinator
owns the wider operation: final revalidation, commit, runtime publication, and
any required Discord guild-tree synchronization and verification.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time
from typing import TypeVar


ResultT = TypeVar('ResultT')


class GuildMutationConflict(RuntimeError):
    """Another active-authority mutation owns the process-wide claim."""


@dataclass(frozen=True)
class ActiveGuildMutation:
    operation: str
    guild_id: int
    requester_id: int
    started_monotonic: float


class GuildMutationCoordinator:
    """Fail-fast, re-entrant ownership with repeated-cancellation draining."""

    def __init__(self) -> None:
        self._active: ActiveGuildMutation | None = None
        self._owner_task: asyncio.Task | None = None

    @property
    def active(self) -> ActiveGuildMutation | None:
        return self._active

    async def _drain_owned_task(
        self,
        task: asyncio.Task[ResultT],
        cancellation: asyncio.CancelledError,
    ) -> ResultT:
        latest_cancellation = cancellation
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                latest_cancellation = exc
            except BaseException:
                break
        if task.cancelled():
            raise latest_cancellation
        exception = task.exception()
        if exception is not None:
            raise exception
        raise latest_cancellation

    async def run(
        self,
        *,
        operation: str,
        guild_id: int,
        requester_id: int,
        runner: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        """Run one complete mutation, rejecting unrelated concurrent work."""

        current_task = asyncio.current_task()
        if current_task is not None and current_task is self._owner_task:
            return await runner()
        if self._active is not None:
            active = self._active
            raise GuildMutationConflict(
                'Another guild-configuration change is still finishing '
                f'(`{active.operation}` for guild `{active.guild_id}`). '
                'No new database or Discord write was started; retry after it '
                'finishes.'
            )
        normalized_operation = str(operation).strip()
        if not normalized_operation:
            raise ValueError('A guild-mutation operation label is required.')
        normalized_guild_id = int(guild_id)
        normalized_requester_id = int(requester_id)
        if normalized_guild_id <= 0 or normalized_requester_id <= 0:
            raise ValueError('Guild and requester IDs must be positive integers.')

        task = asyncio.create_task(
            runner(),
            name=f'guild-mutation:{normalized_operation}:{normalized_guild_id}',
        )
        self._active = ActiveGuildMutation(
            operation=normalized_operation,
            guild_id=normalized_guild_id,
            requester_id=normalized_requester_id,
            started_monotonic=time.monotonic(),
        )
        self._owner_task = task
        try:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                return await self._drain_owned_task(task, exc)
        finally:
            if not task.done():
                # Defensive only: every exit above drains the owned task.
                await asyncio.shield(task)
            self._active = None
            self._owner_task = None


guild_mutation_coordinator = GuildMutationCoordinator()


__all__ = [
    'ActiveGuildMutation',
    'GuildMutationConflict',
    'GuildMutationCoordinator',
    'guild_mutation_coordinator',
]
