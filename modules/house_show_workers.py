"""Bounded worker-local reads for House show and directory commands."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import logging

from modules import exceptions, models
import settings


logger = logging.getLogger('polybot.' + __name__)

MAX_HOUSES = 50
MAX_TEAMS = 200
MAX_MEMBERS = 5000
MAX_ROSTER_PER_TEAM = 100


class HouseShowError(RuntimeError):
    """Base user-facing House read failure."""


class HouseShowPermissionError(HouseShowError):
    """The captured request is outside the approved league/read scope."""


class HouseShowLookupError(HouseShowError):
    """The selected or inferred House cannot be resolved unambiguously."""


class HouseShowPublicationError(HouseShowError):
    """A loaded public House workspace could not be published."""


@dataclass(frozen=True)
class HouseMemberSnapshot:
    discord_id: int
    display_name: str
    role_names: tuple[str, ...]


@dataclass(frozen=True)
class HouseGuildSnapshot:
    guild_id: int
    members: tuple[HouseMemberSnapshot, ...]
    role_names: tuple[str, ...]


@dataclass(frozen=True)
class HouseShowRequest:
    guild_id: int
    requester_id: int
    house_lookup: str | None
    require_selection: bool
    league_scope: bool
    channel_allowed: bool
    inactive_role_name: str | None
    guild_snapshot: HouseGuildSnapshot


@dataclass(frozen=True)
class HouseRosterRow:
    discord_id: int
    display_name: str
    elo: int | None


@dataclass(frozen=True)
class HouseTeamRow:
    team_id: int
    name: str
    emoji: str
    elo: int
    league_tier: int | None
    tier_name: str | None
    archived: bool
    role_found: bool
    roster: tuple[HouseRosterRow, ...]
    roster_truncated: bool


@dataclass(frozen=True)
class HouseRow:
    house_id: int
    name: str
    emoji: str
    image_url: str | None
    league_tokens: int
    role_found: bool
    leaders: tuple[str, ...]
    coleaders: tuple[str, ...]
    recruiters: tuple[str, ...]
    teams: tuple[HouseTeamRow, ...]


@dataclass(frozen=True)
class HouseShowResult:
    guild_id: int
    requester_id: int
    houses: tuple[HouseRow, ...]
    selected_house_id: int | None
    houses_truncated: bool
    teams_truncated: bool


def _tier_name(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return str(settings.tier_lookup(int(value))[1])
    except (AttributeError, TypeError, ValueError, exceptions.NoMatches):
        return None


def _validate_request(request: HouseShowRequest) -> None:
    if int(request.guild_id) != int(request.guild_snapshot.guild_id):
        raise HouseShowPermissionError(
            'The captured guild does not match this request.'
        )
    if not request.league_scope:
        raise HouseShowPermissionError(
            'House commands are available only in the configured league server.'
        )
    if not request.channel_allowed:
        raise HouseShowPermissionError(
            'This command can only be used in a designated ELO bot channel.'
        )
    if len(request.guild_snapshot.members) > MAX_MEMBERS:
        raise HouseShowPermissionError(
            'This server is too large to build the House directory safely.'
        )


def _resolve_selected_house(
    request: HouseShowRequest,
    houses,
) -> int | None:
    lookup = str(request.house_lookup or '').strip()
    if lookup:
        exact = [house for house in houses if str(house.name).casefold() == lookup.casefold()]
        matches = exact or [
            house for house in houses
            if lookup.casefold() in str(house.name).casefold()
        ]
        if not matches:
            raise HouseShowLookupError(
                f'No matching House was found for "{lookup}".'
            )
        if len(matches) > 1:
            raise HouseShowLookupError(
                f'More than one matching House was found for "{lookup}".'
            )
        return int(matches[0].id)

    if not request.require_selection:
        return None

    requester = next(
        (
            member for member in request.guild_snapshot.members
            if int(member.discord_id) == int(request.requester_id)
        ),
        None,
    )
    requester_roles = set(requester.role_names) if requester is not None else set()
    matches = [house for house in houses if str(house.name) in requester_roles]
    if not matches:
        raise HouseShowLookupError(
            'Your House could not be inferred. Choose a House explicitly.'
        )
    if len(matches) > 1:
        raise HouseShowLookupError(
            'Your House is ambiguous. Choose a House explicitly.'
        )
    return int(matches[0].id)


def _load_players(guild_id: int, member_ids: tuple[int, ...]):
    if not member_ids:
        return ()
    return tuple(
        models.Player
        .select(models.Player, models.DiscordMember)
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == int(guild_id))
            & models.DiscordMember.discord_id.in_(member_ids)
        )
    )


def load_house_show(request: HouseShowRequest) -> HouseShowResult:
    """Synchronously load one immutable House directory snapshot."""

    _validate_request(request)
    with models.db.connection_context():
        house_rows = tuple(
            models.House.select().order_by(models.House.name).limit(MAX_HOUSES + 1)
        )
        houses_truncated = len(house_rows) > MAX_HOUSES
        houses = house_rows[:MAX_HOUSES]
        if not houses:
            raise HouseShowLookupError('No Houses are configured.')

        selected_house_id = _resolve_selected_house(request, houses)
        house_ids = tuple(int(house.id) for house in houses)
        team_rows = tuple(
            models.Team.select()
            .where(
                (models.Team.guild_id == int(request.guild_id))
                & models.Team.house.in_(house_ids)
                & (models.Team.is_hidden == 0)
            )
            .order_by(
                models.Team.house,
                models.Team.is_archived,
                models.Team.league_tier,
                -models.Team.elo,
                models.Team.id,
            )
            .limit(MAX_TEAMS + 1)
        )
        teams_truncated = len(team_rows) > MAX_TEAMS
        teams = team_rows[:MAX_TEAMS]

        member_ids = tuple(
            int(member.discord_id) for member in request.guild_snapshot.members
        )
        players = _load_players(request.guild_id, member_ids)
        elo_by_discord_id = {
            int(player.discord_member.discord_id): int(player.elo_moonrise)
            for player in players
        }

        members = tuple(request.guild_snapshot.members)
        role_names = set(request.guild_snapshot.role_names)
        inactive_name = request.inactive_role_name
        teams_by_house: dict[int, list[HouseTeamRow]] = {
            int(house.id): [] for house in houses
        }
        for team in teams:
            team_name = str(team.name)
            team_members = [
                member for member in members
                if team_name in member.role_names
                and (not inactive_name or inactive_name not in member.role_names)
            ]
            roster_rows = sorted(
                (
                    HouseRosterRow(
                        discord_id=int(member.discord_id),
                        display_name=str(member.display_name),
                        elo=elo_by_discord_id.get(int(member.discord_id)),
                    )
                    for member in team_members
                ),
                key=lambda row: (
                    row.elo is None,
                    -(row.elo if row.elo is not None else 0),
                    row.display_name.casefold(),
                    row.discord_id,
                ),
            )
            roster_truncated = len(roster_rows) > MAX_ROSTER_PER_TEAM
            roster = tuple(roster_rows[:MAX_ROSTER_PER_TEAM])
            house_id = int(team.house_id)
            teams_by_house.setdefault(house_id, []).append(
                HouseTeamRow(
                    team_id=int(team.id),
                    name=team_name,
                    emoji=str(getattr(team, 'emoji', '') or ''),
                    elo=int(getattr(team, 'elo', 0)),
                    league_tier=(
                        int(team.league_tier)
                        if getattr(team, 'league_tier', None) is not None
                        else None
                    ),
                    tier_name=_tier_name(getattr(team, 'league_tier', None)),
                    archived=bool(getattr(team, 'is_archived', False)),
                    role_found=team_name in role_names,
                    roster=roster,
                    roster_truncated=roster_truncated,
                )
            )

        result_houses = []
        for house in houses:
            house_name = str(house.name)
            house_members = [
                member for member in members if house_name in member.role_names
            ]

            def leadership(role_name: str) -> tuple[str, ...]:
                return tuple(
                    str(member.display_name)
                    for member in house_members
                    if role_name in member.role_names
                )

            result_houses.append(
                HouseRow(
                    house_id=int(house.id),
                    name=house_name,
                    emoji=str(getattr(house, 'emoji', '') or ''),
                    image_url=(
                        str(house.image_url)
                        if getattr(house, 'image_url', None)
                        else None
                    ),
                    league_tokens=int(getattr(house, 'league_tokens', 0)),
                    role_found=house_name in role_names,
                    leaders=leadership('House Leader'),
                    coleaders=leadership('House Co-Leader'),
                    recruiters=leadership('House Recruiter'),
                    teams=tuple(teams_by_house.get(int(house.id), ())),
                )
            )

        return HouseShowResult(
            guild_id=int(request.guild_id),
            requester_id=int(request.requester_id),
            houses=tuple(result_houses),
            selected_house_id=selected_house_id,
            houses_truncated=houses_truncated,
            teams_truncated=teams_truncated,
        )


_house_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-house-read',
)


async def run_house_show(request: HouseShowRequest) -> HouseShowResult:
    """Run the bounded read without blocking Discord and drain cancellation."""

    concurrent_future = _house_read_executor.submit(
        functools.partial(load_house_show, request)
    )
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException:
            logger.exception('Cancelled House read completed with an error')
        raise asyncio.CancelledError
    return concurrent_future.result()
