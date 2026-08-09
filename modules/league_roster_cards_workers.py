"""Bounded image resolution and rendering for league roster cards."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re

from modules import image_storage, imgen, models


MAX_TEXT_LENGTH = 80
MAX_SOURCE_LENGTH = 2048
DEFAULT_LEFT_COLOUR = '#00ff00'
DEFAULT_RIGHT_COLOUR = '#ff0000'


class RosterCardError(RuntimeError):
    """Base user-facing roster-card failure."""


class RosterCardValidationError(RosterCardError):
    """The supplied card fields are invalid."""


class RosterCardImageError(RosterCardError):
    """An image source could not be resolved or rendered."""


@dataclass(frozen=True)
class RoleColourSnapshot:
    name: str
    colour: str


@dataclass(frozen=True)
class ImageSource:
    """Primitive source description.

    ``lookup`` preserves the prefix command's team-first resolution while
    allowing a captured member-avatar URL as its fallback.
    """

    kind: str
    value: str
    fallback_url: str | None = None


@dataclass(frozen=True)
class RosterCardRequest:
    guild_id: int
    mode: str
    top_text: str
    bottom_text: str
    left: ImageSource
    right: ImageSource
    role_colours: tuple[RoleColourSnapshot, ...]


@dataclass(frozen=True)
class RosterCardResult:
    mode: str
    image_bytes: bytes
    filename: str


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='league-roster-card')


def _text(value: str) -> str:
    value = str(value or '').strip()
    if len(value) > MAX_TEXT_LENGTH:
        raise RosterCardValidationError(
            f'Card text must be {MAX_TEXT_LENGTH} characters or fewer.'
        )
    return value


def _colour_map(request: RosterCardRequest) -> dict[str, str]:
    return {row.name.casefold(): row.colour for row in request.role_colours}


def _resolve_team(guild_id: int, lookup: str, colours: dict[str, str]):
    matches = models.Team.get_by_name(
        team_name=str(lookup), guild_id=int(guild_id), require_exact=False
    )
    if len(matches) != 1:
        return None
    team = matches[0]
    source = image_storage.resolve_image('team', team)
    if not source:
        raise RosterCardImageError(
            f'Team {team.name!r} does not have an image set.'
        )
    colour = colours.get(str(team.name).casefold())
    if not colour:
        raise RosterCardImageError(
            f'Team {team.name!r} does not have an exact Discord role.'
        )
    return source, colour


def _resolve_source(
    request: RosterCardRequest,
    source: ImageSource,
    *,
    default_colour: str,
    colours: dict[str, str],
):
    kind = str(source.kind)
    value = str(source.value or '').strip()
    if len(value) > MAX_SOURCE_LENGTH:
        raise RosterCardValidationError('Image source is too long.')
    if kind == 'url':
        try:
            return image_storage.validate_http_url(value), default_colour
        except image_storage.ImageStorageError as exc:
            raise RosterCardValidationError(str(exc)) from exc
    if kind in {'team', 'lookup'}:
        resolved = _resolve_team(request.guild_id, value, colours)
        if resolved is not None:
            team_source, team_colour = resolved
            override = str(source.fallback_url or '').strip()
            if kind == 'team' and override:
                try:
                    team_source = image_storage.validate_http_url(override)
                except image_storage.ImageStorageError as exc:
                    raise RosterCardValidationError(str(exc)) from exc
            return team_source, team_colour
        if kind == 'team':
            raise RosterCardImageError(
                f'Could not uniquely resolve team {value!r} in this server.'
            )
        fallback = str(source.fallback_url or '').strip()
        if fallback:
            try:
                return image_storage.validate_http_url(fallback), default_colour
            except image_storage.ImageStorageError as exc:
                raise RosterCardValidationError(str(exc)) from exc
        raise RosterCardImageError(
            f'Could not convert {value!r} to a team image or member avatar.'
        )
    raise RosterCardValidationError(f'Unsupported image source type: {kind!r}.')


def _render(request: RosterCardRequest) -> RosterCardResult:
    if request.mode not in {'promote', 'trade'}:
        raise RosterCardValidationError('Card mode must be promote or trade.')
    top_text = _text(request.top_text)
    bottom_text = _text(request.bottom_text)
    colours = _colour_map(request)

    needs_database = request.left.kind in {'team', 'lookup'} or request.right.kind in {
        'team', 'lookup'
    }

    def resolve_and_render():
        left_image, left_colour = _resolve_source(
            request,
            request.left,
            default_colour=DEFAULT_LEFT_COLOUR,
            colours=colours,
        )
        right_image, right_colour = _resolve_source(
            request,
            request.right,
            default_colour=DEFAULT_RIGHT_COLOUR,
            colours=colours,
        )
        arrows = (
            (('u', DEFAULT_LEFT_COLOUR),)
            if request.mode == 'promote'
            else (('r', left_colour), ('l', right_colour))
        )
        rendered = imgen.arrow_card(
            top_text, bottom_text, left_image, right_image, arrows
        )
        try:
            rendered.fp.seek(0)
            payload = bytes(rendered.fp.read())
        finally:
            rendered.close()
        safe_mode = re.sub(r'[^a-z0-9-]+', '-', request.mode.casefold()).strip('-')
        return RosterCardResult(
            mode=request.mode,
            image_bytes=payload,
            filename=f'{safe_mode or "roster"}-card.png',
        )

    if needs_database:
        with models.db.connection_context():
            return resolve_and_render()
    return resolve_and_render()


async def run_roster_card(request: RosterCardRequest) -> RosterCardResult:
    """Render in the bounded executor and drain non-cancellable thread work."""

    loop = asyncio.get_running_loop()
    future = asyncio.wrap_future(_executor.submit(_render, request), loop=loop)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await asyncio.shield(future)
        raise
