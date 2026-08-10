"""Shared application service for prefix, slash, and card win claims.

The result mutation remains in :mod:`modules.elo_workers`.  This module owns
the small amount of invocation-independent orchestration around that worker:
primitive request capture, bounded preflight/name resolution, coordinator and
game-record claims, and the established post-commit competitive output.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

import peewee

import settings
from modules import (
    elo_workers,
    exceptions,
    game_result_publication,
    game_win_workers,
    models,
    utilities,
)
from modules.elo_jobs import EloJobConflict


logger = logging.getLogger('polybot.' + __name__.split('.')[-1])


@dataclass(frozen=True)
class WinRequest:
    """Immutable Discord-side values safe to pass into the win boundary."""

    game_id: int
    guild_id: int
    requester_id: int
    requester_name: str
    requester_mention: str
    requester_description: str
    requester_is_staff: bool
    prefix: str
    winner_text: str
    winning_side_id: int | None = None
    invoked_with: str = 'win'


@dataclass(frozen=True)
class WinApplicationOutcome:
    """Describe the committed win and whether public effects fully published."""

    result: elo_workers.WinResult
    public_effects_published: bool


Send = Callable[[str], Awaitable]
Defer = Callable[[], Awaitable]
PostWinPublisher = Callable[[object, str, object, object], Awaitable]


def build_request(
    *,
    game_id: int,
    member,
    guild_id: int,
    prefix: str,
    winner_text: str = '',
    winning_side_id: int | None = None,
    invoked_with: str = 'win',
) -> WinRequest:
    """Capture only primitive requester values on Discord's event loop."""

    requester_id = int(member.id)
    requester_name = str(
        getattr(member, 'display_name', '')
        or getattr(member, 'name', '')
        or requester_id
    )
    mention = getattr(member, 'mention', None)
    if callable(mention):
        mention = mention()
    requester_mention = str(mention or f'<@{requester_id}>')
    return WinRequest(
        game_id=int(game_id),
        guild_id=int(guild_id),
        requester_id=requester_id,
        requester_name=requester_name,
        requester_mention=requester_mention,
        requester_description=models.GameLog.member_string(member),
        requester_is_staff=bool(settings.is_staff(member)),
        prefix=str(prefix),
        winner_text=str(winner_text or ''),
        winning_side_id=(
            int(winning_side_id)
            if winning_side_id is not None
            else None
        ),
        invoked_with=str(invoked_with),
    )


def _preflight_request(
    request: WinRequest,
) -> game_win_workers.WinPreflightRequest:
    return game_win_workers.WinPreflightRequest(
        game_id=request.game_id,
        guild_id=request.guild_id,
        requester_id=request.requester_id,
        requester_is_staff=request.requester_is_staff,
        prefix=request.prefix,
        winning_side_id=request.winning_side_id,
        winner_text=request.winner_text,
    )


def _usage(request: WinRequest) -> str:
    return (
        'Include both game ID and the name of the winning side. Example usage:\n'
        f'`{request.prefix}win 422 Nelluk`\n'
        f'`{request.prefix}win 425 Home` *For a team game*\n'
    )


async def _run_worker(
    request: WinRequest,
    *,
    selection: game_win_workers.WinSideSelection,
    publication_context,
):
    coordinator = settings.elo_job_coordinator
    lock_acquired = False

    def lock_game() -> None:
        nonlocal lock_acquired
        utilities.lock_game(request.game_id)
        lock_acquired = True

    def unlock_game() -> None:
        if lock_acquired:
            utilities.unlock_game(request.game_id)

    return await coordinator.run(
        operation='record_win',
        game_id=request.game_id,
        requester_id=request.requester_id,
        requester_name=request.requester_name,
        worker=elo_workers.record_win,
        worker_args=(
            request.game_id,
            request.guild_id,
            selection.winning_side_id,
            request.requester_id,
            request.requester_description,
            request.requester_is_staff,
            publication_context,
        ),
        before_submit=lock_game,
        after_complete=unlock_game,
    )


async def _send_error(send_error: Send, content: str):
    await send_error(content)
    return None


async def run_win(
    request: WinRequest,
    *,
    guild,
    current_channel,
    send_public: Send,
    send_error: Send,
    post_win_publisher: PostWinPublisher,
    defer: Defer | None = None,
    acknowledged: bool = False,
    typing_context=None,
) -> WinApplicationOutcome | None:
    """Run one result claim through the shared win application boundary.

    ``send_public`` is used only after the mutation worker commits.  Failure
    paths use ``send_error`` and return without loading the committed game or
    invoking any Discord post-commit publisher.  A non-``None`` outcome always
    contains the committed worker result; ``public_effects_published`` is
    false when the commit succeeded but reconciliation is still required.
    """

    if defer is not None and not acknowledged:
        await defer()

    coordinator = settings.elo_job_coordinator
    if bool(getattr(coordinator, 'is_active', False)):
        logger.info('Skipping win request due to active ELO job')
        return await _send_error(
            send_error,
            f':warning: {request.requester_mention} - I am currently '
            'recalculating the results of prior games. No new game results '
            'can be logged. Please try again in a few minutes.',
        )

    if request.invoked_with.lower() == 'lose':
        return await _send_error(
            send_error,
            'Games are always concluded using the '
            f'`{request.prefix}win` command.\n{_usage(request)}',
        )

    async def execute():
        selection = await game_win_workers.run_prepare_win(
            _preflight_request(request),
        )
        publication_context = game_result_publication.capture_publication_context(
            request.guild_id,
            bot=settings.bot,
        )
        return await _run_worker(
            request,
            selection=selection,
            publication_context=publication_context,
        )

    try:
        if typing_context is None:
            result = await execute()
        else:
            async with typing_context():
                result = await execute()
    except EloJobConflict as exc:
        active_job = exc.active_job
        return await _send_error(
            send_error,
            f':warning: {request.requester_mention} - ELO operation '
            f'`{active_job.operation}` for game '
            f'`{active_job.game_id or "all"}` is already running. '
            'Please try again in a few minutes.',
        )
    except elo_workers.WinValidationError as exc:
        return await _send_error(send_error, str(exc))
    except exceptions.CheckFailedError as exc:
        return await _send_error(send_error, f'*Error*: {exc}')
    except exceptions.MyBaseException as exc:
        # Preserve the established prefix side-parser messages. Native
        # callers provide an ephemeral send_error callback.
        return await _send_error(send_error, str(exc))
    except elo_workers.WinSnapshotError as exc:
        result = exc.result
        logger.exception(
            'Committed win %s could not load its publication snapshot',
            request.game_id,
        )
        try:
            await _send_error(
                send_error,
                f'Game {request.game_id} was updated, but its public result '
                'snapshot could not be loaded. An operator must reconcile it.',
            )
        except Exception:
            logger.exception(
                'Could not send committed win %s reconciliation warning',
                request.game_id,
            )
        return WinApplicationOutcome(
            result=result,
            public_effects_published=False,
        )
    except peewee.PeeweeException:
        logger.exception(
            'Database failure while processing win %s',
            request.game_id,
        )
        return await _send_error(
            send_error,
            f'Game {request.game_id} could not be updated because the '
            'database operation failed. No Discord channel updates were made.',
        )
    except Exception:
        logger.exception(
            'Unexpected failure while processing win %s',
            request.game_id,
        )
        return await _send_error(
            send_error,
            f'Game {request.game_id} could not be updated. No Discord channel '
            'updates were made.',
        )

    try:
        if result.publication is None:
            raise RuntimeError('Committed win has no publication snapshot.')
        await game_result_publication.publish_win_result(
            request=request,
            result=result,
            snapshot=result.publication,
            guild=guild,
            current_channel=current_channel,
            send_public=send_public,
            confirmed_publisher=post_win_publisher,
            bot=settings.bot,
        )
        return WinApplicationOutcome(
            result=result,
            public_effects_published=True,
        )
    except Exception:
        # The database worker has already committed.  Do not claim that the
        # mutation rolled back; let the caller report a reconciliation error
        # while retaining the public-success-before-card-refresh ordering.
        logger.exception(
            'Committed win %s could not publish all post-commit effects',
            request.game_id,
        )
        try:
            await _send_error(
                send_error,
                f'Game {request.game_id} was updated, but its public result '
                'could not be fully published. An operator must reconcile it.',
            )
        except Exception:
            logger.exception(
                'Could not send committed win %s reconciliation warning',
                request.game_id,
            )
        return WinApplicationOutcome(
            result=result,
            public_effects_published=False,
        )


__all__ = [
    'WinApplicationOutcome',
    'WinRequest',
    'build_request',
    'run_win',
]
