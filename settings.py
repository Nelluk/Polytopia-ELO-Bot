import modules.exceptions as exceptions
import logging
import datetime
from discord.ext import commands
import discord
import configparser
logger = logging.getLogger('polybot.' + __name__)

config = configparser.ConfigParser()
config.read('config.ini')

try:
    discord_key = config['DEFAULT']['discord_key']
    psql_user = config['DEFAULT']['psql_user']
except KeyError:
    logger.error('Error finding a required setting (discord_key / psql_user) in config.ini file')
    exit(0)

pastebin_key = config['DEFAULT'].get('pastebin_key', None)

server_ids = {'main': 283436219780825088, 'polychampions': 447883341463814144, 'test': 478571892832206869}
owner_id = 272510639124250625  # Nelluk

config = {'default':
                     {'helper_roles': ['Helper'],
                      'mod_roles': ['Mod'],
                      'require_teams': False,
                      'allow_teams': False,
                      'allow_uneven_teams': False,
                      'max_team_size': 1,
                      'command_prefix': '/',
                      'bot_channels': [],
                      'game_request_channel': None,
                      'game_announce_channel': None,
                      'game_channel_category': None},
          478571892832206869:                           # Nelluk Test Server (discord server ID)
                     {'helper_roles': ['testers'],
                      'mod_roles': ['role1'],
                      'require_teams': True,
                      'allow_teams': True,
                      'max_team_size': 2,
                      'command_prefix': '/',
                      'bot_channels': [479292913080336397, 481558031281160212, 480078679930830849],
                      'game_request_channel': 480078679930830849,
                      'game_announce_channel': 481558031281160212,
                      'game_channel_category': 493149162238640161},
          447883341463814144:                           # Polychampions
                     {'helper_roles': ['Helper', 'ELO Helper'],
                      'mod_roles': ['Mod'],
                      'require_teams': True,
                      'allow_teams': True,
                      'max_team_size': 5,
                      'command_prefix': '$',
                      'bot_channels': [487303307224940545, 487302138704429087, 487304043786665986, 487222333589815315, 487562981635522570, 447902433964851210],
                      'game_request_channel': 487562981635522570,
                      'game_announce_channel': 487302138704429087,
                      'game_channel_category': 488421911529914368},
          283436219780825088:                           # Main Server
                     {'helper_roles': ['Bot Master', 'Tribe Leader', 'Director', 'Bot Master'],
                      'mod_roles': ['MOD', 'Manager'],
                      'require_teams': False,
                      'allow_teams': True,
                      'max_team_size': 2,
                      'command_prefix': '$',
                      'bot_channels': [396069729657421824],
                      'game_request_channel': None,
                      'game_announce_channel': None,
                      'game_channel_category': None}

          }

date_cutoff = datetime.datetime.today() - datetime.timedelta(days=90)  # Players who haven't played since cutoff are not included in leaderboards


def get_setting(setting_name):
    return config['default'][setting_name]


def guild_setting(guild_id: int, setting_name: str):

    try:
        settings_obj = config[guild_id]
    except KeyError:
        logger.error(f'Unauthorized guild id {guild_id}.')
        raise exceptions.CheckFailedError('Unauthorized: This guild is not in the config.ini file.')

    try:
        return settings_obj[setting_name]
    except KeyError:
        return config['default'][setting_name]


def get_matching_roles(discord_member, list_of_role_names):
        # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.polytopia_id
        member_roles = [x.name for x in discord_member.roles]
        return set(member_roles).intersection(list_of_role_names)


def is_power_user(ctx):
    if is_staff(ctx):
        return True

    if ctx.guild.id == server_ids['main']:
        minimum_role = discord.utils.get(ctx.guild.roles, name='Amphibian')
        if ctx.author.top_role < minimum_role:
            # await ctx.send('You must attain "Amphibian" role to do this.')
            return False
    if ctx.guild.id == server_ids['test']:
        minimum_role = discord.utils.get(ctx.guild.roles, name='testers')
        return ctx.author.top_role >= minimum_role

    return True


def is_matchmaking_power_user(ctx):
    if is_staff(ctx):
        return True

    if ctx.guild.id == server_ids['main']:
        minimum_role = discord.utils.get(ctx.guild.roles, name='Archer')
        if ctx.author.top_role < minimum_role:
            # await ctx.send('You must attain "Amphibian" role to do this.')
            return False
    if ctx.guild.id == server_ids['polychampions']:
        minimum_role = discord.utils.get(ctx.guild.roles, name='Team Co-Leader')
        return ctx.author.top_role >= minimum_role

    return True


def is_staff(ctx):

    if ctx.author.id == owner_id:
        return True
    helper_roles = guild_setting(ctx.guild.id, 'helper_roles')
    mod_roles = guild_setting(ctx.guild.id, 'mod_roles')

    target_match = get_matching_roles(ctx.author, helper_roles + mod_roles)
    return len(target_match) > 0


async def is_mod(ctx):

    if ctx.author.id == owner_id:
        return True
    mod_roles = guild_setting(ctx.guild.id, 'mod_roles')

    target_match = get_matching_roles(ctx.author, mod_roles)
    return len(target_match) > 0


def is_staff_check():
    # restrict commands to is_staff with syntax like @settings.is_staff_check()

    def predicate(ctx):
        return is_staff(ctx)
    return commands.check(predicate)


def is_mod_check():
    # restrict commands to is_staff with syntax like @settings.is_mod_check()

    def predicate(ctx):
        return is_mod(ctx)
    return commands.check(predicate)


def on_polychampions():

    def predicate(ctx):
        return ctx.guild.id == server_ids['polychampions']
    return commands.check(predicate)


def teams_allowed():

    def predicate(ctx):
        return guild_setting(ctx.guild.id, 'allow_teams')
    return commands.check(predicate)


def in_bot_channel():
    async def predicate(ctx):
        if guild_setting(ctx.guild.id, 'bot_channels') is None:
            return True
        if await is_mod(ctx):
            return True
        if ctx.message.channel.id in guild_setting(ctx.guild.id, 'bot_channels'):
            return True
        else:
            primary_bot_channel = guild_setting(ctx.guild.id, 'bot_channels')[0]
            await ctx.send(f'This command can only be used in a designated ELO bot channel. Try <#{primary_bot_channel}>')
            return False
    return commands.check(predicate)
