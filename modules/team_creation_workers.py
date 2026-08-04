"""Bounded workers for native team creation."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata

import peewee

from modules import models, team_emoji_workers


MAX_TEAM_ROLE_NAME_LENGTH = 100


class TeamCreationValidationError(RuntimeError):
    """The request contains an invalid or unsafe team name."""


class TeamCreationPermissionError(TeamCreationValidationError):
    """The captured requester or guild policy does not permit creation."""


class TeamCreationConflictError(TeamCreationValidationError):
    """The team name conflicts with an existing or concurrent insert."""


@dataclass(frozen=True)
class TeamCreationRequest:
    """Immutable primitive input for one team creation."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    name: str | None
    requester_description: str
    native: bool = True
    invoked_with: str = '/team create'


@dataclass(frozen=True)
class TeamCreationResult:
    """Immutable primitive result after the Team and audit commit."""

    guild_id: int
    team_id: int
    team_name: str
    native: bool


# Short aliases keep the worker's public vocabulary consistent with the
# existing focused team workers while retaining the more explicit class names.
TeamCreateRequest = TeamCreationRequest
TeamCreateResult = TeamCreationResult


def _validate_access(request: TeamCreationRequest) -> None:
    """Revalidate the captured permission/scope snapshot in the worker."""

    if not bool(request.team_enabled):
        raise TeamCreationPermissionError('Teams are not enabled on this server.')
    if not bool(request.requester_is_mod):
        raise TeamCreationPermissionError(
            'You do not have permission to create teams.'
        )


def validate_team_name(value: str | None) -> str:
    """Return a trimmed Discord-role-compatible team name.

    Discord roles require a non-empty name and cap it at 100 characters.  The
    worker rejects control/format characters and reserved broadcast names so
    the exact-role membership convention remains unambiguous and safe in
    public output.  The model's older five-character edit rule is not applied
    to creation: a one-character Discord role name is valid.
    """

    if value is None or not isinstance(value, str):
        raise TeamCreationValidationError(
            'A team name is required and must match the Discord role name '
            'limit of 1 to 100 characters.'
        )

    name = value.strip()
    if not name:
        raise TeamCreationValidationError(
            'A team name is required and cannot be empty.'
        )
    if len(name) > MAX_TEAM_ROLE_NAME_LENGTH:
        raise TeamCreationValidationError(
            'Team names must be 100 characters or fewer.'
        )
    if any(
        unicodedata.category(character).startswith('C')
        or unicodedata.category(character) in {'Zl', 'Zp'}
        for character in name
    ):
        raise TeamCreationValidationError(
            'Team names cannot contain control or invisible formatting '
            'characters.'
        )
    if name.casefold() in {'@everyone', '@here'}:
        raise TeamCreationValidationError(
            'That name is reserved by Discord and cannot be used for a team.'
        )
    return name


def _audit_message(
    request: TeamCreationRequest,
    *,
    team_id: int,
    team_name: str,
) -> str:
    invocation_note = (
        f' ({request.invoked_with})'
        if str(request.invoked_with).startswith('/')
        else ''
    )
    return (
        f'{request.requester_description} created Team {team_name} '
        f'ID {team_id}.{invocation_note}'
    )


def create_team(request: TeamCreationRequest) -> TeamCreationResult:
    """Create and audit one team on a worker-local Peewee connection."""

    with models.db.connection_context():
        with models.db.atomic():
            _validate_access(request)
            team_name = validate_team_name(request.name)
            try:
                # Keep this create intentionally narrow.  Model defaults own
                # every other Team field; this unit does not create roles,
                # houses, players, or any other related state.
                team = models.Team.create(
                    name=team_name,
                    guild_id=int(request.guild_id),
                    is_hidden=False,
                )
            except peewee.IntegrityError as exc:
                # The composite (name, guild_id) unique index is authoritative
                # for both pre-existing duplicates and racing inserts.
                raise TeamCreationConflictError(
                    f'A team named "{team_name}" already exists on this server.'
                ) from exc

            team_id = int(team.id)
            stored_name = str(getattr(team, 'name', team_name))
            models.GameLog.write(
                guild_id=int(request.guild_id),
                message=_audit_message(
                    request,
                    team_id=team_id,
                    team_name=stored_name,
                ),
            )
            return TeamCreationResult(
                guild_id=int(request.guild_id),
                team_id=team_id,
                team_name=stored_name,
                native=bool(request.native),
            )


async def run_team_creation(
    request: TeamCreationRequest,
) -> TeamCreationResult:
    """Submit creation to the existing bounded team executor.

    ``run_bounded_team_worker`` owns the shared executor and cancellation
    drain, so a cancelled interaction cannot leave database work running
    beyond the worker's lifecycle.
    """

    return await team_emoji_workers.run_bounded_team_worker(
        create_team,
        request,
        drain_on_cancel=True,
    )


# Explicit aliases make the worker convenient to discover without adding a
# second executor or a second implementation path.
run_team_create = run_team_creation
