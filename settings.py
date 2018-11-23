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
    psql_db = config['DEFAULT']['psql_db']
except KeyError:
    logger.error('Error finding a required setting (discord_key / psql_user / psql_db) in config.ini file')
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
                      'include_in_global_lb': False,
                      'bot_channels': [],
                      'match_challenge_channel': None,
                      'bot_channels_private': [],  # channels here will pass any bot channel check, and not linked in bot messages
                      'bot_channels_strict': [],  # channels where the most limited commands work, like leaderboards
                      'bot_channels': [],  # channels were more common commands work, like matchmaking
                      'newbie_message_channels': [],  # channels on which to broadcast a basic help message on repeat
                      'match_challenge_channels': [],  # opengames list broadcast on repeat
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_request_channel': None,
                      'game_announce_channel': None,
                      'game_channel_category': None},
          478571892832206869:                           # Nelluk Test Server (discord server ID)
                     {'helper_roles': ['testers'],
                      'mod_roles': ['role1'],
                      'display_name': 'Development Server',
                      'require_teams': True,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '/',
                      'bot_channels_strict': [479292913080336397],
                      'bot_channels': [479292913080336397, 481558031281160212, 480078679930830849],  # 397 Bot Spam,  849 Admin Spam
                      # 'match_challenge_channels': [481558031281160212, 478571893272870913],  # 212 Testroom1
                      'ranked_game_channel': 479292913080336397,
                      'unranked_game_channel': 481558031281160212,
                      'game_request_channel': 480078679930830849,
                      'game_announce_channel': 481558031281160212,
                      'game_channel_category': 493149162238640161},
          447883341463814144:                           # Polychampions
                     {'helper_roles': ['Helper', 'ELO Helper', 'Team Leader'],
                      'mod_roles': ['Mod'],
                      'display_name': 'PolyChampions',
                      'require_teams': True,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 5,
                      'command_prefix': '$',
                      'include_in_global_lb': True,
                      'bot_channels_private': [487304043786665986],  # 986 elo-staff-talk
                      'bot_channels_strict': [487303307224940545, 448317497473630229],  # 545 elo-commands, 229 bot-commands
                      'bot_channels': [487303307224940545, 448317497473630229, 452639822616723457, 469027618289614850],  # 457 elo-chalenges, 850 dont-timeout
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'match_challenge_channels': [452639822616723457],  # elo-challenges
                      'game_request_channel': 487304043786665986,  # $staffhelp output
                      'game_announce_channel': 487302138704429087,  # elo-drafts
                      'game_channel_category': 514141474229846018},  # ELO CHALLENGES
          283436219780825088:                           # Main Server
                     {'helper_roles': ['Bot Master', 'Tribe Leader', 'Director', 'ELO Helper'],
                      'mod_roles': ['MOD', 'Manager'],
                      'display_name': 'Polytopia',
                      'allow_uneven_teams': True,
                      'require_teams': False,
                      'allow_teams': False,
                      'max_team_size': 1,
                      'command_prefix': '$',
                      'include_in_global_lb': True,
                      'bot_channels_private': [418175357137453058],  # 058 testchamber
                      'bot_channels_strict': [403724174532673536],  # 536 BotCommands
                      'bot_channels': [403724174532673536, 511316081160355852, 511906353476927498],  # 498 unranked-games, 852 ranked-games
                      'newbie_message_channels': [396069729657421824, 413721247260868618, 418326008526143508, 418326044077064192],  # multi-discussion, friend codes NA/euro/asia
                      'ranked_game_channel': 511316081160355852,
                      'unranked_game_channel': 511906353476927498,
                      'match_challenge_channels': [511316081160355852, 511906353476927498],
                      'game_request_channel': None,
                      'game_announce_channel': 505523961812090900,
                      'game_channel_category': None}

          }

lobbies = [{'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': True, 'notes': 'Newbie game - 1025 elo max'},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': True, 'remake_partial': True, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': False, 'remake_partial': True, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': False, 'remake_partial': True, 'notes': ''},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': False, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'},
           {'guild': 447883341463814144, 'size_str': '3v3', 'size': [3, 3], 'ranked': False, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'}]

ban_list = [
    436330481341169675,  # Mr Bucky
    481581027685564416,  # Shadow Knight
]

generic_teams_short = [('Home', ':stadium:'), ('Away', ':airplane:')]  # For two-team games
generic_teams_long = [('Sharks', ':shark:'), ('Owls', ':owl:'), ('Eagles', ':eagle:'), ('Tigers', ':tiger:'),
                      ('Bears', ':bear:'), ('Koalas', ':koala:'), ('Dogs', ':dog:'), ('Bats', ':bat:'),
                      ('Lions', ':lion:'), ('Cats', ':cat:'), ('Birds', ':bird:'), ('Spiders', ':spider:')]

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


def is_mod(ctx):

    if ctx.author.id == owner_id:
        return True
    mod_roles = guild_setting(ctx.guild.id, 'mod_roles')

    target_match = get_matching_roles(ctx.author, mod_roles)
    return len(target_match) > 0


async def is_user(ctx):

    if ctx.guild.id == server_ids['main']:
        minimum_role = discord.utils.get(ctx.guild.roles, name='Rider')
        if ctx.author.top_role < minimum_role:
            if ctx.invoked_with != 'help':
                await ctx.send('You must attain *"Rider"* role to use this command. Please participate in the server more.')
            return False
    return True


def is_user_check():
    # restrict commands to is_staff with syntax like @settings.is_staff_check()

    def predicate(ctx):
        return is_user(ctx)
    return commands.check(predicate)


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
        if is_mod(ctx):
            return True
        if ctx.message.channel.id in guild_setting(ctx.guild.id, 'bot_channels') + guild_setting(ctx.guild.id, 'bot_channels_private'):
            return True
        else:
            if ctx.invoked_with == 'help' and ctx.command.name != 'help':
                # Silently fail check when help cycles through every bot command for a check.
                pass
            else:
                channel_tags = [f'<#{chan_id}>' for chan_id in guild_setting(ctx.guild.id, 'bot_channels')]
                await ctx.send(f'This command can only be used in a designated ELO bot channel. Try: {" ".join(channel_tags)}')
            return False
    return commands.check(predicate)


def in_bot_channel_strict():
    async def predicate(ctx):
        if guild_setting(ctx.guild.id, 'bot_channels_strict') is None:
            if guild_setting(ctx.guild.id, 'bot_channels') is None:
                return True
            else:
                chan_list = guild_setting(ctx.guild.id, 'bot_channels')
        else:
            chan_list = guild_setting(ctx.guild.id, 'bot_channels_strict')
        if is_mod(ctx):
            return True
        if ctx.message.channel.id in chan_list + guild_setting(ctx.guild.id, 'bot_channels_private'):
            return True
        else:
            if ctx.invoked_with == 'help' and ctx.command.name != 'help':
                # Silently fail check when help cycles through every bot command for a check.
                pass
            else:
                # primary_bot_channel = chan_list[0]
                channel_tags = [f'<#{chan_id}>' for chan_id in chan_list]
                await ctx.send(f'This command can only be used in a designated bot spam channel. Try: {" ".join(channel_tags)}')
            return False
    return commands.check(predicate)
