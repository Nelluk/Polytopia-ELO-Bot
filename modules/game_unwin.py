"""Shared application boundary for prefix and slash unwin requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

import peewee

import settings
from modules import elo_workers, game_result_publication, models, utilities
from modules.elo_jobs import EloJobConflict


logger = logging.getLogger('polybot.' + __name__.split('.')[-1])


@dataclass(frozen=True)
class UnwinRequest:
    game_id: int
    guild_id: int
    requester_id: int
    requester_name: str
    requester_mention: str
    requester_description: str
    requester_is_staff: bool
    prefix: str


@dataclass(frozen=True)
class UnwinApplicationOutcome:
    result: elo_workers.UnwinResult
    public_effects_published: bool


Send = Callable[[str], Awaitable]
PostUnwinPublisher = Callable[..., Awaitable]


def build_request(*, game_id: int, member, guild_id: int, prefix: str) -> UnwinRequest:
    requester_id = int(member.id)
    requester_name = str(
        getattr(member, 'display_name', '')
        or getattr(member, 'name', '')
        or requester_id
    )
    mention = getattr(member, 'mention', None)
    if callable(mention):
        mention = mention()
    return UnwinRequest(
        game_id=int(game_id),
        guild_id=int(guild_id),
        requester_id=requester_id,
        requester_name=requester_name,
        requester_mention=str(mention or f'<@{requester_id}>'),
        requester_description=models.GameLog.member_string(member),
        requester_is_staff=bool(settings.is_staff(member)),
        prefix=str(prefix),
    )


async def run_unwin(
    request: UnwinRequest,
    *,
    guild,
    current_channel,
    send: Send,
    post_unwin_publisher: PostUnwinPublisher,
    typing_context=None,
) -> UnwinApplicationOutcome | None:
    coordinator = settings.elo_job_coordinator
    active_job = coordinator.active_job
    if active_job is not None:
        logger.info('Skipping unwin due to active ELO job: %s', active_job)
        await send(
            f':warning: {request.requester_mention} - ELO operation '
            f'`{active_job.operation}` for game '
            f'`{active_job.game_id or "all"}` is already running. '
            'Please try again in a few minutes.'
        )
        return None

    lock_acquired = False

    def lock_game() -> None:
        nonlocal lock_acquired
        utilities.lock_game(request.game_id)
        lock_acquired = True

    def unlock_game() -> None:
        if lock_acquired:
            utilities.unlock_game(request.game_id)

    publication_context = game_result_publication.capture_publication_context(
        request.guild_id,
        bot=settings.bot,
    )

    async def execute():
        return await coordinator.run(
            operation='unwin',
            game_id=request.game_id,
            requester_id=request.requester_id,
            requester_name=request.requester_name,
            worker=elo_workers.unwin_game,
            worker_args=(
                request.game_id,
                request.guild_id,
                request.requester_id,
                request.requester_description,
                request.requester_is_staff,
                publication_context,
            ),
            before_submit=lock_game,
            after_complete=unlock_game,
        )

    try:
        if typing_context is None:
            result = await execute()
        else:
            async with typing_context():
                result = await execute()
    except EloJobConflict as exc:
        active_job = exc.active_job
        await send(
            f':warning: {request.requester_mention} - ELO operation '
            f'`{active_job.operation}` for game '
            f'`{active_job.game_id or "all"}` is already running. '
            'Please try again in a few minutes.'
        )
        return None
    except elo_workers.UnwinValidationError as exc:
        await send(str(exc))
        return None
    except elo_workers.UnwinSnapshotError as exc:
        result = exc.result
        logger.exception(
            'Committed unwin %s could not load its publication snapshot',
            request.game_id,
        )
        await send(
            f'Game {request.game_id} was reset, but its public result snapshot '
            'could not be loaded. An operator must reconcile its channels and '
            'roles; do not run unwin again.'
        )
        return UnwinApplicationOutcome(result, False)
    except peewee.PeeweeException:
        logger.exception('Database failure while processing unwin %s', request.game_id)
        await send(
            f'Game {request.game_id} could not be reset because the database '
            'operation failed. No Discord channel updates were made.'
        )
        return None
    except Exception:
        logger.exception('Unexpected failure while processing unwin %s', request.game_id)
        await send(
            f'Game {request.game_id} could not be reset. No Discord channel '
            'updates were made.'
        )
        return None

    try:
        if result.post_unwin_messaging:
            if result.publication is None:
                raise RuntimeError('Committed unwin has no publication snapshot.')
            await post_unwin_publisher(
                guild,
                request.prefix,
                current_channel,
                result.publication,
                previously_confirmed=result.previously_confirmed,
            )
        await send(result.message)
    except Exception:
        logger.exception(
            'Committed unwin %s could not publish all post-commit effects',
            request.game_id,
        )
        try:
            await send(
                f'Game {request.game_id} was reset, but its public result '
                'could not be fully published. An operator must reconcile its '
                'channels and roles; do not run unwin again.'
            )
        except Exception:
            logger.exception(
                'Could not send committed unwin %s reconciliation warning',
                request.game_id,
            )
        return UnwinApplicationOutcome(result, False)
    return UnwinApplicationOutcome(result, True)


__all__ = [
    'UnwinApplicationOutcome',
    'UnwinRequest',
    'build_request',
    'run_unwin',
]
