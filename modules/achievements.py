import discord
# from discord.ext import commands
import logging
# import asyncio
import modules.models as models
import settings
import peewee
import modules.utilities as utilities
logger = logging.getLogger('polybot.' + __name__)


# ELO Rookie - 2+ games
# ELO Player - 10+ games
# ELO Veteran - 1200+ games
# ELO Hero - 1350+ elo
# ELO Champion - #1 local or global leaderboard


async def set_champion_role():

    # global_champion = models.DiscordMember.select().order_by(-models.DiscordMember.elo).limit(1).get()
    global_champion = models.DiscordMember.leaderboard(date_cutoff=settings.date_cutoff, guild_id=None, max_flag=False).limit(1).get()
    if global_champion.elo_field == 1000:
        global_champion = None

    for guild in settings.bot.guilds:
        log_message = ''
        logger.info(f'Attempting champion set for guild {guild.name}')
        role = discord.utils.get(guild.roles, name='ELO Champion')
        if not role:
            logger.warning(f'Could not load ELO Champion role in guild {guild.name}')
            continue

        # local_champion = models.Player.select().where(models.Player.guild_id == guild.id).order_by(-models.Player.elo).limit(1).get()
        local_champion = models.Player.leaderboard(date_cutoff=settings.date_cutoff, guild_id=guild.id, max_flag=False).limit(1).get()
        if local_champion.elo_field == 1000:
            continue

        local_champion_member = guild.get_member(local_champion.discord_member.discord_id)
        global_champion_member = guild.get_member(global_champion.discord_id) if global_champion else None

        try:
            for old_champion in role.members:
                if old_champion in [local_champion_member, global_champion_member]:
                    logger.debug(f'Skipping role removal for {old_champion.display_name} since champion is the same')
                else:
                    await old_champion.remove_roles(role, reason='Recurring reset of champion list')
                    log_message += f'{models.GameLog.member_string(local_champion_member)} lost **ELO Champion** role.\n'
                    logger.info(f'removing ELO Champion role from {old_champion.name}')

            if local_champion_member:
                if local_champion_member not in role.members:
                    logger.info(f'adding ELO Champion role to {local_champion_member.name}')
                    await local_champion_member.add_roles(role, reason='Local champion')
                    log_message += f'{models.GameLog.member_string(local_champion_member)} given role for local **ELO Champion**\n'
            else:
                logger.warning(f'Couldnt find local champion {local_champion} in guild {guild.name}!')

            if global_champion_member:
                if global_champion_member not in role.members:
                    logger.info(f'adding ELO Champion role to {global_champion_member.name}')
                    await global_champion_member.add_roles(role, reason='Global champion')
                    log_message += f'{models.GameLog.member_string(global_champion_member)} given role for global **ELO Champion**\n'
            else:
                logger.warning(f'Couldnt find global champion {global_champion.name} in guild {guild.name}!')
        except discord.DiscordException as e:
            logger.warning(f'Error during set_champion_role for guild {guild.id}: {e}')
            continue

        if log_message:
            await utilities.send_to_log_channel(guild, log_message)
            models.GameLog.write(guild_id=guild.id, message=log_message)


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
            continue

        if role not in member.roles:
            logger.debug(f'Applying new achievement role {role.name} to {member.display_name}')
            try:
                if role not in role_list or len(role_list) > 1:
                    await member.remove_roles(*role_list)
                    logger.info(f'removing roles from member {member}:\n:{role_list}')
                await member.add_roles(role)
                logger.info(f'adding role {role} to member {member}')
            except discord.DiscordException as e:
                logger.warning(f'Error during set_experience_role for guild {guild.id} member {member.display_name}: {e}')

        max_local_elo = models.Player.select(peewee.fn.Max(models.Player.elo_moonrise)).where(models.Player.guild_id == guild.id).scalar()
        max_global_elo = models.DiscordMember.select(peewee.fn.Max(models.DiscordMember.elo_moonrise)).scalar()

        if discord_member.elo_moonrise >= max_global_elo or guildmember.elo_moonrise >= max_local_elo:
            # This player has #1 spot in either local OR global leaderboard. Apply ELO Champion role on any server where the player is:
            await set_champion_role()
