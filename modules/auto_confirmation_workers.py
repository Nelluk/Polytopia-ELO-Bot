"""Bounded worker-owned discovery for automatic game confirmations."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging

import peewee

from modules import models


MAX_AUTO_CONFIRMATION_CANDIDATES = 100
RANKED_CONFIRMATION_DELAY = datetime.timedelta(hours=24)
UNRANKED_CONFIRMATION_DELAY = datetime.timedelta(hours=6)
logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class AutoConfirmationPolicy:
    """Frozen timing input for transactional eligibility revalidation."""

    as_of: datetime.datetime


@dataclass(frozen=True)
class AutoConfirmationEvidence:
    """Primitive facts that explain one authoritative eligibility decision."""

    reason: str
    confirmed_count: int
    side_count: int


@dataclass(frozen=True)
class AutoConfirmationCandidate:
    game_id: int
    discovered_evidence: AutoConfirmationEvidence


@dataclass(frozen=True)
class AutoConfirmationDiscoveryRequest:
    guild_id: int
    policy: AutoConfirmationPolicy
    limit: int = MAX_AUTO_CONFIRMATION_CANDIDATES


@dataclass(frozen=True)
class AutoConfirmationBatch:
    guild_id: int
    policy: AutoConfirmationPolicy
    unconfirmed_count: int
    candidates: tuple[AutoConfirmationCandidate, ...]
    truncated: bool


_auto_confirmation_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-auto-confirmation-read',
)


def eligibility_evidence(
    *,
    is_ranked: bool,
    win_claimed_ts: datetime.datetime | None,
    confirmed_count: int,
    side_count: int,
    policy: AutoConfirmationPolicy,
) -> AutoConfirmationEvidence | None:
    """Apply the retained automatic-confirmation policy to primitive facts."""

    if win_claimed_ts is None:
        return None
    if (
        is_ranked
        and win_claimed_ts < policy.as_of - RANKED_CONFIRMATION_DELAY
    ):
        reason = 'Ranked win claimed more than 24 hours ago.'
    elif (
        not is_ranked
        and win_claimed_ts < policy.as_of - UNRANKED_CONFIRMATION_DELAY
    ):
        reason = 'Unranked win claimed more than 6 hours ago.'
    elif side_count < 5 and confirmed_count > 1:
        reason = 'Due to partial confirmations.'
    elif side_count >= 5 and confirmed_count > 2:
        reason = 'Due to partial confirmations.'
    else:
        return None
    return AutoConfirmationEvidence(
        reason=reason,
        confirmed_count=int(confirmed_count),
        side_count=int(side_count),
    )


def game_eligibility_evidence(
    game,
    policy: AutoConfirmationPolicy,
) -> AutoConfirmationEvidence | None:
    confirmed_count, side_count, _ = game.confirmations_count()
    return eligibility_evidence(
        is_ranked=bool(game.is_ranked),
        win_claimed_ts=game.win_claimed_ts,
        confirmed_count=confirmed_count,
        side_count=side_count,
        policy=policy,
    )


def _eligible_unconfirmed_query(unconfirmed_query, request):
    """Apply the complete retained policy before the per-cycle bound."""

    confirmed_sides = peewee.fn.SUM(peewee.Case(
        None,
        ((models.GameSide.win_confirmed == True, 1),),
        0,
    ))
    side_count = peewee.fn.COUNT(models.GameSide.id)
    partial_confirmations = (
        models.GameSide
        .select(models.GameSide.game)
        .group_by(models.GameSide.game)
        .having(
            ((side_count < 5) & (confirmed_sides > 1))
            | ((side_count >= 5) & (confirmed_sides > 2))
        )
    )
    ranked_cutoff = request.policy.as_of - RANKED_CONFIRMATION_DELAY
    unranked_cutoff = request.policy.as_of - UNRANKED_CONFIRMATION_DELAY
    return unconfirmed_query.where(
        models.Game.win_claimed_ts.is_null(False)
        & (
            (
                (models.Game.is_ranked == True)
                & (models.Game.win_claimed_ts < ranked_cutoff)
            )
            | (
                (models.Game.is_ranked == False)
                & (models.Game.win_claimed_ts < unranked_cutoff)
            )
            | models.Game.id.in_(partial_confirmations)
        )
    )


def discover_auto_confirmations(
    request: AutoConfirmationDiscoveryRequest,
) -> AutoConfirmationBatch:
    """Freeze a bounded deterministic candidate batch on a local connection."""

    limit = int(request.limit)
    if limit < 1 or limit > MAX_AUTO_CONFIRMATION_CANDIDATES:
        raise ValueError(
            'Automatic-confirmation discovery limit must be between 1 and '
            f'{MAX_AUTO_CONFIRMATION_CANDIDATES}.'
        )
    with models.db.connection_context():
        unconfirmed_query = models.Game.search(
            status_filter=5,
            guild_id=request.guild_id,
        )
        unconfirmed_count = unconfirmed_query.count()
        rows = tuple(
            _eligible_unconfirmed_query(unconfirmed_query, request)
            .order_by(models.Game.win_claimed_ts, models.Game.id)
            .limit(limit + 1)
        )
        candidates = []
        for game in rows[:limit]:
            evidence = game_eligibility_evidence(game, request.policy)
            if evidence is not None:
                candidates.append(AutoConfirmationCandidate(
                    game_id=int(game.id),
                    discovered_evidence=evidence,
                ))
        return AutoConfirmationBatch(
            guild_id=int(request.guild_id),
            policy=request.policy,
            unconfirmed_count=int(unconfirmed_count),
            candidates=tuple(candidates),
            truncated=len(rows) > limit,
        )


async def run_discover_auto_confirmations(
    request: AutoConfirmationDiscoveryRequest,
) -> AutoConfirmationBatch:
    """Run discovery off-loop and retain ownership through cancellation."""

    future = _auto_confirmation_executor.submit(
        discover_auto_confirmations,
        request,
    )
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
            logger.exception(
                'Cancelled automatic-confirmation discovery completed with '
                'an error'
            )
        raise cancellation
    return future.result()
