"""Discord boundary for previewed inactive-member removal."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

import discord

import settings
from modules import league_inactive_kick_workers as workers
from modules import league_user_commands
from modules import models


logger = logging.getLogger('polybot.' + __name__)

STARTER_ROLE_NAMES = (
    'Newbie',
    'ELO Rookie',
    'ELO Player',
    'The Novas',
    'Nova Red',
    'Nova Blue',
    'Nova Grad',
)
PROTECTED_LEADERSHIP_ROLE_NAMES = (
    'House Leader',
    'House Co-Leader',
    'Team Recruiter',
    'Team Leader',
    'Team Co-Leader',
    'PrOPhEt oF MiDJiWaN',
)
KICK_DM = (
    'You have been removed from PolyChampions during a reviewed cleanup of '
    'inactive members. This is not disciplinary. You are welcome to rejoin: '
    'https://discord.gg/YcvBheS'
)


@dataclass(frozen=True)
class InactiveKickConfirmationOutcome:
    state: str
    preview: workers.InactiveKickPreviewResult
    private_message: str
    kicked_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    dm_failed_count: int = 0
    audit_failed_count: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in {'complete', 'reconciliation'}


def access_error(member, guild_id: int) -> str | None:
    if not league_user_commands.league_scope(int(guild_id)):
        return 'This command is available only in the configured league server.'
    if not settings.is_mod(member):
        return 'Removing inactive members requires Mod access.'
    return None


def _role_names(setting_value) -> tuple[str, ...]:
    return tuple(str(name) for name in (setting_value or ()) if str(name))


def protected_role_names(guild) -> tuple[str, ...]:
    configured = []
    for setting_name in ('mod_roles', 'helper_roles'):
        role_ids = settings.configured_role_ids(int(guild.id), setting_name)
        if role_ids:
            configured.extend(
                str(role.name)
                for role_id in role_ids
                if (role := guild.get_role(role_id)) is not None
            )
        else:
            configured.extend(_role_names(
                settings.guild_setting(int(guild.id), setting_name)
            ))
    names = tuple(configured) + PROTECTED_LEADERSHIP_ROLE_NAMES
    return tuple(dict.fromkeys(names))


def _display_name(member) -> str:
    return str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{int(member.id)}'
    )


def capture_request(*, member, guild) -> workers.InactiveKickPreviewRequest:
    error = access_error(member, guild.id)
    if error:
        raise workers.InactiveKickPermissionError(error)
    inactive_role = settings.resolve_configured_role(guild, 'inactive_role')
    if inactive_role is None:
        raise workers.InactiveKickError(
            'The configured Inactive role could not be resolved.'
        )
    inactive_members = tuple(getattr(inactive_role, 'members', ()))
    if not inactive_members:
        inactive_members = tuple(
            current
            for current in getattr(guild, 'members', ())
            if inactive_role in getattr(current, 'roles', ())
        )
    if len(inactive_members) > workers.MAX_MEMBER_SNAPSHOTS:
        raise workers.InactiveKickError(
            f'The Inactive role has more than the safe '
            f'{workers.MAX_MEMBER_SNAPSHOTS:,}-member preview limit.'
        )

    snapshots = []
    for current in inactive_members:
        joined_at = getattr(current, 'joined_at', None)
        snapshots.append(workers.KickMemberSnapshot(
            member_id=int(current.id),
            display_name=_display_name(current),
            joined_timestamp=(
                float(joined_at.timestamp()) if joined_at is not None else None
            ),
            roles=tuple(
                workers.KickRoleSnapshot(
                    role_id=int(role.id),
                    name=str(role.name),
                    managed=bool(getattr(role, 'managed', False)),
                )
                for role in getattr(current, 'roles', ())
            ),
            is_bot=bool(getattr(current, 'bot', False)),
            is_owner=int(current.id) == int(settings.owner_id),
        ))

    return workers.InactiveKickPreviewRequest(
        guild_id=int(guild.id),
        requester_id=int(member.id),
        requester_is_mod=True,
        league_scope=True,
        now_timestamp=float(discord.utils.utcnow().timestamp()),
        inactive_role_id=int(inactive_role.id),
        inactive_role_name=str(inactive_role.name),
        starter_role_names=STARTER_ROLE_NAMES,
        protected_role_names=protected_role_names(guild),
        members=tuple(snapshots),
    )


async def load_preview(*, member, guild) -> workers.InactiveKickPreviewResult:
    return await workers.run_preview(capture_request(member=member, guild=guild))


def _safe(value: object) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def _live_role_error(member, preview) -> str | None:
    roles = tuple(getattr(member, 'roles', ()))
    role_names = {str(role.name) for role in roles}
    role_ids = {int(role.id) for role in roles}
    if bool(getattr(member, 'bot', False)) or int(member.id) == int(settings.owner_id):
        return 'protected bot/owner account'
    if preview.inactive_role_id not in role_ids:
        return 'Inactive role no longer assigned'
    if any(
        bool(getattr(role, 'managed', False)) and str(role.name) != '@everyone'
        for role in roles
    ):
        return 'protected managed role'
    if role_names.intersection(preview.protected_role_names):
        return 'protected staff/leadership role'
    allowed = (
        set(preview.starter_role_names)
        | set(preview.team_role_names)
        | {preview.inactive_role_name, '@everyone'}
    )
    if role_names - allowed:
        return 'protected unrecognized role'
    return None


def _private_details(*, failed, skipped, dm_failed, audit_error) -> str:
    lines = []
    if failed:
        lines.append('**Kick failures:** ' + ', '.join(
            f'{_safe(name)} (`{member_id}`): {_safe(reason)}'
            for member_id, name, reason in failed[:12]
        ))
    if skipped:
        lines.append('**Skipped after refresh:** ' + ', '.join(
            f'{_safe(name)} (`{member_id}`): {_safe(reason)}'
            for member_id, name, reason in skipped[:12]
        ))
    if dm_failed:
        lines.append(
            f'**DM failures:** {len(dm_failed)}; removals still continued.'
        )
    if audit_error:
        lines.append(
            '**Audit reconciliation:** committed Discord removals could not '
            'be fully recorded. Do not retry removed members.'
        )
    return '\n'.join(lines)


def _public_result(*, actor, kicked, failed, skipped, deferred, audit_failed):
    identity = league_user_commands.capture_actor(actor)
    message = (
        f'{identity.label} completed confirmed inactive-member maintenance: '
        f'**{kicked}** removed, **{failed}** kick failure(s), and '
        f'**{skipped}** skipped after refreshed validation.'
    )
    if deferred:
        message += (
            f' **{deferred}** remain beyond the '
            f'{workers.MAX_ACTION_CANDIDATES}-member run limit.'
        )
    if audit_failed:
        message += f' **{audit_failed}** audit row(s) require reconciliation.'
    return message


async def _execute(interaction, previous, confirmation):
    guild = interaction.guild
    if guild is None:
        raise workers.InactiveKickPermissionError(
            'Inactive-member removal requires a server.'
        )
    error = access_error(interaction.user, guild.id)
    if error:
        raise workers.InactiveKickPermissionError(error)
    expected = previous.confirmation_text
    if str(confirmation).strip() != expected:
        raise workers.InactiveKickError(
            f'Type `{expected}` exactly to continue.'
        )

    refreshed = await load_preview(member=interaction.user, guild=guild)
    if refreshed.candidate_ids != previous.candidate_ids:
        return InactiveKickConfirmationOutcome(
            state='refreshed',
            preview=refreshed,
            private_message=(
                'Guild roles or activity changed. The preview was refreshed; '
                'review every row and confirm the new count.'
            ),
        )

    inactive_role = guild.get_role(refreshed.inactive_role_id)
    if inactive_role is None or str(inactive_role.name) != refreshed.inactive_role_name:
        raise workers.InactiveKickError(
            'The configured Inactive role changed after preview. Run the '
            'command again.'
        )

    kicked = []
    failed = []
    skipped = []
    dm_failed = []
    for candidate in refreshed.action_candidates:
        current = guild.get_member(candidate.member_id)
        if current is None:
            skipped.append((
                candidate.member_id,
                candidate.display_name,
                'member already left the server',
            ))
            continue
        role_error = _live_role_error(current, refreshed)
        if role_error:
            skipped.append((current.id, _display_name(current), role_error))
            continue
        try:
            await current.send(KICK_DM)
        except Exception as exc:
            logger.info(
                'Inactive removal DM failed for guild %s member %s: %s',
                guild.id,
                current.id,
                exc,
            )
            dm_failed.append(current.id)
        try:
            await current.kick(reason=(
                f'Confirmed inactive-member maintenance by '
                f'{interaction.user} ({interaction.user.id})'
            ))
        except discord.DiscordException as exc:
            logger.warning(
                'Inactive removal failed for guild %s member %s: %s',
                guild.id,
                current.id,
                exc,
            )
            failed.append((current.id, _display_name(current), 'Discord kick failed'))
            continue
        except Exception as exc:
            logger.exception(
                'Unexpected inactive removal failure for guild %s member %s',
                guild.id,
                current.id,
            )
            failed.append((current.id, _display_name(current), type(exc).__name__))
            continue
        kicked.append((current.id, _display_name(current)))

    if not kicked:
        details = _private_details(
            failed=failed,
            skipped=skipped,
            dm_failed=dm_failed,
            audit_error=False,
        )
        return InactiveKickConfirmationOutcome(
            state='retryable',
            preview=refreshed,
            private_message=(
                'No member was removed. Review the refreshed failures before '
                'trying again.' + (f'\n{details}' if details else '')
            ),
            failed_count=len(failed),
            skipped_count=len(skipped),
            dm_failed_count=len(dm_failed),
        )

    audit_failed = 0
    try:
        audit = await workers.record_kicks(workers.InactiveKickAuditRequest(
            guild_id=int(guild.id),
            actor_id=int(interaction.user.id),
            actor_description=models.GameLog.member_string(interaction.user),
            rows=tuple(
                workers.KickAuditRow(member_id=member_id, display_name=name)
                for member_id, name in kicked
            ),
        ))
        if len(audit.log_ids) != len(kicked):
            audit_failed = len(kicked) - len(audit.log_ids)
    except Exception:
        audit_failed = len(kicked)
        logger.exception(
            'Inactive removals committed in guild %s but audit failed',
            guild.id,
        )

    public_message = _public_result(
        actor=interaction.user,
        kicked=len(kicked),
        failed=len(failed),
        skipped=len(skipped),
        deferred=refreshed.deferred_candidate_count,
        audit_failed=audit_failed,
    )
    channel = getattr(interaction, 'channel', None)
    sender = getattr(channel, 'send', None)
    publication_failed = not callable(sender)
    if not publication_failed:
        try:
            await sender(
                public_message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            publication_failed = True
            logger.exception(
                'Inactive removal aggregate failed to publish in guild %s',
                guild.id,
            )

    details = _private_details(
        failed=failed,
        skipped=skipped,
        dm_failed=dm_failed,
        audit_error=bool(audit_failed),
    )
    reconciliation = publication_failed or bool(audit_failed)
    return InactiveKickConfirmationOutcome(
        state='reconciliation' if reconciliation else 'complete',
        preview=refreshed,
        private_message=(
            f'{len(kicked)} member(s) were removed. '
            + (
                'Audit or public reporting requires reconciliation; do not '
                'retry removed members.'
                if reconciliation
                else 'The attributed public aggregate was posted.'
            )
            + (f'\n{details}' if details else '')
        ),
        kicked_count=len(kicked),
        failed_count=len(failed),
        skipped_count=len(skipped),
        dm_failed_count=len(dm_failed),
        audit_failed_count=audit_failed,
    )


async def confirm_and_publish(interaction, previous, confirmation):
    if not workers.claim_execution():
        raise workers.InactiveKickBusyError(
            'Another inactive-member removal is already running.'
        )
    operation = asyncio.create_task(_execute(interaction, previous, confirmation))
    try:
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError as cancellation:
            current = asyncio.current_task()
            while not operation.done():
                if current is not None:
                    current.uncancel()
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
            operation.result()
            raise asyncio.CancelledError from cancellation
    finally:
        workers.release_execution()
