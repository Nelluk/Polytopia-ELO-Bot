"""Bounded workers for focused team name, server, and tier attributes."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import settings
from modules import exceptions, models, team_emoji_workers, team_record_scope


logger = logging.getLogger('polybot.' + __name__)


TEAM_ATTRIBUTE_NAME = 'name'
TEAM_ATTRIBUTE_SERVER = 'server'
TEAM_ATTRIBUTE_TIER = 'tier'
TEAM_ATTRIBUTE_HOUSE = 'house'
TEAM_ATTRIBUTES = frozenset({
    TEAM_ATTRIBUTE_NAME,
    TEAM_ATTRIBUTE_SERVER,
    TEAM_ATTRIBUTE_TIER,
    TEAM_ATTRIBUTE_HOUSE,
})


class TeamAttributeValidationError(RuntimeError):
    """The request contains an invalid or unsafe team attribute value."""


class TeamAttributeLookupError(TeamAttributeValidationError):
    """The requested team cannot be resolved unambiguously."""


class TeamAttributePermissionError(TeamAttributeValidationError):
    """The captured requester or guild policy does not permit the operation."""


class TeamAttributeConflictError(TeamAttributeValidationError):
    """The mutation conflicts with current team state or another team."""


class TeamTierRoleError(TeamAttributeValidationError):
    """The exact Discord team-role precondition was not satisfied."""


@dataclass(frozen=True)
class TeamAttributeReadRequest:
    """Primitive input for one bounded current-value team read."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    # Captured before worker submission; league-only attributes also enforce
    # the legacy PolyChampions/test scope in the worker.
    league_scope: bool
    team_lookup: str | None
    attribute: str
    requester_description: str
    include_hidden: bool = True
    invoked_with: str = 'team'


@dataclass(frozen=True)
class TeamAttributeMutationRequest:
    """Primitive input for one atomic team attribute mutation."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    # Captured before worker submission; league-only attributes also enforce
    # the legacy PolyChampions/test scope in the worker.
    league_scope: bool
    team_lookup: str | None
    attribute: str
    name: str | None = None
    server_id: int | None = None
    tier: str | None = None
    house: str | None = None
    clear: bool = False
    requester_description: str = ''
    include_hidden: bool = True
    expected_team_id: int | None = None
    expected_value: str | int | None = None
    expected_value_present: bool = False
    team_role_id: int | None = None
    team_role_name: str | None = None
    native: bool = True
    invoked_with: str = '/team'
    prefix: str = '$'
    team_member_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class TeamAttributeReadResult:
    """Immutable primitive current-value snapshot."""

    guild_id: int
    team_id: int
    team_name: str
    attribute: str
    value: str | int | None
    external_server: int | None
    league_tier: int | None
    tier_name: str | None
    house_name: str | None
    is_hidden: bool
    is_archived: bool
    house_role_names: tuple[str, ...]


@dataclass(frozen=True)
class TeamAttributeMutationResult:
    """Immutable primitive result after the transaction commits."""

    guild_id: int
    team_id: int
    attribute: str
    team_name: str
    old_team_name: str
    new_team_name: str
    old_value: str | int | None
    value: str | int | None
    old_tier: int | None
    new_tier: int | None
    old_tier_name: str | None
    new_tier_name: str | None
    old_house_name: str | None
    house_name: str | None
    team_role_id: int | None
    house_role_names: tuple[str, ...]
    cleared: bool
    native: bool
    persisted_member_ids: tuple[int, ...] = ()
    persisted_member_failures: tuple[int, ...] = ()


def _validate_attribute(attribute: str) -> str:
    attribute = str(attribute)
    if attribute not in TEAM_ATTRIBUTES:
        raise TeamAttributeValidationError(
            f'Unsupported team attribute: {attribute}.'
        )
    return attribute


def _validate_access(request) -> str:
    attribute = _validate_attribute(request.attribute)
    if not bool(request.team_enabled):
        raise TeamAttributePermissionError('Teams are not enabled on this server.')
    if attribute in {TEAM_ATTRIBUTE_TIER, TEAM_ATTRIBUTE_HOUSE} and not bool(
        request.league_scope
    ):
        if attribute == TEAM_ATTRIBUTE_TIER:
            raise TeamAttributePermissionError(
                'Team tiers can only be managed in the PolyChampions league server.'
            )
        raise TeamAttributePermissionError(
            'Team houses can only be viewed or managed in the PolyChampions '
            'league server.'
        )
    if (
        isinstance(request, TeamAttributeMutationRequest)
        and not bool(request.requester_is_mod)
    ):
        raise TeamAttributePermissionError(
            'You do not have permission to manage team attributes.'
        )
    if (
        isinstance(request, TeamAttributeReadRequest)
        and attribute not in {TEAM_ATTRIBUTE_HOUSE}
        and not bool(request.requester_is_mod)
    ):
        raise TeamAttributePermissionError(
            'You do not have permission to manage team attributes.'
        )
    if (
        isinstance(request, TeamAttributeMutationRequest)
        and attribute in {TEAM_ATTRIBUTE_NAME, TEAM_ATTRIBUTE_TIER}
        and request.clear
    ):
        raise TeamAttributeValidationError(
            f'Team {attribute}s cannot be cleared in this workflow.'
        )
    return attribute


def _resolve_team(request, *, include_hidden: bool):
    try:
        return team_emoji_workers._resolve_team(
            request,
            include_hidden=bool(include_hidden),
        )
    except team_emoji_workers.TeamEmojiLookupError as exc:
        raise TeamAttributeLookupError(str(exc)) from exc


def _house_name(team) -> str | None:
    house = getattr(team, 'house', None)
    if house is None:
        return None
    name = getattr(house, 'name', None)
    return str(name) if name is not None else None


def _house_role_names() -> tuple[str, ...]:
    return tuple(
        sorted({
            str(house.name)
            for house in models.House.select(models.House.name)
            if getattr(house, 'name', None)
        })
    )


def _tier_name(tier: int | None) -> str | None:
    if tier is None:
        return None
    try:
        return str(settings.tier_lookup(int(tier))[1])
    except (TypeError, ValueError, exceptions.NoMatches):
        return None


def _snapshot(
    team,
    *,
    guild_id: int,
    attribute: str,
) -> TeamAttributeReadResult:
    team_name = str(team.name)
    external_server = (
        int(team.external_server)
        if getattr(team, 'external_server', None) is not None
        else None
    )
    league_tier = (
        int(team.league_tier)
        if getattr(team, 'league_tier', None) is not None
        else None
    )
    house_name = _house_name(team)
    if attribute == TEAM_ATTRIBUTE_NAME:
        value: str | int | None = team_name
    elif attribute == TEAM_ATTRIBUTE_SERVER:
        value = external_server
    elif attribute == TEAM_ATTRIBUTE_HOUSE:
        value = house_name
    else:
        value = league_tier
    return TeamAttributeReadResult(
        guild_id=int(guild_id),
        team_id=int(team.id),
        team_name=team_name,
        attribute=attribute,
        value=value,
        external_server=external_server,
        league_tier=league_tier,
        tier_name=_tier_name(league_tier),
        house_name=house_name,
        is_hidden=bool(getattr(team, 'is_hidden', False)),
        is_archived=bool(getattr(team, 'is_archived', False)),
        house_role_names=(
            _house_role_names()
            if attribute in {TEAM_ATTRIBUTE_TIER, TEAM_ATTRIBUTE_HOUSE}
            else ()
        ),
    )


def _validate_tier_preconditions(request, team) -> None:
    team_name = str(team.name)
    if bool(getattr(team, 'is_archived', False)):
        raise TeamAttributeValidationError(
            f'Team **{team_name}** is **archived**. If it really needs to be '
            'unarchived, ask the bot owner.'
        )
    if getattr(team, 'house', None) is None:
        raise TeamAttributeValidationError(
            f'Team **{team_name}** does not have a House affiliation. '
            'Set one with `/team house` first.'
        )
    if (
        request.team_role_id is None
        or request.team_role_name is None
        or str(request.team_role_name) != team_name
    ):
        raise TeamTierRoleError(
            f':warning: No role matching **{team_name}**. It must have a role '
            'to edit team properties.'
        )
    if (
        request.expected_team_id is not None
        and int(request.expected_team_id) != int(team.id)
    ):
        raise TeamAttributeConflictError(
            f'Team {team_name} changed before this update was applied.'
        )


def _resolve_house(request: TeamAttributeMutationRequest):
    """Resolve the selected global House inside the worker transaction."""

    if request.house is None or not str(request.house).strip():
        raise TeamAttributeValidationError(
            'A House name is required, or use `clear` to remove the affiliation.'
        )
    try:
        return models.House.get_or_except(house_name=str(request.house).strip())
    except exceptions.TooManyMatches as exc:
        raise TeamAttributeLookupError(str(exc)) from exc
    except exceptions.NoMatches as exc:
        raise TeamAttributeLookupError(str(exc)) from exc


def _validate_house_preconditions(request, team) -> None:
    """Preserve legacy house-edit archive and exact-team-role gates."""

    team_name = str(team.name)
    if bool(getattr(team, 'is_archived', False)):
        raise TeamAttributeValidationError(
            f'Team **{team_name}** is **archived**. If it really needs to be '
            'unarchived, ask the bot owner.'
        )
    if (
        request.team_role_id is None
        or request.team_role_name is None
        or str(request.team_role_name) != team_name
    ):
        raise TeamTierRoleError(
            f':warning: No role matching **{team_name}**. It must have a role '
            'to edit team properties.'
        )
    if (
        request.expected_team_id is not None
        and int(request.expected_team_id) != int(team.id)
    ):
        raise TeamAttributeConflictError(
            f'Team {team_name} changed before this update was applied.'
        )


def _reconcile_persisted_team_members(
    request: TeamAttributeMutationRequest,
    team,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Apply the legacy team/preference update for captured role members.

    Discord member objects never cross into this worker.  The event-loop
    preflight captures only the exact team-role member IDs; this function then
    performs the corresponding Peewee work on the worker-local connection and
    transaction.  A missing player retains the legacy warning-and-continue
    behavior, while database failures propagate and roll back the tier/audit.
    """

    if not request.team_member_ids:
        return (), ()

    updated_member_ids = []
    failed_member_ids = []
    for member_id in request.team_member_ids:
        member_id = int(member_id)
        try:
            player = models.Player.get_or_except(
                player_string=member_id,
                guild_id=int(request.guild_id),
            )
        except exceptions.NoSingleMatch as exc:
            logger.warning(
                'Could not load Player %s for team %s %s reconciliation: %s',
                member_id,
                team.id,
                'house' if request.attribute == TEAM_ATTRIBUTE_HOUSE else 'tier',
                exc,
            )
            failed_member_ids.append(member_id)
            continue

        player.team = team
        player.save()
        models.PlayerHousePreference.clear_preferences(player.id)
        updated_member_ids.append(member_id)

    return tuple(updated_member_ids), tuple(failed_member_ids)


def read_team_attribute(
    request: TeamAttributeReadRequest,
) -> TeamAttributeReadResult:
    """Load one team attribute using a worker-local connection."""

    with models.db.connection_context():
        attribute = _validate_access(request)
        team = _resolve_team(request, include_hidden=request.include_hidden)
        return _snapshot(
            team,
            guild_id=request.guild_id,
            attribute=attribute,
        )


def _validate_name(value: str | None) -> str:
    if value is None:
        raise TeamAttributeValidationError(
            'A new team name is required; team names cannot be cleared.'
        )
    value = str(value)
    if len(value) < 5:
        raise TeamAttributeValidationError(
            'New team name needs to be at least 5 characters long. Be sure to '
            'enclose the name "In Quotation Marks" if it includes spaces.'
        )
    return value


def _validate_server(request: TeamAttributeMutationRequest) -> int | None:
    if request.clear:
        if request.server_id is not None:
            raise TeamAttributeValidationError(
                'Choose either a server ID or `clear`, not both.'
            )
        return None
    if request.server_id is None or isinstance(request.server_id, bool):
        raise TeamAttributeValidationError(
            'A numeric external server ID is required, or use `clear`.'
        )
    try:
        return int(request.server_id)
    except (TypeError, ValueError) as exc:
        raise TeamAttributeValidationError(
            'A numeric external server ID is required, or use `clear`.'
        ) from exc


def _validate_tier(value: str | None) -> tuple[int, str]:
    if value is None:
        raise TeamAttributeValidationError(
            'A configured tier name or number is required.'
        )
    try:
        number, name = settings.tier_lookup(str(value))
    except (TypeError, ValueError, exceptions.NoMatches) as exc:
        raise TeamAttributeValidationError(
            f'Could not set team tier based on "{value}". You can use a '
            'name ("gold") or tier number ("2"). '
        ) from exc
    return int(number), str(name)


def _duplicate_team_name(team, *, guild_id: int, name: str) -> bool:
    try:
        query = models.Team.select().where(
            (
                models.Team.guild_id
                == team_record_scope.persistent_team_guild_id(guild_id)
            )
            & (models.Team.name == name)
            & (models.Team.id != int(team.id))
        )
        return bool(query.exists())
    except AttributeError:
        # Small worker doubles may not provide Peewee field descriptors.  The
        # database unique boundary remains authoritative at save() time.
        return False


def _audit_message(request, *, result: TeamAttributeMutationResult) -> str:
    invocation_note = (
        f' ({request.invoked_with})'
        if str(request.invoked_with).startswith('/')
        else ''
    )
    if result.attribute == TEAM_ATTRIBUTE_NAME:
        change = (
            f'set the renamed team ID {result.team_id} from '
            f'{result.old_team_name} to {result.new_team_name}'
        )
    elif result.attribute == TEAM_ATTRIBUTE_SERVER:
        change = (
            f'set the external server of Team {result.team_name} to '
            f'{result.value!r} from {result.old_value!r}'
        )
    elif result.attribute == TEAM_ATTRIBUTE_HOUSE:
        change = (
            f'set the House affiliation of Team {result.team_name} to '
            f'{result.house_name or "None"} from '
            f'{result.old_house_name or "None"}'
        )
    else:
        change = (
            f'set the league tier of Team {result.team_name} to '
            f'{result.new_tier} from {result.old_tier}'
        )
    return f'{request.requester_description} {change}.{invocation_note}'


def set_team_attribute(
    request: TeamAttributeMutationRequest,
) -> TeamAttributeMutationResult:
    """Validate, mutate, and audit one team attribute atomically."""

    with models.db.connection_context():
        with models.db.atomic():
            attribute = _validate_access(request)
            team = _resolve_team(
                request,
                include_hidden=(
                    False
                    if attribute == TEAM_ATTRIBUTE_HOUSE
                    else request.include_hidden
                ),
            )
            if (
                request.expected_team_id is not None
                and int(request.expected_team_id) != int(team.id)
            ):
                raise TeamAttributeConflictError(
                    f'Team {team.name} changed before this update was applied.'
                )
            if attribute == TEAM_ATTRIBUTE_TIER:
                _validate_tier_preconditions(request, team)
            elif attribute == TEAM_ATTRIBUTE_HOUSE:
                _validate_house_preconditions(request, team)

            before = _snapshot(
                team,
                guild_id=request.guild_id,
                attribute=attribute,
            )
            if (
                request.expected_value_present
                and before.value != request.expected_value
            ):
                raise TeamAttributeConflictError(
                    f'Team {before.team_name} changed before this update was '
                    'applied.'
                )

            if attribute == TEAM_ATTRIBUTE_NAME:
                new_value: str | int | None = _validate_name(request.name)
                if _duplicate_team_name(
                    team,
                    guild_id=request.guild_id,
                    name=str(new_value),
                ):
                    raise TeamAttributeConflictError(
                        f'A team named "{new_value}" already exists on this '
                        'server.'
                    )
                team.name = str(new_value)
            elif attribute == TEAM_ATTRIBUTE_SERVER:
                new_value = _validate_server(request)
                team.external_server = new_value
            elif attribute == TEAM_ATTRIBUTE_HOUSE:
                if request.clear and request.house is not None:
                    raise TeamAttributeValidationError(
                        'Choose either a House or `clear`, not both.'
                    )
                selected_house = None if request.clear else _resolve_house(request)
                new_value = (
                    None
                    if selected_house is None
                    else str(selected_house.name)
                )
                team.house = selected_house
            else:
                new_tier, _ = _validate_tier(request.tier)
                new_value = new_tier
                team.league_tier = new_tier

            team.save()
            persisted_member_ids = ()
            persisted_member_failures = ()
            if attribute in {TEAM_ATTRIBUTE_TIER, TEAM_ATTRIBUTE_HOUSE}:
                (
                    persisted_member_ids,
                    persisted_member_failures,
                ) = _reconcile_persisted_team_members(request, team)
            after = _snapshot(
                team,
                guild_id=request.guild_id,
                attribute=attribute,
            )
            result = TeamAttributeMutationResult(
                guild_id=after.guild_id,
                team_id=after.team_id,
                attribute=attribute,
                team_name=after.team_name,
                old_team_name=before.team_name,
                new_team_name=after.team_name,
                old_value=before.value,
                value=after.value,
                old_tier=before.league_tier,
                new_tier=after.league_tier,
                old_tier_name=before.tier_name,
                new_tier_name=after.tier_name,
                old_house_name=before.house_name,
                house_name=after.house_name,
                team_role_id=(
                    int(request.team_role_id)
                    if request.team_role_id is not None
                    else None
                ),
                house_role_names=after.house_role_names,
                cleared=bool(request.clear),
                native=bool(request.native),
                persisted_member_ids=persisted_member_ids,
                persisted_member_failures=persisted_member_failures,
            )
            models.GameLog.write(
                guild_id=int(request.guild_id),
                message=_audit_message(request, result=result),
            )
            return result


async def run_team_attribute_read(
    request: TeamAttributeReadRequest,
) -> TeamAttributeReadResult:
    return await team_emoji_workers.run_bounded_team_worker(
        read_team_attribute,
        request,
        drain_on_cancel=False,
    )


async def run_team_attribute_mutation(
    request: TeamAttributeMutationRequest,
) -> TeamAttributeMutationResult:
    return await team_emoji_workers.run_bounded_team_worker(
        set_team_attribute,
        request,
        drain_on_cancel=True,
    )
