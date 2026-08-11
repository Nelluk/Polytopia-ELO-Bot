"""Discord boundary and reconciliation policy for league inactivity marking."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

import settings
from modules import league_inactivity_workers as workers, league_user_commands


logger = logging.getLogger('polybot.' + __name__)

PROTECTED_LEADERSHIP_ROLE_NAMES = (
    'Team Recruiter',
    'Team Leader',
    'Team Co-Leader',
    'PrOPhEt oF MiDJiWaN',
)


@dataclass(frozen=True)
class InactivityConfirmationOutcome:
    state: str
    preview: workers.InactivityPreviewResult
    private_message: str
    succeeded_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in {'applied', 'reconciliation'}


def access_error(member, guild_id: int) -> str | None:
    if not league_user_commands.league_scope(int(guild_id)):
        return 'This command is available only in the configured league server.'
    if not settings.is_mod(member):
        return 'Marking inactive members requires Mod access.'
    return None


def _protected_role_names(guild) -> tuple[str, ...]:
    configured_ids = settings.configured_role_ids(
        int(guild.id),
        'mod_roles',
    )
    if configured_ids:
        configured_mod_roles = tuple(
            str(role.name)
            for role_id in configured_ids
            if (role := guild.get_role(role_id)) is not None
        )
    else:
        configured_mod_roles = tuple(
            str(name)
            for name in (
                settings.guild_setting(int(guild.id), 'mod_roles') or ()
            )
            if str(name)
        )
    return tuple(dict.fromkeys(
        configured_mod_roles + PROTECTED_LEADERSHIP_ROLE_NAMES
    ))


def _display_name(member) -> str:
    return str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{int(member.id)}'
    )


def capture_request(*, member, guild) -> workers.InactivityPreviewRequest:
    error = access_error(member, guild.id)
    if error:
        raise workers.LeagueInactivityPermissionError(error)

    inactive_role = settings.resolve_configured_role(guild, 'inactive_role')
    if inactive_role is None:
        raise workers.LeagueInactivityError(
            'The configured Inactive role could not be resolved.'
        )

    protected_names = _protected_role_names(guild)
    live_role_names = {
        str(role.name) for role in getattr(guild, 'roles', ())
    }
    missing_names = tuple(
        name for name in protected_names if name not in live_role_names
    )
    current_members = tuple(getattr(guild, 'members', ()))
    if len(current_members) > workers.MAX_GUILD_MEMBER_SNAPSHOTS:
        raise workers.LeagueInactivityError(
            f'This server has more than the safe '
            f'{workers.MAX_GUILD_MEMBER_SNAPSHOTS:,}-member preview limit.'
        )
    snapshots = []
    for current in current_members:
        joined_at = getattr(current, 'joined_at', None)
        snapshots.append(workers.InactivityMemberSnapshot(
            member_id=int(current.id),
            display_name=_display_name(current),
            joined_timestamp=(
                float(joined_at.timestamp()) if joined_at is not None else None
            ),
            role_ids=tuple(
                int(role.id) for role in getattr(current, 'roles', ())
            ),
            role_names=tuple(
                str(role.name) for role in getattr(current, 'roles', ())
            ),
            is_bot=bool(getattr(current, 'bot', False)),
            is_owner=int(current.id) == int(settings.owner_id),
        ))

    return workers.InactivityPreviewRequest(
        guild_id=int(guild.id),
        requester_id=int(member.id),
        requester_is_mod=True,
        league_scope=True,
        now_timestamp=float(discord.utils.utcnow().timestamp()),
        inactive_role_id=int(inactive_role.id),
        inactive_role_name=str(inactive_role.name),
        protected_role_names=protected_names,
        missing_protected_role_names=missing_names,
        members=tuple(snapshots),
    )


async def load_preview(*, member, guild) -> workers.InactivityPreviewResult:
    return await workers.run_inactivity_preview(
        capture_request(member=member, guild=guild)
    )


def _safe_name(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def _private_details(
    *,
    failed: list[tuple[int, str, str]],
    skipped: list[tuple[int, str, str]],
) -> str:
    lines = []
    if failed:
        lines.append(
            '**Failed:** ' + ', '.join(
                f'{_safe_name(name)} (`{member_id}`): {_safe_name(reason)}'
                for member_id, name, reason in failed[:12]
            )
        )
    if skipped:
        lines.append(
            '**Skipped after refresh:** ' + ', '.join(
                f'{_safe_name(name)} (`{member_id}`): {_safe_name(reason)}'
                for member_id, name, reason in skipped[:12]
            )
        )
    return '\n'.join(lines)


def _public_result(
    *,
    actor,
    role_name: str,
    succeeded_count: int,
    failed_count: int,
    skipped_count: int,
    deferred_count: int,
) -> str:
    identity = league_user_commands.capture_actor(actor)
    summary = (
        f'{identity.label} completed league inactivity maintenance: applied '
        f'**{_safe_name(role_name)}** to **{succeeded_count}** member(s); '
        f'**{failed_count}** failed, **{skipped_count}** were skipped after '
        'revalidation.'
    )
    if deferred_count:
        summary += (
            f' **{deferred_count}** remain beyond this run\'s '
            f'{workers.MAX_ACTION_CANDIDATES}-member safety limit.'
        )
    return summary


async def confirm_and_publish(
    interaction: discord.Interaction,
    previous: workers.InactivityPreviewResult,
) -> InactivityConfirmationOutcome:
    guild = interaction.guild
    if guild is None:
        raise workers.LeagueInactivityPermissionError(
            'Inactivity maintenance requires a server.'
        )
    error = access_error(interaction.user, guild.id)
    if error:
        raise workers.LeagueInactivityPermissionError(error)

    refreshed = await load_preview(member=interaction.user, guild=guild)
    if refreshed.candidate_ids != previous.candidate_ids:
        return InactivityConfirmationOutcome(
            state='refreshed',
            preview=refreshed,
            private_message=(
                'Guild activity or roles changed since this preview. The '
                'candidate list was refreshed; review it and confirm again.'
            ),
        )

    inactive_role = guild.get_role(refreshed.inactive_role_id)
    if (
        inactive_role is None
        or str(inactive_role.name) != refreshed.inactive_role_name
    ):
        raise workers.LeagueInactivityError(
            'The configured Inactive role changed after preview. Run the '
            'command again.'
        )

    protected_names = set(refreshed.protected_role_names)
    succeeded = []
    failed: list[tuple[int, str, str]] = []
    skipped: list[tuple[int, str, str]] = []
    for candidate in refreshed.action_candidates:
        try:
            current = guild.get_member(candidate.member_id)
            if current is None:
                skipped.append((
                    candidate.member_id,
                    candidate.display_name,
                    'member left the server',
                ))
                continue
            current_roles = tuple(getattr(current, 'roles', ()))
            current_role_names = {str(role.name) for role in current_roles}
            if (
                bool(getattr(current, 'bot', False))
                or int(current.id) == int(settings.owner_id)
            ):
                skipped.append((
                    current.id,
                    _display_name(current),
                    'protected account',
                ))
                continue
            if inactive_role in current_roles:
                skipped.append((
                    current.id,
                    _display_name(current),
                    'already inactive',
                ))
                continue
            if protected_names.intersection(current_role_names):
                skipped.append((
                    current.id,
                    _display_name(current),
                    'protected role',
                ))
                continue
            await current.add_roles(
                inactive_role,
                reason=(
                    f'Marked inactive by {interaction.user} '
                    f'({interaction.user.id}) after refreshed preview'
                ),
            )
        except discord.DiscordException as exc:
            logger.warning(
                'Could not apply Inactive role in guild %s to member %s: %s',
                guild.id,
                candidate.member_id,
                exc,
            )
            failed.append((
                candidate.member_id,
                candidate.display_name,
                'Discord role update failed',
            ))
            continue
        except Exception as exc:
            logger.exception(
                'Unexpected Inactive role failure in guild %s for member %s',
                guild.id,
                candidate.member_id,
            )
            failed.append((
                candidate.member_id,
                candidate.display_name,
                type(exc).__name__,
            ))
            continue
        succeeded.append((current.id, _display_name(current)))

    details = _private_details(failed=failed, skipped=skipped)
    if not succeeded:
        return InactivityConfirmationOutcome(
            state='retryable',
            preview=refreshed,
            private_message=(
                'No Inactive roles were applied. Review the failures and try '
                'again or rerun the command.'
                + (f'\n{details}' if details else '')
            ),
            failed_count=len(failed),
            skipped_count=len(skipped),
        )

    public_message = _public_result(
        actor=interaction.user,
        role_name=inactive_role.name,
        succeeded_count=len(succeeded),
        failed_count=len(failed),
        skipped_count=len(skipped),
        deferred_count=refreshed.deferred_candidate_count,
    )
    channel = getattr(interaction, 'channel', None)
    sender = getattr(channel, 'send', None)
    if not callable(sender):
        return InactivityConfirmationOutcome(
            state='reconciliation',
            preview=refreshed,
            private_message=(
                f'{len(succeeded)} role change(s) succeeded, but no public '
                'destination was available. Do not retry the completed '
                'members; staff should reconcile the announcement.'
                + (f'\n{details}' if details else '')
            ),
            succeeded_count=len(succeeded),
            failed_count=len(failed),
            skipped_count=len(skipped),
        )
    try:
        await sender(
            public_message,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        logger.exception(
            'Committed inactivity role changes could not publish in guild %s',
            guild.id,
        )
        return InactivityConfirmationOutcome(
            state='reconciliation',
            preview=refreshed,
            private_message=(
                f'{len(succeeded)} role change(s) succeeded, but the public '
                'result failed. Do not retry the completed members; staff '
                'should reconcile the announcement.'
                + (f'\n{details}' if details else '')
            ),
            succeeded_count=len(succeeded),
            failed_count=len(failed),
            skipped_count=len(skipped),
        )

    return InactivityConfirmationOutcome(
        state='applied',
        preview=refreshed,
        private_message=(
            'Inactivity maintenance completed and the attributed public '
            'summary was posted.'
            + (f'\n{details}' if details else '')
        ),
        succeeded_count=len(succeeded),
        failed_count=len(failed),
        skipped_count=len(skipped),
    )
