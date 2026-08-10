"""Identity-gated worker for the process-start DiscordMember ban snapshot."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import importlib


MAX_DISCORD_BANS = 1_000
MAX_POLYTOPIA_BANS = 1_000
MAX_POLYTOPIA_ID_LENGTH = 64


class StartupBanReconciliationError(RuntimeError):
    """The configured startup ban snapshot is invalid."""


@dataclass(frozen=True)
class StartupBanReconciliationRequest:
    discord_ids: tuple[int, ...]
    polytopia_ids: tuple[str, ...]


@dataclass(frozen=True)
class StartupBanReconciliationResult:
    reset_rows: int
    discord_rows: int
    polytopia_rows: int


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-startup-ban-reconciliation',
)


def _load_models():
    return importlib.import_module('modules.models')


def _validate_request(
    request: StartupBanReconciliationRequest,
) -> StartupBanReconciliationRequest:
    if not isinstance(request, StartupBanReconciliationRequest):
        raise StartupBanReconciliationError(
            'A frozen startup ban request is required.'
        )
    if len(request.discord_ids) > MAX_DISCORD_BANS:
        raise StartupBanReconciliationError(
            f'Discord ban IDs exceed the {MAX_DISCORD_BANS}-entry bound.'
        )
    if len(request.polytopia_ids) > MAX_POLYTOPIA_BANS:
        raise StartupBanReconciliationError(
            f'Polytopia ban IDs exceed the {MAX_POLYTOPIA_BANS}-entry bound.'
        )
    if (
        any(not isinstance(value, int) or value <= 0 for value in request.discord_ids)
        or len(set(request.discord_ids)) != len(request.discord_ids)
    ):
        raise StartupBanReconciliationError(
            'Discord ban IDs must be unique positive integers.'
        )
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > MAX_POLYTOPIA_ID_LENGTH
        for value in request.polytopia_ids
    ) or len(set(request.polytopia_ids)) != len(request.polytopia_ids):
        raise StartupBanReconciliationError(
            'Polytopia ban IDs must be unique bounded non-empty strings.'
        )
    return request


def reconcile_startup_bans(
    request: StartupBanReconciliationRequest,
) -> StartupBanReconciliationResult:
    """Atomically replace stored ban flags on one worker-owned connection."""

    request = _validate_request(request)
    models = _load_models()
    with models.db.connection_context():
        with models.db.atomic():
            reset_rows = models.DiscordMember.update(is_banned=False).execute()
            discord_rows = 0
            if request.discord_ids:
                discord_rows = (
                    models.DiscordMember
                    .update(is_banned=True)
                    .where(models.DiscordMember.discord_id.in_(request.discord_ids))
                    .execute()
                )
            polytopia_rows = 0
            if request.polytopia_ids:
                polytopia_rows = (
                    models.DiscordMember
                    .update(is_banned=True)
                    .where(
                        models.DiscordMember.polytopia_id.in_(
                            request.polytopia_ids
                        )
                    )
                    .execute()
                )
    return StartupBanReconciliationResult(
        reset_rows=int(reset_rows),
        discord_rows=int(discord_rows),
        polytopia_rows=int(polytopia_rows),
    )


async def _drain_future(future: Future):
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        try:
            future.result()
        except BaseException:
            pass
        raise cancellation
    return future.result()


async def run_startup_ban_reconciliation(
    request: StartupBanReconciliationRequest,
) -> StartupBanReconciliationResult:
    return await _drain_future(_executor.submit(reconcile_startup_bans, request))
