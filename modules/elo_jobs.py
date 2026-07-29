"""Coordination and execution for serialized ELO mutation jobs."""

from __future__ import annotations

import asyncio
import datetime
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, TypeVar


ResultT = TypeVar('ResultT')


@dataclass(frozen=True)
class EloJob:
    operation: str
    game_id: int | None
    requester_id: int | None
    requester_name: str
    started_at: datetime.datetime


class EloJobConflict(RuntimeError):
    def __init__(self, active_job: EloJob):
        self.active_job = active_job
        super().__init__(
            f'{active_job.operation} for game '
            f'{active_job.game_id or "all games"} is already running'
        )


class EloJobCoordinator:
    """Serialize ELO mutations without occupying the Discord event loop."""

    def __init__(self):
        self._state_lock = threading.Lock()
        self._active_job: EloJob | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix='polybot-elo',
        )

    @property
    def active_job(self) -> EloJob | None:
        with self._state_lock:
            return self._active_job

    @property
    def is_active(self) -> bool:
        return self.active_job is not None

    def shutdown(self) -> None:
        """Release executor resources (primarily for isolated tests)."""

        self._executor.shutdown(wait=True)

    def _start(
        self,
        *,
        operation: str,
        game_id: int | None,
        requester_id: int | None,
        requester_name: str,
    ) -> EloJob:
        job = EloJob(
            operation=operation,
            game_id=game_id,
            requester_id=requester_id,
            requester_name=requester_name,
            started_at=datetime.datetime.now(datetime.timezone.utc),
        )
        with self._state_lock:
            if self._active_job is not None:
                raise EloJobConflict(self._active_job)
            self._active_job = job
        return job

    def _finish(self, job: EloJob) -> None:
        with self._state_lock:
            if self._active_job is job:
                self._active_job = None

    @contextmanager
    def claimed(
        self,
        *,
        operation: str,
        game_id: int | None,
        requester_id: int | None,
        requester_name: str,
    ):
        """Claim coordinator state for a synchronous maintenance operation."""

        job = self._start(
            operation=operation,
            game_id=game_id,
            requester_id=requester_id,
            requester_name=requester_name,
        )
        try:
            yield job
        finally:
            self._finish(job)

    async def run(
        self,
        *,
        operation: str,
        game_id: int | None,
        requester_id: int | None,
        requester_name: str,
        worker: Callable[..., ResultT],
        worker_args: tuple = (),
        before_submit: Callable[[], None] | None = None,
        after_complete: Callable[[], None] | None = None,
    ) -> ResultT:
        job = self._start(
            operation=operation,
            game_id=game_id,
            requester_id=requester_id,
            requester_name=requester_name,
        )

        try:
            if before_submit is not None:
                before_submit()
            loop = asyncio.get_running_loop()
            call = functools.partial(worker, *worker_args)
            concurrent_future = self._executor.submit(call)
            future = asyncio.wrap_future(concurrent_future, loop=loop)
            completed = asyncio.Event()
            concurrent_future.add_done_callback(
                lambda _future: loop.call_soon_threadsafe(completed.set)
            )
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # A running thread cannot be cancelled. Keep the job reserved
                # until its transaction actually finishes.
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                await completed.wait()
                concurrent_future.result()
                raise asyncio.CancelledError
        finally:
            try:
                if after_complete is not None:
                    after_complete()
            finally:
                self._finish(job)


elo_job_coordinator = EloJobCoordinator()
