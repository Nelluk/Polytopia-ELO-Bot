"""Worker-owned planning and persistence for external game invitations."""

from __future__ import annotations

from dataclasses import dataclass

import settings
from modules import game_open_workers, models


MAX_EXTERNAL_BROADCAST_DESTINATIONS = 16
MAX_SCOPE_LABEL_CHARACTERS = 500

READY = 'ready'
GONE = 'gone'
STALE = 'stale'
DUPLICATE = 'duplicate'
TRACKED = 'tracked'


@dataclass(frozen=True)
class BroadcastRoleSnapshot:
    role_id: int
    role_name: str


@dataclass(frozen=True)
class BroadcastPlanRequest:
    game_id: int
    guild_id: int
    jump_url: str
    role_locks: tuple[BroadcastRoleSnapshot, ...]


@dataclass(frozen=True)
class BroadcastDestinationPlan:
    external_server_id: int
    scopes: tuple[str, ...]
    content_with_join: str
    content_without_join: str


@dataclass(frozen=True)
class BroadcastPlanResult:
    game_id: int
    guild_id: int
    status: str
    destinations: tuple[BroadcastDestinationPlan, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class BroadcastTargetRequest:
    game_id: int
    guild_id: int
    channel_id: int
    message_id: int | None = None


@dataclass(frozen=True)
class BroadcastTargetResult:
    status: str
    game_id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    row_id: int | None = None


def _load_game(request):
    game = models.Game.get_or_none(id=request.game_id)
    if game is None:
        return None, GONE
    if int(game.guild_id) != int(request.guild_id) or not game.is_pending:
        return None, STALE
    return game, READY


def _house_external_server_ids(*, house, guild_id: int) -> tuple[int, ...]:
    rows = (
        models.Team.select(models.Team.external_server)
        .where(
            (models.Team.house == house.id)
            & (models.Team.guild_id == guild_id)
            & (models.Team.is_hidden == False)
            & (models.Team.is_archived == False)
            & models.Team.external_server.is_null(False)
            & (models.Team.external_server > 0)
        )
    )
    return tuple(sorted({int(row.external_server) for row in rows}))


def _resolve_role_scope(*, guild_id: int, role: BroadcastRoleSnapshot):
    team = models.Team.get_or_none(
        (models.Team.guild_id == guild_id)
        & (models.Team.name == role.role_name)
        & (models.Team.is_hidden == False)
        & (models.Team.is_archived == False)
    )
    house = models.House.get_or_none(models.House.name == role.role_name)
    if team is not None and house is not None:
        return None, (
            f'Role {role.role_name!r} matches both a Team and a House; its '
            'external broadcast was skipped.'
        )
    if team is not None:
        if not team.external_server:
            return None, (
                f'Team {role.role_name!r} has no external server configured; '
                'its external broadcast was skipped.'
            )
        label = f'Team {role.role_name.replace("The ", "")}'
        return (int(team.external_server), label), None
    if house is not None:
        server_ids = _house_external_server_ids(
            house=house,
            guild_id=guild_id,
        )
        if len(server_ids) != 1:
            detail = 'none' if not server_ids else ', '.join(map(str, server_ids))
            return None, (
                f'House {role.role_name!r} resolves to {len(server_ids)} '
                f'distinct active-Team external servers ({detail}); its '
                'external broadcast was skipped.'
            )
        return (server_ids[0], f'House {role.role_name}'), None
    return None, (
        f'Role {role.role_name!r} does not exactly match an active Team or '
        'House; its external broadcast was skipped.'
    )


def _scope_text(scopes: tuple[str, ...]) -> str:
    accepted = []
    for index, scope in enumerate(scopes):
        candidate = ' / '.join((*accepted, scope))
        remaining = len(scopes) - index - 1
        suffix = f' / +{remaining} more' if remaining else ''
        if len(candidate + suffix) > MAX_SCOPE_LABEL_CHARACTERS:
            return ' / '.join(accepted) + f' / +{len(scopes) - len(accepted)} more'
        accepted.append(scope)
    return ' / '.join(accepted)


def _content(*, game, scopes: tuple[str, ...], jump_url: str, join: bool) -> str:
    notes = f'\nNotes: *{game.notes}*' if game.notes else ''
    scope_text = _scope_text(scopes)
    content = (
        f'New PolyChampions game `{game.id}` for {scope_text} created by '
        f'{game.host.name}\n{game.size_string()} {game.get_headline()}'
        f'{notes}\n{jump_url}'
    )
    if game.is_uncaught_season_game():
        return content + (
            '\n(*This appears to be a **Season Game** so join reactions are '
            'disabled.*)'
        )
    if join:
        return content + f'\n{game.reaction_join_string()}.'
    return content + '\n:warning: *Missing add reactions permission*.'


def build_broadcast_plan(request: BroadcastPlanRequest) -> BroadcastPlanResult:
    """Resolve one immutable, deterministic external-destination plan."""

    if request.guild_id not in (
        settings.server_ids['polychampions'],
        settings.server_ids['test'],
    ):
        return BroadcastPlanResult(
            game_id=request.game_id,
            guild_id=request.guild_id,
            status=READY,
            destinations=(),
            warnings=(),
        )
    with models.db.connection_context():
        game, status = _load_game(request)
        if game is None:
            return BroadcastPlanResult(
                game_id=request.game_id,
                guild_id=request.guild_id,
                status=status,
                destinations=(),
                warnings=(),
            )

        grouped: dict[int, list[str]] = {}
        warnings: list[str] = []
        seen_roles: set[tuple[int, str]] = set()
        for role in request.role_locks:
            role_key = (int(role.role_id), str(role.role_name))
            if role_key in seen_roles:
                continue
            seen_roles.add(role_key)
            resolved, warning = _resolve_role_scope(
                guild_id=request.guild_id,
                role=role,
            )
            if warning:
                warnings.append(warning)
                continue
            external_server_id, scope = resolved
            scopes = grouped.setdefault(external_server_id, [])
            if scope not in scopes:
                scopes.append(scope)

        grouped_items = tuple(grouped.items())
        if len(grouped_items) > MAX_EXTERNAL_BROADCAST_DESTINATIONS:
            warnings.append(
                f'Game {request.game_id} resolved to {len(grouped_items)} '
                'external destinations; only the first '
                f'{MAX_EXTERNAL_BROADCAST_DESTINATIONS} were attempted.'
            )
            grouped_items = grouped_items[:MAX_EXTERNAL_BROADCAST_DESTINATIONS]

        destinations = tuple(
            BroadcastDestinationPlan(
                external_server_id=server_id,
                scopes=tuple(scopes),
                content_with_join=_content(
                    game=game,
                    scopes=tuple(scopes),
                    jump_url=request.jump_url,
                    join=True,
                ),
                content_without_join=_content(
                    game=game,
                    scopes=tuple(scopes),
                    jump_url=request.jump_url,
                    join=False,
                ),
            )
            for server_id, scopes in grouped_items
        )
        return BroadcastPlanResult(
            game_id=request.game_id,
            guild_id=request.guild_id,
            status=READY,
            destinations=destinations,
            warnings=tuple(warnings),
        )


def _existing_row(request: BroadcastTargetRequest):
    return models.TeamServerBroadcastMessage.get_or_none(
        (models.TeamServerBroadcastMessage.game == request.game_id)
        & (models.TeamServerBroadcastMessage.channel_id == request.channel_id)
    )


def preflight_broadcast_target(
    request: BroadcastTargetRequest,
) -> BroadcastTargetResult:
    """Revalidate a pending target immediately before Discord publication."""

    with models.db.connection_context():
        game, status = _load_game(request)
        if game is None:
            return BroadcastTargetResult(
                status=status,
                game_id=request.game_id,
                guild_id=request.guild_id,
                channel_id=request.channel_id,
                message_id=None,
            )
        existing = _existing_row(request)
        return BroadcastTargetResult(
            status=DUPLICATE if existing is not None else READY,
            game_id=request.game_id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            message_id=(int(existing.message_id) if existing is not None else None),
            row_id=(int(existing.id) if existing is not None else None),
        )


def persist_broadcast_target(
    request: BroadcastTargetRequest,
) -> BroadcastTargetResult:
    """Atomically retain one concrete message only while its game is pending."""

    if request.message_id is None:
        raise ValueError('A concrete Discord message ID is required.')
    with models.db.connection_context():
        with models.db.atomic():
            game, status = _load_game(request)
            if game is None:
                return BroadcastTargetResult(
                    status=status,
                    game_id=request.game_id,
                    guild_id=request.guild_id,
                    channel_id=request.channel_id,
                    message_id=request.message_id,
                )
            existing = _existing_row(request)
            if existing is not None:
                return BroadcastTargetResult(
                    status=DUPLICATE,
                    game_id=request.game_id,
                    guild_id=request.guild_id,
                    channel_id=request.channel_id,
                    message_id=request.message_id,
                    row_id=int(existing.id),
                )
            row = models.TeamServerBroadcastMessage.create(
                game=game.id,
                channel_id=request.channel_id,
                message_id=request.message_id,
            )
            return BroadcastTargetResult(
                status=TRACKED,
                game_id=request.game_id,
                guild_id=request.guild_id,
                channel_id=request.channel_id,
                message_id=request.message_id,
                row_id=int(row.id),
            )


async def run_build_broadcast_plan(request: BroadcastPlanRequest):
    return await game_open_workers.pending_game_coordinator.run_worker(
        build_broadcast_plan,
        request,
    )


async def run_preflight_broadcast_target(request: BroadcastTargetRequest):
    return await game_open_workers.pending_game_coordinator.run_worker(
        preflight_broadcast_target,
        request,
    )


async def run_persist_broadcast_target(request: BroadcastTargetRequest):
    return await game_open_workers.pending_game_coordinator.run_worker(
        persist_broadcast_target,
        request,
    )
