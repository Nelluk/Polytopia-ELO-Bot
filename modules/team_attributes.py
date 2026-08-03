"""Shared adapters for focused team name, server, and tier workflows."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

import settings
from modules import exceptions, team_emoji, team_emoji_workers
from modules import team_attributes_workers as workers
from modules import utilities


logger = logging.getLogger('polybot.' + __name__)

TeamAttributeActor = team_emoji.TeamEmojiActor
capture_actor = team_emoji.capture_actor

TEAM_TIER_CHOICES = [
    discord.app_commands.Choice(
        name=f'{number}: {name}',
        value=str(number),
    )
    for number, name in settings.league_tiers
]


@dataclass(frozen=True)
class TierPreflight:
    """Primitive result of a tier read plus exact role validation."""

    current: workers.TeamAttributeReadResult
    team_role_id: int
    team_role_name: str


@dataclass(frozen=True)
class TierRoleReconciliation:
    """Post-commit Discord role reconciliation outcome."""

    team_id: int
    attempted: int
    updated: int
    failed_member_ids: tuple[int, ...] = ()
    missing_role_names: tuple[str, ...] = ()
    team_role_missing: bool = False

    @property
    def warning(self) -> str | None:
        details = []
        if self.team_role_missing:
            details.append('the exact team role was no longer available')
        if self.missing_role_names:
            details.append(
                'missing managed role(s): ' + ', '.join(self.missing_role_names)
            )
        if self.failed_member_ids:
            details.append(
                'failed member IDs: '
                + ', '.join(str(member_id) for member_id in self.failed_member_ids)
            )
        if not details:
            return None
        return (
            ':warning: The team tier was saved, but league-role '
            'reconciliation was partial: ' + '; '.join(details) + '.'
        )


def _team_enabled(guild_id: int) -> bool:
    try:
        return bool(settings.guild_setting(int(guild_id), 'allow_teams'))
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return False


def _requester_is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def _league_scope(guild_id: int) -> bool:
    try:
        return int(guild_id) in {
            int(settings.server_ids['polychampions']),
            int(settings.server_ids['test']),
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _include_hidden(attribute: str) -> bool:
    return attribute in {
        workers.TEAM_ATTRIBUTE_NAME,
        workers.TEAM_ATTRIBUTE_SERVER,
    }


def build_read_request(
    *,
    member,
    guild_id: int,
    attribute: str,
    team_lookup: str | None = None,
    invoked_with: str = '/team',
) -> workers.TeamAttributeReadRequest:
    """Capture only primitive/member-safe values for a worker read."""

    return workers.TeamAttributeReadRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        team_enabled=_team_enabled(guild_id),
        league_scope=_league_scope(guild_id),
        team_lookup=(str(team_lookup) if team_lookup is not None else None),
        attribute=str(attribute),
        requester_description=team_emoji.capture_actor(member).identity,
        include_hidden=_include_hidden(str(attribute)),
        invoked_with=str(invoked_with),
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    attribute: str,
    team_lookup: str | None = None,
    name: str | None = None,
    server_id: int | None = None,
    tier: str | None = None,
    clear: bool = False,
    expected_team_id: int | None = None,
    expected_value: str | int | None = None,
    expected_value_present: bool = False,
    team_role_id: int | None = None,
    team_role_name: str | None = None,
    native: bool = True,
    invoked_with: str = '/team',
    prefix: str = '$',
) -> workers.TeamAttributeMutationRequest:
    """Capture Discord/member values into an immutable mutation request."""

    return workers.TeamAttributeMutationRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        team_enabled=_team_enabled(guild_id),
        league_scope=_league_scope(guild_id),
        team_lookup=(str(team_lookup) if team_lookup is not None else None),
        attribute=str(attribute),
        name=(str(name) if name is not None else None),
        server_id=(int(server_id) if server_id is not None else None),
        tier=(str(tier) if tier is not None else None),
        clear=bool(clear),
        requester_description=team_emoji.capture_actor(member).identity,
        include_hidden=_include_hidden(str(attribute)),
        expected_team_id=(
            int(expected_team_id) if expected_team_id is not None else None
        ),
        expected_value=expected_value,
        expected_value_present=bool(expected_value_present),
        team_role_id=(int(team_role_id) if team_role_id is not None else None),
        team_role_name=(
            str(team_role_name) if team_role_name is not None else None
        ),
        native=bool(native),
        invoked_with=str(invoked_with),
        prefix=str(prefix),
    )


def native_access_error(member, guild_id: int, attribute: str) -> str | None:
    """Return a private pre-defer denial while retaining legacy gates."""

    attribute = str(attribute)
    if attribute != workers.TEAM_ATTRIBUTE_TIER and not _team_enabled(guild_id):
        return 'Teams are not enabled on this server.'
    if not _requester_is_mod(member):
        return 'You do not have permission to manage team attributes.'
    if attribute == workers.TEAM_ATTRIBUTE_TIER and not _league_scope(guild_id):
        return 'Team tiers can only be managed in the PolyChampions league server.'
    return None


async def run_read(request: workers.TeamAttributeReadRequest):
    return await workers.run_team_attribute_read(request)


async def run_mutation(request: workers.TeamAttributeMutationRequest):
    return await workers.run_team_attribute_mutation(request)


def _exact_team_role(guild, team_name: str):
    role = utilities.guild_role_by_name(guild, name=team_name, allow_partial=False)
    if role is None:
        raise workers.TeamTierRoleError(
            f':warning: No role matching **{team_name}**. It must have a role '
            'to edit team properties.'
        )
    return role


async def run_tier_preflight(
    *,
    member,
    guild,
    team_lookup: str | None,
    invoked_with: str,
) -> TierPreflight:
    request = build_read_request(
        member=member,
        guild_id=guild.id,
        attribute=workers.TEAM_ATTRIBUTE_TIER,
        team_lookup=team_lookup,
        invoked_with=invoked_with,
    )
    current = await run_read(request)
    if current.is_archived:
        raise workers.TeamAttributeValidationError(
            f'Team **{current.team_name}** is **archived**. If it really '
            'needs to be unarchived, ask the bot owner.'
        )
    if current.house_name is None:
        raise workers.TeamAttributeValidationError(
            f'Team **{current.team_name}** does not have a House affiliation. '
            'Set one with `$team_house` first.'
        )
    role = _exact_team_role(guild, current.team_name)
    return TierPreflight(
        current=current,
        team_role_id=int(role.id),
        team_role_name=str(role.name),
    )


async def autocomplete_teams(
    interaction: discord.Interaction,
    current: str,
) -> list[discord.app_commands.Choice[str]]:
    """Guild-scoped, bounded autocomplete shared by every `/team` attribute."""

    guild_id = getattr(getattr(interaction, 'guild', None), 'id', None)
    if guild_id is None:
        return []
    request = team_emoji_workers.TeamAutocompleteRequest(
        guild_id=int(guild_id),
        current=str(current or ''),
        limit=25,
    )
    try:
        results = await team_emoji_workers.run_team_autocomplete(request)
    except Exception:
        logger.exception('Team autocomplete failed for guild %s', guild_id)
        return []
    return [
        discord.app_commands.Choice(
            name=result.team_name[:100],
            value=result.team_name[:100],
        )
        for result in results[:25]
    ]


def _display(value) -> str:
    if value is None or value == '':
        return 'None'
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def _tier_display(result) -> str:
    if result.league_tier is None:
        return 'None'
    if result.tier_name:
        return f'{_display(result.tier_name)} ({result.league_tier})'
    return str(result.league_tier)


def read_message(
    result: workers.TeamAttributeReadResult,
    *,
    actor: TeamAttributeActor | None = None,
) -> str:
    if result.attribute == workers.TEAM_ATTRIBUTE_NAME:
        value = f'**{_display(result.team_name)}**'
        label = 'name'
    elif result.attribute == workers.TEAM_ATTRIBUTE_SERVER:
        value = f'`{_display(result.external_server)}`'
        label = 'external server ID'
    else:
        value = _tier_display(result)
        label = 'tier'
    message = (
        f'Current {label} for team **{_display(result.team_name)}**: {value}'
    )
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    return message


def legacy_server_read_message(result) -> str:
    return (
        f'Team **{result.team_name}** has been assigned an external server of '
        f'`{result.external_server}`.'
    )


def legacy_mutation_message(result: workers.TeamAttributeMutationResult) -> str:
    if result.attribute == workers.TEAM_ATTRIBUTE_NAME:
        return (
            f'Team **{result.old_team_name}** has been renamed to '
            f'**{result.new_team_name}**.'
        )
    if result.attribute == workers.TEAM_ATTRIBUTE_SERVER:
        old_server = result.old_value if result.old_value is not None else 'None'
        new_server = result.value if result.value is not None else 'None'
        return (
            f'Team **{result.team_name}** has been assigned an external server '
            f'of `{new_server}`. Previous value was `{old_server}`.'
        )
    old_tier = (
        str(result.old_tier) if result.old_tier is not None else 'NONE'
    )
    return (
        f'Changed league tier of team  **{result.team_name}** to '
        f'{result.new_tier_name} ({result.new_tier}). Previous tier was '
        f'{old_tier}. Team members have had their tier roles refreshed.'
    )


def native_mutation_message(
    result: workers.TeamAttributeMutationResult,
    *,
    actor: TeamAttributeActor,
) -> str:
    if result.attribute == workers.TEAM_ATTRIBUTE_NAME:
        return (
            f'{actor.label} renamed Team **{_display(result.old_team_name)}** '
            f'to **{_display(result.new_team_name)}**.\n'
            f':warning: Discord roles are not renamed automatically. Rename '
            f'the role **{_display(result.old_team_name)}** to exactly '
            f'**{_display(result.new_team_name)}** before team membership can '
            'be detected.'
        )
    if result.attribute == workers.TEAM_ATTRIBUTE_SERVER:
        if result.cleared:
            return (
                f'{actor.label} cleared the external server for Team '
                f'**{_display(result.team_name)}**. Previous value was '
                f'`{_display(result.old_value)}`.'
            )
        return (
            f'{actor.label} set the external server for Team '
            f'**{_display(result.team_name)}** to `{result.value}`. Previous '
            f'value was `{_display(result.old_value)}`.'
        )
    old_tier = _display(result.old_tier_name or result.old_tier or 'None')
    new_tier = _display(result.new_tier_name or result.new_tier)
    return (
        f'{actor.label} changed Team **{_display(result.team_name)}** from '
        f'**{old_tier}** to **{new_tier}** ({result.new_tier}).'
    )


async def publish_mutation_result(
    result: workers.TeamAttributeMutationResult,
    *,
    send,
    actor: TeamAttributeActor | None = None,
    reconciliation: TierRoleReconciliation | None = None,
) -> None:
    message = (
        legacy_mutation_message(result)
        if actor is None
        else native_mutation_message(result, actor=actor)
    )
    try:
        await send(message)
    except Exception:
        logger.exception(
            'Committed team-%s mutation for team %s could not publish',
            result.attribute,
            result.team_id,
        )
        try:
            await send(
                f':warning: Team **{_display(result.team_name)}** '
                f'{result.attribute} was saved, but the public success message '
                'could not be sent. An operator must reconcile the team '
                'presentation.'
            )
        except Exception:
            logger.exception('Committed team mutation warning could not be sent')
    if reconciliation is not None and reconciliation.warning:
        try:
            await send(reconciliation.warning)
        except Exception:
            logger.exception(
                'Team tier reconciliation warning could not be sent for %s',
                result.team_id,
            )


def _role_key(role):
    return int(getattr(role, 'id', 0)) if role is not None else None


async def reconcile_tier_roles(
    guild,
    result: workers.TeamAttributeMutationResult,
) -> TierRoleReconciliation:
    """Refresh legacy managed roles after a committed tier transaction.

    This function intentionally accepts/uses Discord objects only on the
    event-loop side.  It never opens a database connection and never changes
    the already-committed team tier when an individual Discord edit fails.
    """

    team_role = None
    get_role = getattr(guild, 'get_role', None)
    if callable(get_role) and result.team_role_id is not None:
        team_role = get_role(int(result.team_role_id))
    if team_role is None:
        team_role = utilities.guild_role_by_name(
            guild,
            name=result.team_name,
            allow_partial=False,
        )
    if team_role is None or str(getattr(team_role, 'name', '')) != result.team_name:
        return TierRoleReconciliation(
            team_id=result.team_id,
            attempted=0,
            updated=0,
            team_role_missing=True,
        )

    roles = tuple(getattr(guild, 'roles', ()) or ())
    tier_roles = tuple(
        discord.utils.get(roles, name=f'{tier_name} Player')
        for _, tier_name in settings.league_tiers
    )
    house_roles = tuple(
        discord.utils.get(roles, name=house_name)
        for house_name in result.house_role_names
    )
    league_role = discord.utils.get(roles, name='League Member')
    missing_names = tuple(
        role_name
        for role_name, role in [
            *[
                (f'{tier_name} Player', tier_role)
                for (_, tier_name), tier_role in zip(
                    settings.league_tiers,
                    tier_roles,
                )
            ],
            *[
                (house_name, house_role)
                for house_name, house_role in zip(
                    result.house_role_names,
                    house_roles,
                )
            ],
            ('League Member', league_role),
        ]
        if role is None
    )

    target_house_role = (
        discord.utils.get(roles, name=result.house_name)
        if result.house_name
        else None
    )
    target_tier_role = (
        discord.utils.get(
            roles,
            name=(
                f'{result.new_tier_name} Player'
                if result.new_tier_name
                else ''
            ),
        )
        if result.new_tier is not None
        else None
    )
    target_roles = tuple(
        role for role in (target_house_role, target_tier_role, league_role)
        if role is not None
    )
    managed_roles = tuple(
        role for role in (*tier_roles, *house_roles, league_role)
        if role is not None
    )
    managed_role_keys = {
        _role_key(role)
        for role in managed_roles
    }
    managed_role_names = {
        str(getattr(role, 'name', ''))
        for role in managed_roles
    }

    failed_member_ids = []
    updated = 0
    members = tuple(getattr(team_role, 'members', ()) or ())
    for member in members:
        member_id = int(getattr(member, 'id', 0))
        try:
            current_roles = list(getattr(member, 'roles', ()) or ())
            current_role_keys = {_role_key(role) for role in current_roles}
            team_role_present = _role_key(team_role) in current_role_keys
            new_roles = [
                role for role in current_roles
                if (
                    _role_key(role) not in managed_role_keys
                    and str(getattr(role, 'name', ''))
                    not in managed_role_names
                )
            ]
            if team_role_present:
                new_roles = [
                    role for role in new_roles
                    if not str(getattr(role, 'name', '')).startswith('Prefers ')
                ]
            for role in target_roles:
                if (
                    _role_key(role) not in {
                        _role_key(item) for item in new_roles
                    }
                    and str(getattr(role, 'name', '')) not in {
                        str(getattr(item, 'name', '')) for item in new_roles
                    }
                ):
                    new_roles.append(role)
            await member.edit(
                roles=new_roles,
                reason='Refreshing member\'s league roles',
            )
            updated += 1
        except Exception:
            logger.exception(
                'Could not reconcile league roles for team %s member %s',
                result.team_id,
                member_id,
            )
            failed_member_ids.append(member_id)

    return TierRoleReconciliation(
        team_id=result.team_id,
        attempted=len(members),
        updated=updated,
        failed_member_ids=tuple(failed_member_ids),
        missing_role_names=missing_names,
    )
