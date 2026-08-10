from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord
# from discord.ext import commands
import logging
# import asyncio
import modules.models as models
import settings
import peewee
import modules.utilities as utilities
from modules import champion_role_workers
logger = logging.getLogger('polybot.' + __name__)
_champion_reconciliation_lock = asyncio.Lock()


# ELO Rookie - 2+ games
# ELO Player - 10+ games
# ELO Veteran - 1200+ games
# ELO Hero - 1350+ elo
# ELO Champion - #1 local or global leaderboard


@dataclass(frozen=True)
class ChampionGuildOutcome:
    guild_id: int
    succeeded_count: int
    failed_count: int
    converged: bool
    audit_recorded: bool
    staff_log_sent: bool


@dataclass(frozen=True)
class ChampionCycleOutcome:
    plan: champion_role_workers.ChampionRolePlan
    guilds: tuple[ChampionGuildOutcome, ...]
    post_effect_plan: champion_role_workers.ChampionRolePlan | None
    plan_current: bool


def _member_log_string(member) -> str:
    name = getattr(member, 'display_name', None) or getattr(member, 'name', None)
    return (
        f'**{discord.utils.escape_markdown(name or "Unknown member")}** '
        f'(`{member.id}`)'
    )


async def _apply_champion_guild(guild, target, global_champion_id):
    logger.info('Attempting champion reconciliation for guild %s', guild.id)
    role = discord.utils.get(guild.roles, name='ELO Champion')
    if role is None:
        logger.warning(
            'Could not load ELO Champion role in guild %s', guild.name
        )
        return ChampionGuildOutcome(guild.id, 0, 1, False, False, False)

    local_member = (
        guild.get_member(target.local_champion_discord_id)
        if target.local_champion_discord_id is not None
        else None
    )
    global_member = (
        guild.get_member(global_champion_id)
        if global_champion_id is not None
        else None
    )
    planned_ids = {
        int(member_id)
        for member_id in (
            target.local_champion_discord_id,
            global_champion_id,
        )
        if member_id is not None
    }
    desired = []
    resolved_ids = set()
    for member, reason in (
        (local_member, 'Local champion'),
        (global_member, 'Global champion'),
    ):
        if member is None or int(member.id) in resolved_ids:
            continue
        resolved_ids.add(int(member.id))
        desired.append((member, reason))

    missing_ids = planned_ids - resolved_ids
    for member_id in sorted(missing_ids):
        logger.warning(
            'Could not resolve planned ELO Champion member %s in guild %s',
            member_id,
            guild.id,
        )

    assigned_ids = {
        int(member.id) for member in tuple(getattr(role, 'members', ()) or ())
    }
    messages = []
    failures = len(missing_ids)
    for old_champion in tuple(getattr(role, 'members', ()) or ()):
        if int(old_champion.id) in planned_ids:
            continue
        try:
            await old_champion.remove_roles(
                role,
                reason='Recurring reset of champion list',
            )
        except Exception:
            failures += 1
            logger.exception(
                'Could not remove ELO Champion role in guild %s from %s',
                guild.id,
                old_champion.id,
            )
            continue
        assigned_ids.discard(int(old_champion.id))
        messages.append(
            f'{_member_log_string(old_champion)} lost '
            '**ELO Champion** role.'
        )

    for member, reason in desired:
        if int(member.id) in assigned_ids:
            continue
        try:
            await member.add_roles(role, reason=reason)
        except Exception:
            failures += 1
            logger.exception(
                'Could not add ELO Champion role in guild %s to %s',
                guild.id,
                member.id,
            )
            continue
        assigned_ids.add(int(member.id))
        messages.append(
            f'{_member_log_string(member)} given role for '
            f'{reason.lower()} **ELO Champion**'
        )

    audit_recorded = not messages
    if messages:
        try:
            await champion_role_workers.run_record_champion_role_audit(
                champion_role_workers.ChampionAuditRequest(
                    guild_id=int(guild.id),
                    messages=tuple(messages),
                )
            )
            audit_recorded = True
        except Exception:
            logger.exception(
                'Champion role effects completed in guild %s but their '
                'database audit requires reconciliation',
                guild.id,
            )

    staff_log_sent = not messages
    if messages:
        try:
            await utilities.send_to_log_channel(guild, '\n'.join(messages))
            staff_log_sent = True
        except Exception:
            logger.exception(
                'Champion role effects completed in guild %s but their '
                'staff log could not be sent',
                guild.id,
            )

    return ChampionGuildOutcome(
        guild_id=int(guild.id),
        succeeded_count=len(messages),
        failed_count=failures,
        converged=assigned_ids == planned_ids and not missing_ids,
        audit_recorded=audit_recorded,
        staff_log_sent=staff_log_sent,
    )


async def _set_champion_role(*, bot=None):
    """Reconcile champion roles from one immutable worker-loaded plan."""

    bot = bot or settings.bot
    request = champion_role_workers.ChampionRoleRequest(
        guild_ids=tuple(int(guild.id) for guild in bot.guilds),
        date_cutoff=settings.date_cutoff,
    )
    plan = await champion_role_workers.run_load_champion_role_plan(request)
    outcomes = []
    for target in plan.guilds:
        guild = bot.get_guild(target.guild_id)
        if guild is None:
            logger.warning(
                'Could not load champion reconciliation guild %s',
                target.guild_id,
            )
            outcomes.append(ChampionGuildOutcome(
                target.guild_id, 0, 1, False, False, False
            ))
            continue
        try:
            outcomes.append(await _apply_champion_guild(
                guild,
                target,
                plan.global_champion_discord_id,
            ))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                'Champion reconciliation failed for guild %s; later guilds '
                'remain available',
                target.guild_id,
            )
            outcomes.append(ChampionGuildOutcome(
                target.guild_id, 0, 1, False, False, False
            ))
    try:
        post_effect_plan = await (
            champion_role_workers.run_load_champion_role_plan(request)
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            'Champion role effects completed but the authoritative '
            'post-effect plan could not be reloaded'
        )
        post_effect_plan = None
    plan_current = post_effect_plan == plan
    if not plan_current:
        logger.warning(
            'Champion role eligibility changed or could not be revalidated '
            'during publication; the next cycle will reconcile it'
        )
    return ChampionCycleOutcome(
        plan=plan,
        guilds=tuple(outcomes),
        post_effect_plan=post_effect_plan,
        plan_current=plan_current,
    )


async def set_champion_role(*, bot=None):
    """Serialize recurring and result-triggered champion reconciliation."""

    async with _champion_reconciliation_lock:
        return await _set_champion_role(bot=bot)


async def award_booster_role(discord_member):
    logger.info(f'awarding booster role for member {discord_member.name}')

    counter = 0
    for guildmember in list(discord_member.guildmembers):
        guild = settings.bot.get_guild(guildmember.guild_id)
        member = guild.get_member(discord_member.discord_id) if guild else None

        if not member:
            logger.debug(f'Skipping guild {guildmember.guild_id}, could not load both guild and its member object')
            continue

        boost_role = discord.utils.find(lambda r: 'ELO' in r.name.upper() and 'BOOSTER' in r.name.upper(), guild.roles)
        if not boost_role:
            logger.debug(f'Skipping guild {guildmember.guild_id}, could not load a matching booster role')
            continue

        logger.debug(f'Using boost_role {boost_role.name} for guild {guild.name}')

        try:
            await member.add_roles(boost_role)
            logger.info(f'adding role {boost_role} to member {member}')
            counter += 1
        except discord.DiscordException as e:
            logger.warning(f'Error during award_booster_role for guild {guild.id} member {member.display_name}: {e}')

    logger.debug(f'Successfully awarded role in {counter} servers')
    return counter


async def set_experience_role(discord_member):
    logger.debug(f'processing experience role for member {discord_member.name}')
    completed_games = discord_member.completed_game_count(only_ranked=False, moonrise=models.is_post_moonrise())

    for guildmember in list(discord_member.guildmembers):
        guild = settings.bot.get_guild(guildmember.guild_id)
        member = guild.get_member(discord_member.discord_id) if guild else None

        if not member:
            logger.debug(f'Skipping guild {guildmember.guild_id}, could not load both guild and its member object')
            continue

        role_list = []
        elo_max = discord_member.elo_max_moonrise if models.is_post_moonrise() else discord_member.elo_max
        role = None
        if completed_games >= 2:
            role = discord.utils.get(guild.roles, name='ELO Rookie')
            role_list.append(role) if role is not None else None
        if completed_games >= 10:
            role = discord.utils.get(guild.roles, name='ELO Player')
            role_list.append(role) if role is not None else None
        if discord_member.elo_max >= 1200 or discord_member.elo_max_moonrise >= 1200:
            # special case for resetting pre-moonrise Veterans and above down to Veteran
            role = discord.utils.get(guild.roles, name='ELO Veteran')
            role_list.append(role) if role is not None else None
        if elo_max >= 1350:
            role = discord.utils.get(guild.roles, name='ELO Hero')
            role_list.append(role) if role is not None else None
        if elo_max >= 1500:
            role = discord.utils.get(guild.roles, name='ELO Elite')
            role_list.append(role) if role is not None else None
        if elo_max >= 1650:
            role = discord.utils.get(guild.roles, name='ELO Master')
            role_list.append(role) if role is not None else None
        if elo_max >= 1800:
            role = discord.utils.get(guild.roles, name='ELO Titan')
            role_list.append(role) if role is not None else None

        if not role:
            logger.debug(f'No relevant achievement role loaded for guild {guild.name} ')
            continue

        role_list.remove(role)
        logger.debug(f'Earned role: {role.name}\nUnearned role list: {role_list}')

        if role not in member.roles or any(item in member.roles for item in role_list):
            logger.debug(f'Updating achievement roles for {member.display_name}')
            try:
                await member.remove_roles(*role_list)
                logger.info(f'removing roles from member {member}:\n:{role_list}')
                await member.add_roles(role)
                logger.info(f'adding role {role} to member {member}')
            except discord.DiscordException as e:
                logger.warning(f'Error during set_experience_role for guild {guild.id} member {member.display_name}: {e}')
        else:
            logger.debug('No achievement roles to change')

        max_local_elo = models.Player.select(peewee.fn.Max(models.Player.elo_moonrise)).where(models.Player.guild_id == guild.id).scalar()
        max_global_elo = models.DiscordMember.select(peewee.fn.Max(models.DiscordMember.elo_moonrise)).scalar()

        if discord_member.elo_moonrise >= max_global_elo or guildmember.elo_moonrise >= max_local_elo:
            # This player has #1 spot in either local OR global leaderboard. Apply ELO Champion role on any server where the player is:
            await set_champion_role()
