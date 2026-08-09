"""Bounded database reads and rendering for league draft cards."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re

from modules import exceptions, image_storage, imgen, models


MAX_NAME_LENGTH = 100
MAX_URL_LENGTH = 2048


class DraftCardError(RuntimeError):
    """Base user-facing draft-card failure."""


class DraftCardValidationError(DraftCardError):
    """The captured Discord input is invalid."""


class DraftCardLookupError(DraftCardError):
    """A required persisted player, Team, role, or image is unavailable."""


@dataclass(frozen=True)
class RoleColourSnapshot:
    name: str
    colour: str


@dataclass(frozen=True)
class DraftCardRequest:
    guild_id: int
    player_discord_id: int
    player_name: str
    player_avatar_url: str
    team_name: str
    role_colours: tuple[RoleColourSnapshot, ...]


@dataclass(frozen=True)
class DraftCardResult:
    player_name: str
    team_name: str
    image_bytes: bytes
    filename: str


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='league-draft-card')


def _clean_text(value: str, *, label: str) -> str:
    value = str(value or '').strip()
    if not value:
        raise DraftCardValidationError(f'{label} is required.')
    if len(value) > MAX_NAME_LENGTH:
        raise DraftCardValidationError(
            f'{label} must be {MAX_NAME_LENGTH} characters or fewer.'
        )
    return value


def _player_summary(player) -> str:
    local_wins, local_losses = player.get_record()
    global_wins, global_losses = player.discord_member.get_record()
    return (
        f'LOCAL\n  {player.elo_moonrise} ELO\n'
        f'  {local_wins} W / {local_losses} L\n'
        f'GLOBAL\n  {player.discord_member.elo_moonrise} ELO\n'
        f'  {global_wins} W / {global_losses} L'
    )


def _render(request: DraftCardRequest) -> DraftCardResult:
    if int(request.guild_id) <= 0 or int(request.player_discord_id) <= 0:
        raise DraftCardValidationError('The server and player IDs must be valid.')
    player_name = _clean_text(request.player_name, label='Player name')
    team_name = _clean_text(request.team_name, label='Team')
    avatar_url = str(request.player_avatar_url or '').strip()
    if len(avatar_url) > MAX_URL_LENGTH:
        raise DraftCardValidationError('The player avatar URL is too long.')
    try:
        avatar_url = image_storage.validate_http_url(avatar_url)
    except image_storage.ImageStorageError as exc:
        raise DraftCardValidationError(str(exc)) from exc

    role_colours = {
        str(row.name).casefold(): str(row.colour)
        for row in request.role_colours
    }

    with models.db.connection_context():
        try:
            team = models.Team.get_or_except(
                team_name=team_name,
                guild_id=int(request.guild_id),
                require_exact=True,
            )
        except exceptions.NoSingleMatch as exc:
            raise DraftCardLookupError(f'Could not find that Team: {exc}') from exc
        team_image = image_storage.resolve_image('team', team)
        if not team_image:
            raise DraftCardLookupError(
                f'Team {team.name!r} does not have an image set. '
                'Use /team image first.'
            )
        team_colour = role_colours.get(str(team.name).casefold())
        if not team_colour:
            raise DraftCardLookupError(
                f'Team {team.name!r} does not have an exact Discord role.'
            )
        try:
            player = models.Player.get_or_except(
                player_string=str(int(request.player_discord_id)),
                guild_id=int(request.guild_id),
            )
        except exceptions.NoSingleMatch as exc:
            raise DraftCardLookupError(
                f'{player_name} is not registered in this server.'
            ) from exc

        selecting_string = team.name
        if team.house_id:
            house_name = str(team.house.name)
            # Preserve the legacy card rule: use the House label only when an
            # exact Discord House role is present; otherwise fall back to the
            # exact Team role already validated above.
            if house_name.casefold() in role_colours:
                selecting_string = house_name
        summary = _player_summary(player)
        rendered = imgen.player_draft_card_from_sources(
            player_name=player_name,
            player_avatar_source=avatar_url,
            player_summary=summary,
            team_name=str(team.name),
            team_image_source=team_image,
            team_colour=team_colour,
            selecting_string=str(selecting_string),
        )
        try:
            rendered.fp.seek(0)
            payload = bytes(rendered.fp.read())
        finally:
            rendered.close()

    safe_team = re.sub(r'[^a-z0-9-]+', '-', str(team.name).casefold()).strip('-')
    safe_player = re.sub(r'[^a-z0-9-]+', '-', player_name.casefold()).strip('-')
    return DraftCardResult(
        player_name=player_name,
        team_name=str(team.name),
        image_bytes=payload,
        filename=f'{safe_team or "team"}-selects-{safe_player or "player"}.png',
    )


async def run_draft_card(request: DraftCardRequest) -> DraftCardResult:
    """Render in the bounded executor and drain non-cancellable thread work."""

    loop = asyncio.get_running_loop()
    future = asyncio.wrap_future(_executor.submit(_render, request), loop=loop)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await asyncio.shield(future)
        raise
