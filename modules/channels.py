import discord
# import asyncio
# from discord.ext import commands
import settings
# import peewee
# import modules.models as models
import modules.exceptions as exceptions
import logging

logger = logging.getLogger('polybot.' + __name__)


def generate_channel_name(game, team_name: str = None):
    # Turns game named 'The Mountain of Fire' to something like #e41-mountain-of-fire_ronin
    game_name = game.name
    game_id = game.id

    if not game_name:
        game_name = 'No Name'
        logger.warning(f'No game name passed to generate_channel_name for game {game_id}')
    if not team_name:
        logger.info(f'No team name passed to generate_channel_name for game {game_id}')
        team_name = ''

    game_team = f'{game_name.replace("the ","").replace("The ","")}_{team_name.replace("the ","").replace("The ","")}'.strip('_')

    if game.is_season_game():
        # hack to have special naming for season games, named eg 'S3W1 Mountains of Fire'. Makes channel easier to see
        chan_name = f'{" ".join(game_team.split()).replace(" ", "-")}-e{game_id}'
    else:
        chan_name = f'e{game_id}-{" ".join(game_team.split()).replace(" ", "-")}'
    return chan_name


def get_channel_category(guild, team_name: str = None, using_team_server_flag: bool = False):
    # Returns (DiscordCategory, Bool_IsTeamCategory?) or None
    # Bool_IsTeamCategory? == True if its using a team-specific category, False if using a central games category

    logger.debug(f'in get_channel_category - team_name: {team_name}; using_team_server_flag: {using_team_server_flag} ')
    list_of_generic_team_names = [a[0] for a in settings.generic_teams_long] + [a[0] for a in settings.generic_teams_short]

    if guild.me.guild_permissions.manage_channels is not True:
        logger.warning('manage_channels permission is false.')  # TODO: change this to see if bot has this perm in the category it selects
        # return None, None

    if team_name:
        team_name_lc = team_name.lower().replace('the', '').strip()  # The Ronin > ronin
        # first seek a category named something like 'Polychamps Ronin Games', fallback to any category with 'Ronin' in the name.
        # TODO: perm check in each fall back condition to make sure bot actually has permissions
        for cat in guild.categories:
            if 'polychamp' in cat.name.lower() and team_name_lc in cat.name.lower():
                logger.debug(f'Using {cat.id} - {cat.name} as a team channel category')
                if len(cat.channels) >= 50:
                    logger.warning('Chosen category is full - falling back')
                    continue
                return cat, True
        for cat in guild.categories:
            if team_name_lc in cat.name.lower():
                logger.debug(f'Using {cat.id} - {cat.name} as a team channel category')
                if len(cat.channels) >= 50:
                    logger.warning('Chosen category is full - falling back')
                    continue
                return cat, True
        if team_name in list_of_generic_team_names and using_team_server_flag:
            for cat in guild.categories:
                if 'polychamp' in cat.name.lower() and 'other' in cat.name.lower():
                    logger.debug(f'Mixed team - Using {cat.id} - {cat.name} as a team channel category')
                    if len(cat.channels) >= 50:
                        logger.warning('Chosen category is full - falling back')
                        continue
                    return cat, True

    # No team category found - using default category. ie. intermingled home/away games or channel for entire game

    for game_channel_category in settings.guild_setting(guild.id, 'game_channel_categories'):

        chan_category = discord.utils.get(guild.categories, id=int(game_channel_category))
        if chan_category is None:
            logger.warning(f'chans_category_id {game_channel_category} was supplied but cannot be loaded')
            continue

        if len(chan_category.channels) >= 50:
            logger.warning(f'chans_category_id {game_channel_category} was supplied but is full')
            continue

        logger.debug(f'using {chan_category.id} - {chan_category.name} for channel category')
        return chan_category, False  # Successfully use this chan_category

    logger.error('could not successfully load a channel category')
    return None, None


async def create_game_channel(guild, game, player_list, team_name: str = None, using_team_server_flag: bool = False):
    chan_cat, team_cat_flag = get_channel_category(guild, team_name, using_team_server_flag)
    if chan_cat is None:
        logger.error('in create_squad_channel - cannot proceed due to None category')
        return None

    if game.name.upper()[:3] == 'LR1' and guild.id == settings.server_ids['polychampions']:
        # temp code for event-specific game prefixes to go in their own category
        # 'wwn' leftover from original 'world war newt' event
        # code reused for LigaRex event Sept 2019
        wwn_category = discord.utils.get(guild.categories, id=622058617964593193)
        if wwn_category:
            chan_cat, team_cat_flag = wwn_category, False

    chan_name = generate_channel_name(game=game, team_name=team_name)
    chan_members = [guild.get_member(p.discord_member.discord_id) for p in player_list]
    if None in chan_members:
        logger.error(f'At least one member of game is not found in guild {guild.name}. May be using external server and they are not in both servers?')
        chan_members = [member for member in chan_members if member]

    chan_permissions = chan_cat.overwrites
    if not team_cat_flag and not using_team_server_flag:
        # Both chans going into a central ELO Games category. Set a default permissions to ensure it isnt world-readable
        chan_permissions[guild.default_role] = discord.PermissionOverwrite(read_messages=False)

    perm = discord.PermissionOverwrite(read_messages=True, add_reactions=True, send_messages=True, attach_files=True, manage_messages=True, create_public_threads=True)

    for m in chan_members + [guild.me]:
        chan_permissions[m] = perm
    try:
        new_chan = await guild.create_text_channel(name=chan_name, overwrites=chan_permissions, category=chan_cat, reason='ELO Game chan')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Exception in create_game_channels:\n{e} - Status {e.status}, Code {e.code}: {e.text}', exc_info=True)
        raise exceptions.MyBaseException(e)
        # return None
    except discord.errors.InvalidArgument as e:
        logger.error(f'Exception in create_game_channels:\n{e}')
        raise exceptions.MyBaseException(e)
        # return None
    logger.debug(f'Created channel {new_chan.name}')

    return new_chan


async def add_member_to_channel(channel, member):
    # Specifically add one given DiscordMember to a channel's permission overwrites
    # used when a player rejoins a server with games pending to get re-added to the channels
    overwrites = channel.overwrites
    overwrites[member] = discord.PermissionOverwrite(read_messages=True, add_reactions=True, send_messages=True, attach_files=True, manage_messages=True, create_public_threads=True)

    await channel.edit(overwrites=overwrites)


async def greet_game_channel(guild, chan, roster_names, game, player_list, full_game: bool = False):

    chan_mentions = [f'<@{p.discord_member.discord_id}>' for p in player_list]

    if full_game:
        allies_str = f'Participants in this game are {" / ".join(chan_mentions)}\n'
        chan_type_str = '**full game channel**'
    else:
        allies_str = f'Your teammates are {" / ".join(chan_mentions)}\n'
        chan_type_str = '**allied team channel**'

    if game.host or game.notes:
        match_content = f'Game hosted by **{game.host.name}**\n' if game.host else ''
        match_content = match_content + f'**Notes:** {game.notes}\n' if game.notes else match_content
    else:
        match_content = ''

    greeting_message = (f'This is the {chan_type_str} for game **{game.name}**, ID {game.id}.\n{allies_str}'
            f'The teams for this game are:\n{roster_names}\n\n'
            f'{match_content}'
            '*This channel will self-destruct soon after the game is marked as concluded.*')

    try:
        await chan.send(greeting_message)
        await chan.edit(topic=greeting_message[:1024], reason='Add topic')
    except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
        logger.error(f'Could not send to created channel:\n{e} - Status {e.status}, Code {e.code}: {e.text}')


async def delete_game_channel(guild, channel_id: int):

    try:
        chan = await settings.bot.fetch_channel(channel_id)
    except discord.DiscordException as e:
        return logger.warning(f'Could not retrieve channel with id {channel_id}: {e}')

    if 'ARCHIVE' in chan.name.upper() or (chan.category and 'ARCHIVE' in chan.category.name.upper()) or chan.permissions_for(guild.me).manage_channels is False:
        return logger.info(f'Skipping deletion for channel {chan.name} - appears to be archived by name or category name, or has manage_channel denied to me.')

    try:
        logger.warning(f'Deleting channel {chan.name}')
        await chan.delete(reason='Game concluded')
    except discord.DiscordException as e:
        logger.error(f'Could not delete channel: {e}')


async def send_message_to_channel(guild, channel_id: int, message: str, suppress_errors=True):
    chan = guild.get_channel(channel_id)
    if chan is None:
        logger.warning(f'Channel ID {channel_id} provided for message but it could not be loaded from guild')
        if suppress_errors:
            return
        raise exceptions.CheckFailedError(f':no_entry_sign: Channel `{channel_id}` provided for message but it could not be loaded from guild')

    try:
        await chan.send(message)
    except discord.DiscordException as e:
        logger.error(f'Could not send message to channel: {e}')
        if not suppress_errors:
            raise exceptions.CheckFailedError(f':no_entry_sign: Problem sending message to channel <#{channel_id}> `{channel_id}`: {e}')


async def update_game_channel_name(guild, channel_id: int, game, team_name: str = None):
    chan = guild.get_channel(channel_id)
    if chan is None:
        return logger.warning(f'Channel ID {channel_id} provided for update but it could not be loaded from guild')

    game_id, game_name = game.id, game.name

    chan_name = generate_channel_name(game=game, team_name=team_name)

    if chan_name.lower() == chan.name.lower():
        return logger.debug(f'Newly-generated channel name for channel {channel_id} game {game_id} is the same - no change to channel.')

    try:
        await chan.edit(name=chan_name, reason='Game renamed')
        logger.info(f'Renamed channel for game {game_id} to {chan_name}')
    except discord.DiscordException as e:
        logger.error(f'Could not edit channel: {e}')

    try:
        await chan.send(f'This game has been renamed to *{game_name}*.')
    except discord.DiscordException as e:
        logger.error(f'Could not send to channel: {e}')
