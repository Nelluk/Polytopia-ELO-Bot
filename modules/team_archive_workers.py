"""Bounded worker for the destructive team-archive transition."""

from __future__ import annotations

from dataclasses import dataclass

from modules import models, team_emoji_workers


class TeamArchiveError(RuntimeError):
    """Base error for a rejected team-archive request."""


class TeamArchivePermissionError(TeamArchiveError):
    """The requester or guild policy does not permit archival."""


class TeamArchiveValidationError(TeamArchiveError):
    """The selected Team is not eligible for archival."""


class TeamArchiveConflictError(TeamArchiveValidationError):
    """The Team changed after the Discord-side preflight."""


@dataclass(frozen=True)
class TeamArchiveRequest:
    """Immutable primitive request captured before worker submission."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    league_scope: bool
    team_lookup: str
    expected_team_id: int
    team_role_id: int
    team_role_name: str
    requester_description: str
    confirmed: bool
    invoked_with: str = '/team archive'


@dataclass(frozen=True)
class TeamArchiveResult:
    """Immutable primitive result returned only after commit."""

    guild_id: int
    team_id: int
    team_name: str
    audit_message: str


def _validate_access(request: TeamArchiveRequest) -> None:
    if not bool(request.team_enabled):
        raise TeamArchivePermissionError('Teams are not enabled on this server.')
    if not bool(request.league_scope):
        raise TeamArchivePermissionError(
            'Teams can only be archived in the PolyChampions league server.'
        )
    if not bool(request.requester_is_mod):
        raise TeamArchivePermissionError(
            'You do not have permission to archive teams.'
        )
    if not bool(request.confirmed):
        raise TeamArchiveValidationError(
            'Team archival was not confirmed. Set `confirm` to true only '
            'after checking the selected Team.'
        )


def _resolve_team(request: TeamArchiveRequest):
    try:
        team = team_emoji_workers._resolve_team(request, include_hidden=False)
    except team_emoji_workers.TeamEmojiLookupError as exc:
        raise TeamArchiveValidationError(str(exc)) from exc
    if int(team.id) != int(request.expected_team_id):
        raise TeamArchiveConflictError(
            f'Team {team.name} changed before archival was applied.'
        )
    return team


def _validate_exact_role(request: TeamArchiveRequest, team) -> None:
    if int(request.team_role_id) <= 0 or str(request.team_role_name) != str(
        team.name
    ):
        raise TeamArchiveConflictError(
            f'The exact Discord role for Team {team.name} changed before '
            'archival was applied.'
        )


def _incomplete_game_count(team) -> int:
    return int(
        models.Game.search(team_filter=[team], status_filter=2).count()
    )


def archive_team(request: TeamArchiveRequest) -> TeamArchiveResult:
    """Archive and audit one eligible Team in one synchronous transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            _validate_access(request)
            team = _resolve_team(request)
            _validate_exact_role(request, team)
            if bool(team.is_archived):
                raise TeamArchiveValidationError(
                    f'Team **{team.name}** is already archived.'
                )
            if team.house is not None:
                raise TeamArchiveValidationError(
                    f'Remove the House affiliation of Team **{team.name}** '
                    f'with `/team house team:{team.name} clear:true` before '
                    'archiving it. '
                    f'Current House: **{team.house.name}**.'
                )
            incomplete_count = _incomplete_game_count(team)
            if incomplete_count:
                raise TeamArchiveValidationError(
                    f'Team **{team.name}** has {incomplete_count} incomplete '
                    'game(s). A Team can be archived only when that count is '
                    'zero.'
                )

            team.is_archived = True
            team.save()
            audit_message = (
                f'{request.requester_description} archived Team {team.name} '
                f'ID {team.id}. ({request.invoked_with})'
            )
            models.GameLog.write(
                guild_id=int(request.guild_id),
                message=audit_message,
            )
            return TeamArchiveResult(
                guild_id=int(request.guild_id),
                team_id=int(team.id),
                team_name=str(team.name),
                audit_message=audit_message,
            )


async def run_team_archive(
    request: TeamArchiveRequest,
) -> TeamArchiveResult:
    """Submit archival to the existing bounded team executor."""

    return await team_emoji_workers.run_bounded_team_worker(
        archive_team,
        request,
        drain_on_cancel=True,
    )
