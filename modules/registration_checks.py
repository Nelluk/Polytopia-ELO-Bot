"""Bounded account-registration reads for shared prefix command checks."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from modules import models


@dataclass(frozen=True)
class RegistrationCheckRequest:
    discord_id: int


@dataclass(frozen=True)
class RegistrationCheckResult:
    discord_id: int
    registered: bool


_registration_check_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='registration-check-read',
)


def load_registration_check(
    request: RegistrationCheckRequest,
) -> RegistrationCheckResult:
    """Read account-wide registration on one worker-local connection."""

    discord_id = int(request.discord_id)
    if discord_id <= 0:
        raise ValueError('The Discord member ID must be positive.')
    with models.db.connection_context():
        registered = (
            models.DiscordMember
            .select(models.DiscordMember.discord_id)
            .where(models.DiscordMember.discord_id == discord_id)
            .exists()
        )
    return RegistrationCheckResult(
        discord_id=discord_id,
        registered=bool(registered),
    )


async def _drain_future(future: Future):
    """Drain a submitted thread operation before propagating cancellation."""

    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            future.result()
        except BaseException:
            pass
        raise
    return future.result()


async def run_registration_check(
    request: RegistrationCheckRequest,
    *,
    executor=None,
) -> RegistrationCheckResult:
    """Submit one bounded registration read without blocking Discord."""

    selected_executor = executor or _registration_check_executor
    return await _drain_future(
        selected_executor.submit(load_registration_check, request)
    )
