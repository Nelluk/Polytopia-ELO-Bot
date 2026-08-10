"""Bounded database workers for owner-selected manual channel cleanup."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json

import peewee

import settings
from modules import models


STALE = 'stale'
CAPACITY = 'capacity'
ORPHAN = 'orphan'
MISSING = 'missing'
MODES = frozenset({STALE, CAPACITY, ORPHAN, MISSING})
GAME_TARGET = 'game'
SIDE_TARGET = 'side'
ORPHAN_TARGET = 'orphan'
MAX_PREVIEW_CHANNELS = 500
MAX_PREVIEW_CANDIDATES = 100
MAX_SELECTED_CHANNELS = 25
STALE_DAYS = 30
CAPACITY_THRESHOLD = 425
RECONCILED = 'reconciled'
ALREADY_RECONCILED = 'already_reconciled'
TARGET_CHANGED = 'target_changed'
NO_REFERENCE = 'no_reference'


class ManualChannelPurgeError(RuntimeError):
    """The manual channel-purge request is unsafe or invalid."""


@dataclass(frozen=True)
class ChannelSnapshot:
    channel_id: int
    name: str
    category_id: int | None
    category_name: str | None
    last_message_id: int | None
    last_activity_at: datetime.datetime | None
    manageable: bool
    archive_protected: bool


@dataclass(frozen=True)
class ManualPurgePreviewRequest:
    guild_id: int
    requester_id: int
    mode: str
    as_of: datetime.datetime
    guild_channel_count: int
    configured_category_ids: tuple[int, ...]
    channels: tuple[ChannelSnapshot, ...]


@dataclass(frozen=True)
class ChannelReference:
    kind: str
    record_id: int
    game_id: int
    source_guild_id: int
    channel_id: int
    game_name: str
    is_completed: bool
    is_pending: bool
    league_season: int | None
    recent_nova: bool
    external: bool
    notice_targets: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class ManualPurgeCandidate:
    key: str
    mode: str
    channel_id: int
    channel_name: str
    category_id: int | None
    category_name: str | None
    last_message_id: int | None
    last_activity_at: datetime.datetime | None
    kind: str
    record_id: int | None
    game_id: int | None
    source_guild_id: int | None
    game_name: str | None
    reason: str
    missing: bool
    notice_targets: tuple[tuple[int, int], ...]
    eligibility_token: str


@dataclass(frozen=True)
class ManualPurgePreview:
    guild_id: int
    mode: str
    as_of: datetime.datetime
    guild_channel_count: int
    candidates: tuple[ManualPurgeCandidate, ...]
    exclusions: tuple[str, ...]
    truncated: bool
    fingerprint: str


@dataclass(frozen=True)
class ManualPurgeReconcileRequest:
    guild_id: int
    requester_id: int
    requester_description: str
    candidate: ManualPurgeCandidate


@dataclass(frozen=True)
class ManualPurgeAuthorizationRequest:
    guild_id: int
    requester_id: int
    candidate: ManualPurgeCandidate
    as_of: datetime.datetime


@dataclass(frozen=True)
class ManualPurgeReconcileResult:
    channel_id: int
    status: str


_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-operator-channel-purge-read',
)
_write_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-operator-channel-purge-write',
)


def _assert_owner(requester_id: int) -> None:
    if int(requester_id) != int(settings.owner_id):
        raise ManualChannelPurgeError(
            'Only the configured bot owner can purge game channels.'
        )


def _normalise_preview_request(request: ManualPurgePreviewRequest):
    _assert_owner(request.requester_id)
    if int(request.guild_id) <= 0:
        raise ManualChannelPurgeError('A positive guild ID is required.')
    if request.mode not in MODES:
        raise ManualChannelPurgeError('The channel-purge mode is invalid.')
    if not isinstance(request.as_of, datetime.datetime):
        raise ManualChannelPurgeError('A datetime preview boundary is required.')
    if len(request.channels) > MAX_PREVIEW_CHANNELS:
        raise ManualChannelPurgeError(
            f'Channel inventory exceeds the {MAX_PREVIEW_CHANNELS}-channel bound.'
        )
    channel_ids = tuple(int(row.channel_id) for row in request.channels)
    if any(value <= 0 for value in channel_ids) or len(set(channel_ids)) != len(channel_ids):
        raise ManualChannelPurgeError('Channel inventory IDs must be unique and positive.')
    return request


def _recent_nova(notes, completed_ts, as_of):
    text = str(notes or '').upper()
    boundary = as_of - datetime.timedelta(days=4)
    if completed_ts and completed_ts.tzinfo is None:
        boundary = boundary.replace(tzinfo=None)
    elif completed_ts and boundary.tzinfo is None:
        completed_ts = completed_ts.replace(tzinfo=datetime.UTC)
    return bool(
        completed_ts
        and 'NOVA RED' in text
        and 'NOVA BLUE' in text
        and completed_ts > boundary
    )


def load_channel_references(
    guild_id: int,
    *,
    as_of: datetime.datetime,
) -> tuple[ChannelReference, ...]:
    """Load every local or externally-targeted channel reference."""

    with models.db.connection_context():
        game_rows = tuple(
            models.Game
            .select(
                models.Game.id,
                models.Game.guild_id,
                models.Game.name,
                models.Game.is_completed,
                models.Game.is_pending,
                models.Game.league_season,
                models.Game.notes,
                models.Game.completed_ts,
                models.Game.game_chan,
            )
            .where(
                (models.Game.guild_id == guild_id)
                & models.Game.game_chan.is_null(False)
            )
            .dicts()
        )
        side_rows = tuple(
            models.GameSide
            .select(
                models.GameSide.id,
                models.GameSide.game,
                models.GameSide.team_chan,
                models.GameSide.team_chan_external_server,
                models.Game.guild_id,
                models.Game.name,
                models.Game.is_completed,
                models.Game.is_pending,
                models.Game.league_season,
                models.Game.notes,
                models.Game.completed_ts,
            )
            .join(models.Game)
            .where(
                models.GameSide.team_chan.is_null(False)
                & (
                    (
                        (models.Game.guild_id == guild_id)
                        & models.GameSide.team_chan_external_server.is_null(True)
                    )
                    | (models.GameSide.team_chan_external_server == guild_id)
                )
            )
            .dicts()
        )

    notice_by_game: dict[int, list[tuple[int, int]]] = {}
    for row in side_rows:
        game_id = int(row['game'])
        target_guild = int(
            row['team_chan_external_server'] or row['guild_id']
        )
        notice_by_game.setdefault(game_id, []).append(
            (target_guild, int(row['team_chan']))
        )

    references = []
    for row in game_rows:
        game_id = int(row['id'])
        references.append(ChannelReference(
            kind=GAME_TARGET,
            record_id=game_id,
            game_id=game_id,
            source_guild_id=int(row['guild_id']),
            channel_id=int(row['game_chan']),
            game_name=str(row['name'] or f'Game {game_id}'),
            is_completed=bool(row['is_completed']),
            is_pending=bool(row['is_pending']),
            league_season=row['league_season'],
            recent_nova=_recent_nova(
                row['notes'], row['completed_ts'], as_of
            ),
            external=False,
            notice_targets=tuple(notice_by_game.get(game_id, ())),
        ))
    for row in side_rows:
        game_id = int(row['game'])
        source_guild = int(row['guild_id'])
        external = bool(row['team_chan_external_server'])
        references.append(ChannelReference(
            kind=SIDE_TARGET,
            record_id=int(row['id']),
            game_id=game_id,
            source_guild_id=source_guild,
            channel_id=int(row['team_chan']),
            game_name=str(row['name'] or f'Game {game_id}'),
            is_completed=bool(row['is_completed']),
            is_pending=bool(row['is_pending']),
            league_season=row['league_season'],
            recent_nova=_recent_nova(
                row['notes'], row['completed_ts'], as_of
            ),
            external=external or source_guild != int(guild_id),
            notice_targets=tuple(notice_by_game.get(game_id, ())),
        ))
    return tuple(references)


def _token(values) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _candidate(
    *,
    request,
    channel,
    reference=None,
    reason,
    missing=False,
):
    kind = reference.kind if reference else ORPHAN_TARGET
    record_id = reference.record_id if reference else None
    game_id = reference.game_id if reference else None
    source_guild_id = reference.source_guild_id if reference else None
    identity = f'{kind}:{record_id or 0}:{channel.channel_id}'
    values = {
        'mode': request.mode,
        'channel_id': channel.channel_id,
        'category_id': channel.category_id,
        'last_message_id': channel.last_message_id,
        'kind': kind,
        'record_id': record_id,
        'game_id': game_id,
        'source_guild_id': source_guild_id,
        'missing': missing,
        'guild_channel_count': request.guild_channel_count,
    }
    return ManualPurgeCandidate(
        key=identity,
        mode=request.mode,
        channel_id=int(channel.channel_id),
        channel_name=str(channel.name),
        category_id=channel.category_id,
        category_name=channel.category_name,
        last_message_id=channel.last_message_id,
        last_activity_at=channel.last_activity_at,
        kind=kind,
        record_id=record_id,
        game_id=game_id,
        source_guild_id=source_guild_id,
        game_name=reference.game_name if reference else None,
        reason=reason,
        missing=bool(missing),
        notice_targets=(
            reference.notice_targets
            if reference and request.mode == CAPACITY else ()
        ),
        eligibility_token=_token(values),
    )


def build_manual_purge_preview(
    request: ManualPurgePreviewRequest,
    references: tuple[ChannelReference, ...],
) -> ManualPurgePreview:
    """Classify a frozen Discord inventory against frozen database refs."""

    request = _normalise_preview_request(request)
    channels = {row.channel_id: row for row in request.channels}
    refs_by_channel: dict[int, list[ChannelReference]] = {}
    for reference in references:
        refs_by_channel.setdefault(int(reference.channel_id), []).append(reference)
    exclusions = []
    candidates = []
    cutoff = request.as_of - datetime.timedelta(days=STALE_DAYS)

    def protected(channel, reference=None):
        if channel.archive_protected:
            return 'archive-protected channel'
        if not channel.manageable:
            return 'bot lacks Manage Channels'
        if reference is not None:
            if reference.external:
                return 'external/cross-guild reference'
            if reference.is_completed:
                return 'completed-game cleanup is automatic'
            if reference.league_season:
                return 'season game is protected'
            if reference.recent_nova:
                return 'recent Nova game is protected'
        return None

    if request.mode == ORPHAN:
        configured = set(request.configured_category_ids)
        for channel in request.channels:
            refs = refs_by_channel.get(channel.channel_id, ())
            if refs:
                continue
            if channel.category_id not in configured:
                continue
            reason = protected(channel)
            if reason:
                exclusions.append(f'`{channel.channel_id}`: {reason}')
                continue
            candidates.append(_candidate(
                request=request,
                channel=channel,
                reason='configured game-category channel with no database reference',
            ))
    else:
        for channel_id, refs in refs_by_channel.items():
            if len(refs) != 1:
                exclusions.append(
                    f'`{channel_id}`: {len(refs)} database references are ambiguous'
                )
                continue
            reference = refs[0]
            channel = channels.get(channel_id)
            if channel is None:
                if request.mode == MISSING and not reference.external:
                    missing_snapshot = ChannelSnapshot(
                        channel_id=channel_id,
                        name=f'missing-{channel_id}',
                        category_id=None,
                        category_name=None,
                        last_message_id=None,
                        last_activity_at=None,
                        manageable=True,
                        archive_protected=False,
                    )
                    reason = protected(missing_snapshot, reference)
                    if reason:
                        exclusions.append(f'`{channel_id}`: {reason}')
                    else:
                        candidates.append(_candidate(
                            request=request,
                            channel=missing_snapshot,
                            reference=reference,
                            reason=(
                                'database reference points to an absent '
                                'Discord channel'
                            ),
                            missing=True,
                        ))
                continue
            reason = protected(channel, reference)
            if reason:
                exclusions.append(f'`{channel_id}`: {reason}')
                continue
            if request.mode == CAPACITY:
                if request.guild_channel_count <= CAPACITY_THRESHOLD:
                    continue
                if reference.kind != GAME_TARGET:
                    continue
                candidates.append(_candidate(
                    request=request,
                    channel=channel,
                    reference=reference,
                    reason=(
                        f'central channel while guild has '
                        f'{request.guild_channel_count} channels'
                    ),
                ))
            elif request.mode == STALE and (
                channel.last_activity_at is None
                or channel.last_activity_at <= cutoff
            ):
                candidates.append(_candidate(
                    request=request,
                    channel=channel,
                    reference=reference,
                    reason=(
                        'tracked channel has no messages'
                        if channel.last_activity_at is None else
                        f'last activity is at least {STALE_DAYS} days old'
                    ),
                ))

    candidates.sort(key=lambda row: (row.channel_id, row.kind, row.record_id or 0))
    truncated = len(candidates) > MAX_PREVIEW_CANDIDATES
    candidates = candidates[:MAX_PREVIEW_CANDIDATES]
    fingerprint = _token([
        request.guild_id,
        request.mode,
        request.guild_channel_count,
        [(row.key, row.eligibility_token) for row in candidates],
    ])
    return ManualPurgePreview(
        guild_id=int(request.guild_id),
        mode=request.mode,
        as_of=request.as_of,
        guild_channel_count=int(request.guild_channel_count),
        candidates=tuple(candidates),
        exclusions=tuple(exclusions[:MAX_PREVIEW_CANDIDATES]),
        truncated=truncated,
        fingerprint=fingerprint,
    )


def load_manual_purge_preview(request):
    request = _normalise_preview_request(request)
    references = load_channel_references(
        int(request.guild_id),
        as_of=request.as_of,
    )
    return build_manual_purge_preview(request, references)


def authorize_manual_purge_candidate(
    request: ManualPurgeAuthorizationRequest,
) -> bool:
    """Recheck one selected database target immediately before deletion."""

    _assert_owner(request.requester_id)
    candidate = request.candidate
    if int(request.guild_id) <= 0 or candidate.channel_id <= 0:
        raise ManualChannelPurgeError('Positive authorization IDs are required.')
    references = tuple(
        row for row in load_channel_references(
            int(request.guild_id), as_of=request.as_of,
        )
        if int(row.channel_id) == int(candidate.channel_id)
    )
    if candidate.kind == ORPHAN_TARGET:
        return not references
    if len(references) != 1:
        return False
    reference = references[0]
    return bool(
        reference.kind == candidate.kind
        and int(reference.record_id) == int(candidate.record_id or 0)
        and int(reference.game_id) == int(candidate.game_id or 0)
        and int(reference.source_guild_id)
        == int(candidate.source_guild_id or 0)
        and not reference.external
        and not reference.is_completed
        and not reference.league_season
        and not reference.recent_nova
    )


def reconcile_manual_purge(request: ManualPurgeReconcileRequest):
    _assert_owner(request.requester_id)
    candidate = request.candidate
    if int(request.guild_id) <= 0 or candidate.channel_id <= 0:
        raise ManualChannelPurgeError('Positive reconciliation IDs are required.')
    if candidate.kind == ORPHAN_TARGET:
        return ManualPurgeReconcileResult(candidate.channel_id, NO_REFERENCE)
    if candidate.kind not in {GAME_TARGET, SIDE_TARGET}:
        raise ManualChannelPurgeError('The reconciliation target is invalid.')
    if not candidate.record_id or not candidate.game_id:
        raise ManualChannelPurgeError('The reconciliation target is incomplete.')

    with models.db.connection_context():
        with models.db.atomic():
            if candidate.kind == GAME_TARGET:
                row = (
                    models.Game.select()
                    .where(
                        (models.Game.id == candidate.record_id)
                        & (models.Game.guild_id == request.guild_id)
                    )
                    .for_update()
                    .first()
                )
                field = models.Game.game_chan
                current = None if row is None else row.game_chan
            else:
                row = (
                    models.GameSide
                    .select(models.GameSide, models.Game)
                    .join(models.Game)
                    .where(
                        (models.GameSide.id == candidate.record_id)
                        & (models.GameSide.game == candidate.game_id)
                        & (models.Game.guild_id == request.guild_id)
                        & models.GameSide.team_chan_external_server.is_null(True)
                    )
                    .for_update()
                    .first()
                )
                field = models.GameSide.team_chan
                current = None if row is None else row.team_chan

            if row is None or current is None:
                status = ALREADY_RECONCILED
            elif int(current) != int(candidate.channel_id):
                status = TARGET_CHANGED
            else:
                setattr(row, field.name, None)
                row.save(only=(field,))
                models.GameLog.write(
                    game_id=int(candidate.game_id),
                    guild_id=int(request.guild_id),
                    is_protected=True,
                    message=(
                        f'Owner manual channel purge cleared {candidate.kind} '
                        f'channel `{candidate.channel_id}` after Discord '
                        f'deletion; actor {request.requester_description}.'
                    ),
                )
                status = RECONCILED
    return ManualPurgeReconcileResult(candidate.channel_id, status)


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


async def run_load_manual_purge_preview(request):
    return await _drain_future(_read_executor.submit(load_manual_purge_preview, request))


async def run_authorize_manual_purge_candidate(request):
    return await _drain_future(
        _read_executor.submit(authorize_manual_purge_candidate, request)
    )


async def run_reconcile_manual_purge(request):
    return await _drain_future(_write_executor.submit(reconcile_manual_purge, request))
