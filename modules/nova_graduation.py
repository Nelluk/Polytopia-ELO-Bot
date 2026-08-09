"""Discord adapter for bounded automatic Nova graduation reads."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

import settings
from modules import nova_graduation_workers as workers, utilities


logger = logging.getLogger('polybot.' + __name__)

DEFAULT_SIGNUP_MESSAGE = (
    'Free Agent signups open regularly - pay attention to server '
    'announcements for a notification of the next one.'
)


@dataclass(frozen=True)
class NovaGraduationOutcome:
    game_id: int
    graduated_member_ids: tuple[int, ...]
    warnings: tuple[str, ...]


def _allowed_guild_ids() -> tuple[int, ...]:
    return tuple(dict.fromkeys(
        int(settings.server_ids[key])
        for key in ('polychampions', 'test')
        if settings.server_ids.get(key)
    ))


def _participant_snapshots(
    *,
    guild,
    participant_ids,
    nova_role,
    grad_role,
):
    snapshots = []
    missing = []
    seen = set()
    for raw_id in participant_ids:
        discord_id = int(raw_id)
        if discord_id in seen:
            continue
        seen.add(discord_id)
        member = guild.get_member(discord_id)
        if member is None:
            missing.append(discord_id)
            continue
        roles = tuple(getattr(member, 'roles', ()) or ())
        snapshots.append(workers.NovaParticipantSnapshot(
            discord_id=discord_id,
            member_name=str(member.name),
            mention=str(member.mention),
            has_nova_role=nova_role in roles,
            has_grad_role=grad_role in roles,
        ))
    return tuple(snapshots), tuple(missing)


async def _signup_message(guild, result: workers.NovaGraduationResult) -> str:
    if (
        not result.draft_open
        or result.draft_channel_id is None
        or result.draft_message_id is None
    ):
        return DEFAULT_SIGNUP_MESSAGE
    channel = guild.get_channel(result.draft_channel_id)
    if channel is None:
        return DEFAULT_SIGNUP_MESSAGE
    try:
        await channel.fetch_message(result.draft_message_id)
    except discord.NotFound:
        return DEFAULT_SIGNUP_MESSAGE
    except discord.DiscordException as exc:
        logger.warning(
            'Error loading draft announcement %s/%s for Nova graduation: %s',
            result.draft_channel_id,
            result.draft_message_id,
            exc,
        )
        return DEFAULT_SIGNUP_MESSAGE
    return f'Free Agent signups are currently open in <#{channel.id}>'


def _announcement(candidate, *, grad_role_name: str, signup_message: str):
    return (
        f'Player {candidate.mention} (*Global ELO: {candidate.global_elo} '
        f'\u00a0\u00a0\u00a0\u00a0W {candidate.wins} / L {candidate.losses}*) '
        f'has met the qualifications and is now a **{grad_role_name}**\n'
        f'{signup_message}'
    )


def _warning(game_id: int, detail: str) -> str:
    return f':warning: Game {game_id} Nova graduation reconciliation: {detail}'


async def run_nova_graduation(
    *,
    guild,
    game_id: int,
    participant_ids,
    output_channel=None,
    nova_role_name: str,
    grad_role_name: str,
) -> NovaGraduationOutcome:
    """Load eligibility once, then isolate every Discord-side candidate."""

    allowed_guild_ids = _allowed_guild_ids()
    if int(guild.id) not in allowed_guild_ids:
        return NovaGraduationOutcome(int(game_id), (), ())

    nova_role = discord.utils.get(guild.roles, name=nova_role_name)
    grad_role = discord.utils.get(guild.roles, name=grad_role_name)
    if nova_role is None or grad_role is None:
        logger.warning(
            'Could not load required Nova roles for game %s guild %s',
            game_id,
            guild.id,
        )
        return NovaGraduationOutcome(int(game_id), (), ())

    snapshots, missing_ids = _participant_snapshots(
        guild=guild,
        participant_ids=participant_ids,
        nova_role=nova_role,
        grad_role=grad_role,
    )
    for discord_id in missing_ids:
        logger.warning(
            'Could not load guild member %s for game %s Nova graduation',
            discord_id,
            game_id,
        )

    result = await workers.run_load_nova_graduation(
        workers.NovaGraduationRequest(
            game_id=int(game_id),
            guild_id=int(guild.id),
            allowed_guild_ids=allowed_guild_ids,
            participants=snapshots,
        )
    )
    signup_message = await _signup_message(guild, result)
    warnings = []
    graduated = []

    for candidate in result.candidates:
        member = guild.get_member(candidate.discord_id)
        if member is None:
            warnings.append(_warning(
                result.game_id,
                f'{candidate.mention} qualified, but the member is no longer '
                'available in this server.',
            ))
            continue
        roles = tuple(getattr(member, 'roles', ()) or ())
        if nova_role not in roles or grad_role in roles:
            logger.info(
                'Skipping stale Nova candidate %s for game %s after role '
                'revalidation',
                candidate.discord_id,
                result.game_id,
            )
            continue
        try:
            await member.add_roles(
                grad_role,
                reason=f'Automatic Nova graduation after game {result.game_id}',
            )
        except Exception as exc:
            logger.exception(
                'Could not assign Nova graduation role to %s for game %s',
                candidate.discord_id,
                result.game_id,
            )
            warnings.append(_warning(
                result.game_id,
                f'Discord rejected the graduation role for '
                f'{candidate.mention}: {exc}',
            ))
            continue

        graduated.append(candidate.discord_id)
        announcement = _announcement(
            candidate,
            grad_role_name=str(grad_role.name),
            signup_message=signup_message,
        )
        try:
            await utilities.send_to_log_channel(guild, announcement)
        except Exception as exc:
            logger.exception(
                'Could not publish Nova graduation log for %s game %s',
                candidate.discord_id,
                result.game_id,
            )
            warnings.append(_warning(
                result.game_id,
                f'{candidate.mention} received **{grad_role.name}**, but the '
                f'staff-log announcement failed: {exc}',
            ))
        if output_channel is not None:
            try:
                await output_channel.send(announcement)
            except Exception as exc:
                logger.exception(
                    'Could not publish Nova graduation output for %s game %s',
                    candidate.discord_id,
                    result.game_id,
                )
                warnings.append(_warning(
                    result.game_id,
                    f'{candidate.mention} received **{grad_role.name}**, but '
                    f'the game-channel announcement failed: {exc}',
                ))

    return NovaGraduationOutcome(
        game_id=result.game_id,
        graduated_member_ids=tuple(graduated),
        warnings=tuple(warnings),
    )
