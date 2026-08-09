"""Worker-owned persistence for Discord team-role reconciliation."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import models


class LeagueRoleReconciliationError(RuntimeError):
    """A team-role assignment could not be reconciled safely."""


@dataclass(frozen=True)
class LeagueRoleUpdateRequest:
    guild_id: int
    member_id: int
    member_description: str
    before_role_names: tuple[str, ...]
    after_role_names: tuple[str, ...]


@dataclass(frozen=True)
class LeagueRoleUpdateResult:
    guild_id: int
    member_id: int
    changed: bool
    registered: bool
    ambiguous: bool
    before_team_names: tuple[str, ...]
    after_team_name: str | None
    player_id: int | None
    previous_team_id: int | None
    team_id: int | None
    team_name: str | None
    house_name: str | None
    league_tier: int | None
    managed_house_names: tuple[str, ...]
    log_message: str | None


_league_role_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='league-role-update',
)


def _active_team_rows(guild_id: int) -> tuple[tuple[int, str], ...]:
    return tuple(
        (int(team_id), str(team_name))
        for team_id, team_name in (
            models.Team
            .select(models.Team.id, models.Team.name)
            .where(
                (models.Team.guild_id == int(guild_id))
                & (models.Team.is_hidden == False)
                & (models.Team.is_archived == False)
            )
            .order_by(models.Team.id)
            .tuples()
        )
    )


def _managed_house_names() -> tuple[str, ...]:
    return tuple(
        str(name)
        for (name,) in (
            models.House
            .select(models.House.name)
            .order_by(models.House.id)
            .tuples()
        )
    )


def _player_for_member(*, guild_id: int, member_id: int):
    return (
        models.Player
        .select(models.Player)
        .join(models.DiscordMember)
        .where(
            (models.DiscordMember.discord_id == int(member_id))
            & (models.Player.guild_id == int(guild_id))
        )
        .get()
    )


def _team_by_id(team_id: int):
    return (
        models.Team
        .select(models.Team, models.House)
        .join(models.House, peewee.JOIN.LEFT_OUTER)
        .where(models.Team.id == int(team_id))
        .get()
    )


def _empty_result(
    request: LeagueRoleUpdateRequest,
    *,
    changed: bool,
    registered: bool,
    ambiguous: bool,
    before_team_names: tuple[str, ...],
    after_team_name: str | None,
) -> LeagueRoleUpdateResult:
    return LeagueRoleUpdateResult(
        guild_id=int(request.guild_id),
        member_id=int(request.member_id),
        changed=changed,
        registered=registered,
        ambiguous=ambiguous,
        before_team_names=before_team_names,
        after_team_name=after_team_name,
        player_id=None,
        previous_team_id=None,
        team_id=None,
        team_name=None,
        house_name=None,
        league_tier=None,
        managed_house_names=(),
        log_message=None,
    )


def reconcile_league_team_role(
    request: LeagueRoleUpdateRequest,
) -> LeagueRoleUpdateResult:
    """Persist one authoritative Discord team-role transition atomically."""

    if int(request.guild_id) <= 0 or int(request.member_id) <= 0:
        raise LeagueRoleReconciliationError(
            'Guild and member IDs must be valid.'
        )
    if not str(request.member_description).strip():
        raise LeagueRoleReconciliationError(
            'The member description is required.'
        )

    with models.db.connection_context():
        with models.db.atomic():
            active_rows = _active_team_rows(int(request.guild_id))
            team_ids_by_name = {name: team_id for team_id, name in active_rows}
            captured_before_names = set(request.before_role_names)
            captured_after_names = set(request.after_role_names)
            before_team_names = tuple(
                name for _team_id, name in active_rows
                if name in captured_before_names
            )
            after_team_names = tuple(
                name for _team_id, name in active_rows
                if name in captured_after_names
            )
            if before_team_names == after_team_names:
                return _empty_result(
                    request,
                    changed=False,
                    registered=False,
                    ambiguous=False,
                    before_team_names=before_team_names,
                    after_team_name=(
                        after_team_names[0] if len(after_team_names) == 1
                        else None
                    ),
                )
            if len(after_team_names) > 1:
                return _empty_result(
                    request,
                    changed=True,
                    registered=False,
                    ambiguous=True,
                    before_team_names=before_team_names,
                    after_team_name=None,
                )

            after_team_name = (
                after_team_names[0] if after_team_names else None
            )
            try:
                player = _player_for_member(
                    guild_id=int(request.guild_id),
                    member_id=int(request.member_id),
                )
            except peewee.DoesNotExist:
                return _empty_result(
                    request,
                    changed=True,
                    registered=False,
                    ambiguous=False,
                    before_team_names=before_team_names,
                    after_team_name=after_team_name,
                )

            previous_team_id = (
                int(player.team_id) if player.team_id is not None else None
            )
            team = None
            if after_team_name is not None:
                team = _team_by_id(team_ids_by_name[after_team_name])
                player.team = int(team.id)
            else:
                player.team = None
            player.save(only=[models.Player.team])
            if team is not None:
                models.PlayerHousePreference.clear_preferences(player.id)

            if after_team_name is not None:
                log_message = (
                    f'{request.member_description} had team role '
                    f'**{after_team_name}** added.'
                )
            else:
                removed_name = (
                    before_team_names[0] if before_team_names else 'Unknown'
                )
                log_message = (
                    f'{request.member_description} had team role '
                    f'**{removed_name}** removed and is teamless.'
                )
            models.GameLog.write(
                guild_id=int(request.guild_id),
                game_id=0,
                message=log_message,
            )
            managed_house_names = _managed_house_names()

    return LeagueRoleUpdateResult(
        guild_id=int(request.guild_id),
        member_id=int(request.member_id),
        changed=True,
        registered=True,
        ambiguous=False,
        before_team_names=before_team_names,
        after_team_name=after_team_name,
        player_id=int(player.id),
        previous_team_id=previous_team_id,
        team_id=int(team.id) if team is not None else None,
        team_name=str(team.name) if team is not None else None,
        house_name=(
            str(team.house.name)
            if team is not None and team.house_id is not None
            else None
        ),
        league_tier=(
            int(team.league_tier)
            if team is not None and team.league_tier is not None
            else None
        ),
        managed_house_names=managed_house_names,
        log_message=log_message,
    )


async def _drain_future(future: Future):
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


async def run_league_team_role_update(
    request: LeagueRoleUpdateRequest,
) -> LeagueRoleUpdateResult:
    return await _drain_future(
        _league_role_executor.submit(reconcile_league_team_role, request)
    )
