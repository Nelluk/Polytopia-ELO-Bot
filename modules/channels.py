import discord
# import asyncio
# from discord.ext import commands
import settings
# import peewee
# import modules.models as models
import logging

logger = logging.getLogger('polybot.' + __name__)


def generate_channel_name(game_id, game_name, team_name):
    # Turns game named 'The Mountain of Fire' to something like #e41-mountain-of-fire_ronin

    if not game_name:
        game_name = 'No Name'
        logger.warn(f'No game name passed to generate_channel_name for game {game_id}')
    if not team_name:
        logger.warn(f'No team name passed to generate_channel_name for game {game_id}')
        team_name = 'No Team'

    game_team = f'{game_name.replace("the ","").replace("The ","")}_{team_name.replace("the ","").replace("The ","")}'

    if game_name.lower()[:2] == 's3' or game_name.lower()[:2] == 's4':
        # hack to have special naming for season 3 or season 4 games, named eg 'S3W1 Mountains of Fire'. Makes channel easier to see
        chan_name = f'{" ".join(game_team.split()).replace(" ", "-")}-e{game_id}'
    else:
        chan_name = f'e{game_id}-{" ".join(game_team.split()).replace(" ", "-")}'
    return chan_name


def get_channel_category(ctx, team_name):
    # Returns (DiscordCategory, Bool_IsTeamCategory?) or None
    # Bool_IsTeamCategory? == True if its using a team-specific category, False if using a central games category

    if ctx.guild.me.guild_permissions.manage_channels is not True:
        logger.error('manage_channels permission is false.')
        return None, None
    team_name = team_name.lower().replace('the', '').strip()  # The Ronin > ronin
    for cat in ctx.guild.categories:
        if team_name in cat.name.lower():
            logger.debug(f'Using {cat.id} - {cat.name} as a team channel category')
            return cat, True

    # No team category found - using default category. ie. intermingled home/away games
    game_channel_category = settings.guild_setting(ctx.guild.id, 'game_channel_category')
    if game_channel_category is None:
        return None, None
    chan_category = discord.utils.get(ctx.guild.categories, id=int(game_channel_category))
    if chan_category is None:
        logger.error(f'chans_category_id {game_channel_category} was supplied but cannot be loaded')
        return None, None
    return chan_category, False


async def create_squad_channel(ctx, game, team_name, player_list):
    chan_cat, team_cat_flag = get_channel_category(ctx, team_name)
    if chan_cat is None:
        logger.error(f'in create_squad_channel - cannot proceed due to None category')
        return None

    chan_name = generate_channel_name(game_id=game.id, game_name=game.name, team_name=team_name)
    chan_members = [ctx.guild.get_member(p.discord_member.discord_id) for p in player_list]

    if team_cat_flag:
        # Channel is going into team-specific category, so let its permissions sync
        chan_permissions = None
    else:
        # Both chans going into a central ELO Games category. Give them special permissions so only game players can see chan

        chan_permissions = {}
        perm = discord.PermissionOverwrite(read_messages=True, add_reactions=True, send_messages=True, attach_files=True)

        for m in chan_members + [ctx.guild.me]:
            chan_permissions[m] = perm

        chan_permissions[ctx.guild.default_role] = discord.PermissionOverwrite(read_messages=False)

    try:
        new_chan = await ctx.guild.create_text_channel(name=chan_name, overwrites=chan_permissions, category=chan_cat, reason='ELO Game chan')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Exception in create_game_channels:\n{e} - Status {e.status}, Code {e.code}: {e.text}')
        await ctx.send(f'Could not create game channel for this game. Error has been logged.\n{e}')
        return None,
    except discord.errors.InvalidArgument as e:
        logger.error(f'Exception in create_game_channels:\n{e}')
        await ctx.send(f'Could not create game channel for this game. Error has been logged.\n{e}')
        return None
    logger.debug(f'Created channel {new_chan.name}')

    return new_chan


async def greet_squad_channel(ctx, chan, player_list, roster_names, game):
    chan_mentions = [ctx.guild.get_member(p.discord_member.discord_id).mention for p in player_list]

    if game.host or game.notes:
        match_content = f'Game hosted by **{game.host.name}**\n' if game.host else ''
        match_content = match_content + f'**Notes:** {game.notes}\n' if game.notes else match_content
    else:
        match_content = ''
    try:
        await chan.send(f'This is the team channel for game **{game.name}**, ID {game.id}.\n'
            f'Your teammates are {" / ".join(chan_mentions)}\n'
            f'The teams for this game are: {roster_names}\n\n'
            f'{match_content}'
            '*This channel will self-destruct as soon as the game is marked as concluded.*')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Could not send to created channel:\n{e} - Status {e.status}, Code {e.code}: {e.text}')


async def delete_squad_channel(ctx, channel_id: int):

    chan = ctx.guild.get_channel(channel_id)
    if chan is None:
        return logger.warn(f'Channel ID {channel_id} provided for deletion but it could not be loaded from guild')
    try:
        logger.warn(f'Deleting channel {chan.name}')
        await chan.delete(reason='Game concluded')
    except discord.DiscordException as e:
        logger.error(f'Could not delete channel: {e}')


async def update_squad_channel_name(ctx, channel_id: int, game_id: int, game_name: str, team_name: str):
    chan = ctx.guild.get_channel(channel_id)
    if chan is None:
        return logger.warn(f'Channel ID {channel_id} provided for update but it could not be loaded from guild')

    chan_name = generate_channel_name(game_id=game_id, game_name=game_name, team_name=team_name)
    try:
        await chan.edit(name=chan_name, reason='Game renamed')
        logger.info(f'Renamed channel for game {game_id} to {chan_name}')
    except discord.DiscordException as e:
        logger.error(f'Could not delete channel: {e}')

    await chan.send(f'This game has been renamed to *{game_name}*.')
