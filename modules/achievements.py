import discord
# from discord.ext import commands
import logging
# import asyncio
# import modules.models as models
import settings
# import peewee

logger = logging.getLogger('polybot.' + __name__)


async def set_experience_role(discord_member):
    completed_games = discord_member.completed_game_count()

    for guildmember in discord_member.guildmembers:
        guild = discord.utils.get(settings.bot.guilds, id=guildmember.guild_id)
        member = guild.get_member(discord_member.discord_id) if guild else None

        if not member:
            continue

        role_list = []

        role = None
        if completed_games > 2:
            role = discord.utils.get(guild.roles, name='ELO-Player')
            role_list.append(role) if role is not None else None
        if completed_games > 15:
            role = discord.utils.get(guild.roles, name='ELO-Veteran')
            role_list.append(role) if role is not None else None

        if not role:
            continue

        if role not in member.roles:
            await member.remove_roles(*role_list)
            logger.info(f'removing roles from member {member}:\n:{role_list}')
            await member.add_roles(role)
            logger.info(f'adding role {role} to member {member}')
