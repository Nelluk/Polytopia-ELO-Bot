import discord
# from discord.ext import commands
import logging
# import asyncio
import modules.models as models
import settings
import peewee
logger = logging.getLogger('polybot.' + __name__)

# platinum - 1500
# gold - 1300+

# ELO Player - 2+ games
# ELO Veteran - 10+ games
# ELO Hero - 1350+ elo
# ELO Champion - #1 local or global leaderboard


async def set_champion_role():

    global_champion = models.DiscordMember.select().order_by(-models.DiscordMember.elo).limit(1).get()

    for guild in settings.bot.guilds:
        logger.warn(f'Attempting champion set for guild {guild.name}')
        role = discord.utils.get(guild.roles, name='ELO Champion')
        if not role:
            logger.warn(f'Could not load ELO Champion role in guild {guild.name}')
            continue

        local_champion = models.Player.select().where(models.Player.guild_id == guild.id).order_by(-models.Player.elo).limit(1).get()

        local_champion_member = guild.get_member(local_champion.discord_member.discord_id)
        global_champion_member = guild.get_member(global_champion.discord_id)

        for old_champion in role.members:
            await old_champion.remove_roles(role)
            logger.info(f'removing ELO Champion role from {old_champion.name}')

        if local_champion_member:
            logger.info(f'adding ELO Champion role to {local_champion_member.name}')
            await local_champion_member.add_roles(role)
        else:
            logger.warn(f'Couldnt find local champion {local_champion} in guild {guild.name}!')

        if global_champion_member:
            logger.info(f'adding ELO Champion role to {global_champion_member.name}')
            await global_champion_member.add_roles(role)
        else:
            logger.warn(f'Couldnt find global champion {global_champion.name} in guild {guild.name}!')


# async def set_achievement_role(player):
#     logger.debug(f'processing experience role for member {player.discord_member.name}')
#     max_local_elo = models.Player.select(peewee.fn.Max(models.Player.elo)).scalar()
#     max_global_elo = models.DiscordMember.select(peewee.fn.Max(models.DiscordMember.elo)).scalar()

#     flag_qualifies, flag_champion, flag_platinum, flag_gold = False, False, False, False

#     if player.discord_member.elo >= max_global_elo or player.elo >= max_local_elo:
#         flag_qualifies, flag_champion = True, True
#         # This player has #1 spot in either local OR global leaderboard. Apply ELO Champion role on any server where the player is

#     if player.discord_member.elo_max >= 1500:
#         flag_qualifies, flag_platinum = True, True
#     elif player.discord_member.elo_max >= 1300:
#         flag_qualifies, flag_gold = True, True

#     if flag_qualifies:
#         # member qualifies to have at least one achievement role assigned
#         for guildmember in player.discord_member.guildmembers:
#             guild = discord.utils.get(settings.bot.guilds, id=guildmember.guild_id)
#             member = guild.get_member(player.discord_member.discord_id) if guild else None

#             if not member:
#                 continue
#             if flag_champion:
#                 role = discord.utils.get(guild.roles, name='ELO Champion')


async def set_experience_role(discord_member):
    logger.debug(f'processing experience role for member {discord_member.name}')
    completed_games = discord_member.completed_game_count()

    for guildmember in discord_member.guildmembers:
        guild = discord.utils.get(settings.bot.guilds, id=guildmember.guild_id)
        member = guild.get_member(discord_member.discord_id) if guild else None

        if not member:
            continue

        role_list = []

        role = None
        if completed_games >= 2:
            role = discord.utils.get(guild.roles, name='ELO Player')
            role_list.append(role) if role is not None else None
        if completed_games >= 10:
            role = discord.utils.get(guild.roles, name='ELO Veteran')
            role_list.append(role) if role is not None else None
        if discord_member.elo_max >= 1350:
            role = discord.utils.get(guild.roles, name='ELO Hero')
            role_list.append(role) if role is not None else None

        if not role:
            continue

        if role not in member.roles:
            await member.remove_roles(*role_list)
            logger.info(f'removing roles from member {member}:\n:{role_list}')
            await member.add_roles(role)
            logger.info(f'adding role {role} to member {member}')

        max_local_elo = models.Player.select(peewee.fn.Max(models.Player.elo)).where(models.Player.guild_id == guild.id).scalar()
        max_global_elo = models.DiscordMember.select(peewee.fn.Max(models.DiscordMember.elo)).scalar()

        if discord_member.elo >= max_global_elo or guildmember.elo >= max_local_elo:
            # This player has #1 spot in either local OR global leaderboard. Apply ELO Champion role on any server where the player is:
            await set_champion_role()
