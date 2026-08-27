"""Model-free orchestration for supervised bot restarts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re


RESTART_EXIT_STATUS = 75
FORCE_CONFIRMATION = 'RESTART NOW'
COMPOSE_SUPERVISOR = 'compose'
_CHECKPOINT = re.compile(r'^[0-9a-f]{40}$')


class RestartError(RuntimeError):
    """Base class for a restart that was refused before process exit."""


class RestartPermissionError(RestartError):
    """The requester is not authorized for the selected restart mode."""


class RestartSupervisionError(RestartError):
    """The bot is not running beneath the required process supervisor."""


class RestartCheckoutError(RestartError):
    """The checkout cannot safely be loaded by the supervisor."""


class RestartBusyError(RestartError):
    """Known in-process work makes a normal restart unsafe."""


class RestartConflictError(RestartError):
    """Another accepted restart already owns process shutdown."""


class RestartConfirmationError(RestartError):
    """The force confirmation text did not match exactly."""


@dataclass(frozen=True, slots=True)
class RestartActivitySnapshot:
    descriptions: tuple[str, ...] = ()

    @property
    def busy(self) -> bool:
        return bool(self.descriptions)


@dataclass(frozen=True, slots=True)
class RestartCheckoutSnapshot:
    running_source: str
    restart_source: str
    supervisor: str = 'systemd'


@dataclass(frozen=True, slots=True)
class RestartRequest:
    requester_id: int
    requester_name: str
    is_superuser: bool
    is_owner: bool
    force: bool = False
    confirmation_text: str | None = None


@dataclass(frozen=True, slots=True)
class RestartPreview:
    requester_id: int
    requester_name: str
    force: bool
    checkout: RestartCheckoutSnapshot
    activity: RestartActivitySnapshot


@dataclass(frozen=True, slots=True)
class ActiveRestart:
    requester_id: int
    force: bool


def assert_authorized(request: RestartRequest) -> None:
    if not request.is_superuser:
        raise RestartPermissionError(
            'Only a configured bot superuser can restart the bot.'
        )
    if request.force and not request.is_owner:
        raise RestartPermissionError(
            'Only the configured bot owner can force a restart.'
        )
    if request.force and request.confirmation_text is not None:
        if request.confirmation_text != FORCE_CONFIRMATION:
            raise RestartConfirmationError(
                f'Type `{FORCE_CONFIRMATION}` exactly to force the restart.'
            )


def _supervisor_kind(environ: Mapping[str, str]) -> str:
    configured = str(environ.get('POLYBOT_RESTART_SUPERVISOR', ''))
    if configured:
        if configured == COMPOSE_SUPERVISOR:
            return COMPOSE_SUPERVISOR
        raise RestartSupervisionError(
            'The configured restart supervisor is invalid, so restart was '
            'refused without stopping the bot.'
        )
    invocation_id = str(environ.get('INVOCATION_ID', ''))
    if re.fullmatch(r'[0-9a-f]{32}', invocation_id):
        return 'systemd'
    raise RestartSupervisionError(
        'This bot process is not running under a reviewed systemd or '
        'Compose supervisor, so restart was refused without '
        'stopping it.'
    )


def assert_supervised(environ: Mapping[str, str] | None = None) -> None:
    environment = os.environ if environ is None else environ
    _supervisor_kind(environment)


async def _git_output(project_root: Path, *arguments: str) -> str:
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            'git',
            *arguments,
            cwd=str(project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), 2.0)
    except asyncio.CancelledError:
        if process is not None:
            await _kill_and_reap(process)
        raise
    except (OSError, asyncio.TimeoutError) as exc:
        if process is not None:
            await _kill_and_reap(process)
        raise RestartCheckoutError(
            'Could not inspect the bot checkout; restart was refused.'
        ) from exc
    if process.returncode != 0:
        raise RestartCheckoutError(
            'Could not inspect the bot checkout; restart was refused.'
        )
    return stdout.decode('utf-8', errors='replace').strip()


async def _kill_and_reap(process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    wait_task = asyncio.create_task(process.wait())
    current = asyncio.current_task()
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            if current is not None:
                current.uncancel()
    wait_task.result()


async def inspect_checkout(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> RestartCheckoutSnapshot:
    environment = os.environ if environ is None else environ
    supervisor = (
        _supervisor_kind(environment)
        if environment.get('POLYBOT_RESTART_SUPERVISOR')
        else 'systemd'
    )
    if supervisor == COMPOSE_SUPERVISOR:
        return RestartCheckoutSnapshot(
            running_source='current container image',
            restart_source='current container image',
            supervisor=supervisor,
        )

    project_root = Path(project_root).resolve()
    status = await _git_output(
        project_root,
        'status',
        '--porcelain',
        '--untracked-files=all',
    )
    if status:
        raise RestartCheckoutError(
            'The bot checkout has uncommitted or untracked changes. Commit '
            'or remove them before restarting.'
        )
    desired = await _git_output(
        project_root,
        'rev-parse',
        '--verify',
        'HEAD',
    )
    if not _CHECKPOINT.fullmatch(desired):
        raise RestartCheckoutError(
            'The bot checkout revision is invalid; restart was refused.'
        )
    return RestartCheckoutSnapshot(
        running_source='current process',
        restart_source=desired,
        supervisor=supervisor,
    )


def _busy_message(activity: RestartActivitySnapshot) -> str:
    joined = '; '.join(activity.descriptions)
    return (
        f'Restart refused because work is still active: {joined}. '
        'Wait for it to finish. The configured bot owner may instead rerun '
        'with `force:true`.'
    )


async def drain_cancellation(task: asyncio.Task[None]) -> None:
    cancellation: asyncio.CancelledError | None = None
    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            if current is not None:
                current.uncancel()
    task.result()
    if cancellation is not None:
        raise cancellation


class RestartCoordinator:
    """Serialize final revalidation and supervised process shutdown."""

    def __init__(self) -> None:
        self.active: ActiveRestart | None = None

    async def preview(
        self,
        request: RestartRequest,
        *,
        project_root: Path,
        activity_loader: Callable[[], RestartActivitySnapshot],
        environ: Mapping[str, str] | None = None,
    ) -> RestartPreview:
        assert_authorized(request)
        assert_supervised(environ)
        checkout = await inspect_checkout(project_root, environ=environ)
        activity = activity_loader()
        if activity.busy and not request.force:
            raise RestartBusyError(_busy_message(activity))
        return RestartPreview(
            requester_id=request.requester_id,
            requester_name=request.requester_name,
            force=request.force,
            checkout=checkout,
            activity=activity,
        )

    async def run(
        self,
        request: RestartRequest,
        *,
        project_root: Path,
        activity_loader: Callable[[], RestartActivitySnapshot],
        shutdown: Callable[[int, bool], Awaitable[None]],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        assert_authorized(request)
        if request.force and request.confirmation_text != FORCE_CONFIRMATION:
            raise RestartConfirmationError(
                f'Type `{FORCE_CONFIRMATION}` exactly to force the restart.'
            )
        if self.active is not None:
            raise RestartConflictError(
                'Another accepted restart is already shutting down the bot.'
            )
        self.active = ActiveRestart(request.requester_id, request.force)
        try:
            assert_supervised(environ)
            await inspect_checkout(project_root, environ=environ)
            activity = activity_loader()
            if activity.busy and not request.force:
                raise RestartBusyError(_busy_message(activity))
            shutdown_task = asyncio.create_task(
                shutdown(request.requester_id, request.force)
            )
            await drain_cancellation(shutdown_task)
        finally:
            self.active = None


restart_coordinator = RestartCoordinator()
