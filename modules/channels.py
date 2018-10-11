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
        return None, None

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
        await ctx.send(f'Could not create game channel for this game. Error has been logged.')
        return None, None
    except discord.errors.InvalidArgument as e:
        logger.error(f'Exception in create_game_channels:\n{e}')
        await ctx.send(f'Could not create game channel for this game. Error has been logged.')
        return None, None
    logger.debug(f'Created channel {new_chan.name}')

    return new_chan, chan_cat


async def greet_squad_channel(ctx, chan, cat, player_list, roster_names, game):
    chan_mentions = [ctx.guild.get_member(p.discord_member.discord_id).mention for p in player_list]

    try:
        await chan.send(f'This is the team channel for game **{game.name}**, ID {game.id}.\n'
            f'Your teammates are {" / ".join(chan_mentions)}\n'
            f'The teams for this game are: {roster_names}\n\n'
            '*This channel will self-destruct as soon as the game is marked as concluded.*')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Could not send to created channel:\n{e} - Status {e.status}, Code {e.code}: {e.text}')


async def create_game_channels(ctx, game, home_players, away_players):

    home_cat = get_channel_category(ctx, game.home_team.name)
    away_cat = get_channel_category(ctx, game.away_team.name)
    if home_cat is None or away_cat is None:
        return logger.error(f'in create_game_channels - cannot proceed due to None category')

    home_chan_name = generate_channel_name(game_id=game.id, game_name=game.name, team_name=game.home_team.name)
    away_chan_name = generate_channel_name(game_id=game.id, game_name=game.name, team_name=game.away_team.name)
    home_members = [ctx.guild.get_member(p.discord_id) for p in home_players]
    away_members = [ctx.guild.get_member(p.discord_id) for p in away_players]

    if home_cat == away_cat:
        # Both chans going into a central ELO Games category. Give them special permissions so only game players can see chan

        home_permissions, away_permissions = {}, {}
        perm = discord.PermissionOverwrite(read_messages=True, add_reactions=True, send_messages=True, attach_files=True)

        for m in home_members + [ctx.guild.me]:
            home_permissions[m] = perm
        for m in away_members + [ctx.guild.me]:
            away_permissions[m] = perm

        home_permissions[ctx.guild.default_role] = away_permissions[ctx.guild.default_role] = discord.PermissionOverwrite(read_messages=False)
    else:
        # I assume in this case games are going into their respective Team categories, so let them sync permissions.
        # This might need to change if channel structure on the server changes
        home_permissions = away_permissions = None

    try:
        home_chan = await ctx.guild.create_text_channel(name=home_chan_name, overwrites=home_permissions, category=home_cat, reason='ELO Game chan')
        away_chan = await ctx.guild.create_text_channel(name=away_chan_name, overwrites=away_permissions, category=away_cat, reason='ELO Game chan')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Exception in create_game_channels:\n{e} - Status {e.status}, Code {e.code}: {e.text}')
        return await ctx.send(f'Could not create game channel for this game. Error has been logged.')
    except discord.errors.InvalidArgument as e:
        logger.error(f'Exception in create_game_channels:\n{e}')
        return await ctx.send(f'Could not create game channel for this game. Error has been logged.')
    logger.debug(f'Created channels {home_chan.name} and {away_chan.name}')

    home_mentions, away_mentions = [p.mention for p in home_members], [p.mention for p in away_members]
    home_names, away_names = [p.discord_name for p in home_players], [p.discord_name for p in away_players]

    try:
        await home_chan.send(f'This is the team channel for game **{game.name}**, ID {game.id}.\n'
            f'This team is composed of {" / ".join(home_mentions)}\n'
            f'Your opponents are: {" / ".join(away_names)}\n\n'
            '*This channel will self-destruct as soon as the game is marked as concluded.*')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Could not send to created channel:\n{e} - Status {e.status}, Code {e.code}: {e.text}')
    try:
        await away_chan.send(f'This is the team channel for game **{game.name}**, ID {game.id}.\n'
            f'This team is composed of {" / ".join(away_mentions)}\n'
            f'Your opponents are: {" / ".join(home_names)}\n\n'
            '*This channel will self-destruct as soon as the game is marked as concluded.*')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Could not send to created channel:\n{e} - Status {e.status}, Code {e.code}: {e.text}')

    return


async def delete_game_channels(ctx, game):

    home_cat = get_channel_category(ctx, game.home_team.name)
    away_cat = get_channel_category(ctx, game.away_team.name)
    if home_cat is None or away_cat is None:
        return logger.error(f'in delete_game_channels - cannot proceed due to None category')

    matching_chans = [c for c in (home_cat.channels + away_cat.channels) if c.name.startswith(f'e{game.id}-')]
    for chan in matching_chans:
        logger.warn(f'Deleting channel {chan.name}')
        try:
            await chan.delete(reason='Game concluded')
        except (discord.DiscordException, discord.errors.DiscordException) as e:
            logger.error(f'Could not delete channel: {e}')


async def update_game_channel_name(ctx, game, old_game_name, new_game_name):

    # Update a channel's name when its associated game is renamed
    # This will fail if the team name has changed since the game started, or if someone manually renamed the channel already

    home_cat = get_channel_category(ctx, game.home_team.name)
    away_cat = get_channel_category(ctx, game.away_team.name)
    if home_cat is None or away_cat is None:
        return logger.error(f'in update_game_channel_name - cannot proceed due to None category')

    old_home_chan_name = generate_channel_name(game_id=game.id, game_name=old_game_name, team_name=game.home_team.name)
    old_away_chan_name = generate_channel_name(game_id=game.id, game_name=old_game_name, team_name=game.away_team.name)

    new_home_chan_name = generate_channel_name(game_id=game.id, game_name=new_game_name, team_name=game.home_team.name)
    new_away_chan_name = generate_channel_name(game_id=game.id, game_name=new_game_name, team_name=game.away_team.name)

    old_home_chan = discord.utils.get(home_cat.channels, name=old_home_chan_name.lower())
    old_away_chan = discord.utils.get(away_cat.channels, name=old_away_chan_name.lower())

    if old_home_chan is None or old_away_chan is None:
        logger.error(f'Was not able to find existing channel to rename: {old_home_chan} or {old_away_chan}')
        return

    logger.debug(f'updating {old_home_chan_name} to {new_home_chan_name}')
    logger.debug(f'updating {old_away_chan_name} to {new_away_chan_name}')
    await old_home_chan.edit(name=new_home_chan_name, reason='Game renamed')
    await old_away_chan.edit(name=new_away_chan_name, reason='Game renamed')
