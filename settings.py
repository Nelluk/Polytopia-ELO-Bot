import modules.exceptions as exceptions
import logging
import datetime
from discord.ext import commands
import discord
import re
from types import MappingProxyType
from runtime_config import get_runtime_profile
from modules.application_command_policy import (
    build_capability_policy,
    policy_from_server_settings,
)
from modules import guild_configuration_runtime
from modules.elo_jobs import elo_job_coordinator

logger = logging.getLogger('polybot.' + __name__)

runtime_profile = get_runtime_profile()
server_settings = runtime_profile.server_settings
discord_key = runtime_profile.discord_token
psql_user = runtime_profile.database_user
psql_db = runtime_profile.database_name
psql_password = runtime_profile.database_password
psql_host = runtime_profile.database_host
psql_port = runtime_profile.database_port
owner_id = runtime_profile.owner_id
superuser_ids = runtime_profile.superuser_ids
pastebin_key = runtime_profile.pastebin_key
# github test change
server_ids = server_settings.server_shortcut_ids
# server_ids = {'main': 283436219780825088, 'polychampions': 447883341463814144, 'test': 478571892832206869, 'beta': 274660262873661442}
bot_id_beta = 479029527553638401
bot_id = runtime_profile.expected_bot_id

guild_configuration_source = runtime_profile.guild_configuration_source
# Database-selected processes expose no static guild allowlist or settings
# while their exact startup snapshot is still being validated.
config = (
    server_settings.server_list
    if guild_configuration_source == 'static'
    else MappingProxyType({})
)
application_command_policy = (
    policy_from_server_settings(
        server_settings,
        runtime_profile.allowed_guild_ids,
    )
    if guild_configuration_source == 'static'
    else build_capability_policy({}, runtime_profile.allowed_guild_ids)
)
bot = None
run_tasks = runtime_profile.background_tasks_enabled
maintenance_mode = False  # if set as True bot will ignore all commands (TODO: respond to all commands?)
_database_guild_configuration = None
team_elo_reset_date = '1/1/2020'

moonrise_reset_date = datetime.date(2020, 12, 1)
elo_calc_v2_date = datetime.date(2020, 8, 2)  # tweaked elo calc Aug 2, 2020

emoji_join_game = '⚔️'
re_join_game = re.compile(fr'join game (\d+) by reacting with {emoji_join_game}')
# Have to do weird double {{braces}} to mix f-strings and raw strings, https://stackoverflow.com/a/45527907/1281743

# bot invite URL https://discordapp.com/oauth2/authorize?client_id=484067640302764042&scope=bot
# with perms: https://discord.com/api/oauth2/authorize?client_id=484067640302764042&permissions=388176&scope=bot
# bot invite URL for beta bot https://discordapp.com/oauth2/authorize?client_id=479029527553638401&scope=bot
# with perms: https://discord.com/api/oauth2/authorize?client_id=479029527553638401&permissions=388176&scope=bot


lobbies = [{'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': True, 'notes': '**Newbie game** - 1075 elo max'},
           {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': False, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': True, 'remake_partial': False, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': False, 'remake_partial': True, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': False, 'remake_partial': False, 'notes': ''},
           # {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [1327424071960367125, 1327424071960367125], 'notes': 'Newbie 2v2 game, Novas welcome <:novas:1327341237665005669>'},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [1327424071960367125, 1327424071960367125], 'notes': 'Newbie 2 v 2 game, Novas welcome <:novas:1327341237665005669>'},    
        #    {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841367103602768, 696841359616901150], 'notes': '**Newbie game**: Nova Red vs Nova Blue'},
        #    {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841359616901150, 696841367103602768], 'notes': '**Newbie game**: Nova Blue vs Nova Red'},
        #    {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841367103602768, 696841359616901150], 'notes': '**Newbie game**: Nova Red vs Nova Blue'},
        #    {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841359616901150, 696841367103602768], 'notes': '**Newbie game**: Nova Blue vs Nova Red'},
        #    {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841367103602768, 696841359616901150], 'notes': '**Newbie game**: Nova Red vs Nova Blue'},
        #    {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841359616901150, 696841367103602768], 'notes': '**Newbie game**: Nova Blue vs Nova Red'},
        #    {'guild': 447883341463814144, 'size_str': '3v3', 'size': [3, 3], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 1327424071960367125], 'notes': 'Newbie 3v3 game, Novas welcome <:novas:1327341237665005669>'},
        #    {'guild': 447883341463814144, 'size_str': '3v3', 'size': [3, 3], 'ranked': True, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'},
        #    {'guild': 478571892832206869, 'size_str': '3v3', 'size': [3, 3], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 480350546172182530], 'notes': ''},
        #    {'guild': 478571892832206869, 'size_str': 'FFA', 'size': [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], 'ranked': True, 'exp': 95, 'remake_partial': True, 'notes': 'Open to all'},
           {'guild': 722870446252359751, 'size_str': '1v1', 'size': [1, 1], 'ranked': False, 'exp': 95, 'remake_partial': False, 'notes': 'Unranked'},
        #    {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': False, 'notes': 'QT 0 wins players only. Small map. Drylands. Yadakk mirror (Oumaji mirror as free alternative)'}
           ]

discord_id_ban_list = [
    493503844865671187,  # BlueberryCraft#9080 (star hacker)
    436330481341169675,  # Mr Bucky
    481581027685564416,  # Shadow Knight
    # 396699990577119244,  # Skrealder
    # 481525222072254484,  # testaccount1
    342341358218117121,  # Caesar Augustas Trajan
    327433644589187072,  # Spacebar/Robit
    359831073737146369,  # Epi
    427018182310756352,  # Freeze
    386549614964244481,  # logs#4361
    # 313427349775450112,  # SouthPenguinJay#3692
    616737820261875721,  # CoolGuyNotFoolGuy#0498 troll who blatantly lied about game confirmations
    735809555837091861,  # XaeroXD8401  points cheater
    484067640302764042,  # polyelo himself
    479029527553638401,  # polyelo beta herself
    590055026643435544,  # mikeyphelps#1744 aka dfgigy - star hacking
    430699598483554325,  # Mewtic - offensive troll
    # 630118686589714434,  # Joyoung
    # 662634965527429130,  # Aido
    # 325596275007422466,  # Resetti
    # 259701692835037184,  # Justluck
    # 761544966710231050,  # Kirby
    799078333149085716,  # Ciruelatron - alting and then attempting to evade ELO Ban
]

poly_id_ban_list = [
    'pKUaK61nd2BzNY65',  # BlueberryCraft#9080 (star hacker)
    'MvSRS2t5vWLUyyuu',  # Caesar Augustas Trajan
    'AfMDTSO3yareZN2E',  # Freeze
    # 'qIqw1okeZZgaFpUL',  # Remalin (skre alt)
    # '815D2hK94mN7StoL',  # Skrealder
    'fOEjbnrzO9tg1QYT',  # Doggo#8422
    # '8ZWg85d9PlogdY1H',  # Stupid#7043
    'R5NregRkLycUsq7C',  # Just7609
    '9x85fWIxxkLyOMem',  # logs#4361
    # 'JU1Zb9jGO4H1I4Ls',  # SouthPenguinJay#3692
    'MhJJohJENaeBUz7H',  # CoolGuyNotFoolGuy#0498
    '20aih8HH5IcromHX',  # XaeroXD8401
    'U4x51BhGC2kGp5rv',  # lil'omen 746795654994722901 and Atlas 456142908303867935
    'JRZhrtFOX2Gv0Vv0',  # D’Escanor#9505 and Exiosking#9132
    'TLyEvHTBidd21Vl9',  # mikeyphelps#1744 aka dfgigy
    'm6U1t7dXUHbLYFC1',  # Mewtic
]


generic_teams_short = [('Home', ':stadium:'), ('Away', ':airplane:')]  # For two-team games
generic_teams_short_novas = [('Nova Red', ':red_circle:'), ('Nova Blue', ':blue_circle:')]  # For two-team games with both Novas
generic_teams_long = [('Sharks', ':shark:'), ('Owls', ':owl:'), ('Eagles', ':eagle:'), ('Tigers', ':tiger:'),
                      ('Bears', ':bear:'), ('Koalas', ':koala:'), ('Dogs', ':dog:'), ('Bats', ':bat:'),
                      ('Lions', ':lion:'), ('Cats', ':cat:'), ('Birds', ':bird:'), ('Spiders', ':spider:'),
                      ('Camels', ':camel:'), ('Bugs', ':bug:'), ('Whales', ':whale:'), ('Unicorns', ':unicorn:')]

max_game_size = 16

map_types = ['Lakes', 'Pangea', 'Dryland', 'Archipelago', 'Water World', 'Continents']

assert bool(len(generic_teams_long) == max_game_size), 'generic_teams_long must have a number of entries equal to max_game_size.'

date_cutoff = datetime.datetime.today() - datetime.timedelta(days=365)  # Players who haven't played since cutoff are not included in leaderboards

league_tiers = [
    # Exclusively used for PolyChampions leagues
    (1, 'Platinum'),
    (2, 'Gold'),
    (3, 'Silver'),
    (4, 'Bronze'),
    (5, 'Copper'),
    (6, 'Stone'),
    (7, 'Paper')
]

def tier_lookup(lookup_key):
    logger.debug(f'tier_lookup by "{lookup_key}"')
    
    try:
        tier_number = int(lookup_key)
        tier_name = None
        logger.debug('Searching by tier number')
    except ValueError:
        tier_name = lookup_key
        tier_number = None
        logger.debug('Searching by tier name')

    if tier_number:
        for league_tier in league_tiers:
            if league_tier[0] == tier_number:
                return league_tier
    if tier_name:
        for league_tier in league_tiers:
            if tier_name.upper() in league_tier[1].upper():
                return league_tier
            
    logger.warn(f'Unsuccessful lookup in tier_lookup')
    raise exceptions.NoMatches('No matching tier found.')


def get_setting(setting_name):
    if _database_guild_configuration is not None:
        raise exceptions.CheckFailedError(
            'A guild ID is required for database-backed configuration.'
        )
    return config['default'][setting_name]


def guild_setting(guild_id: int, setting_name: str):
    # if guild_id = None, default block will be used

    if guild_id:

        try:
            settings_obj = config[guild_id]
        except KeyError:
            logger.warning(f'Unknown guild id {guild_id} requested for setting name {setting_name}.')
            raise exceptions.CheckFailedError('Unauthorized: This guild is not in the config.ini file.')
            # return config['default'][setting_name]

        try:
            return settings_obj[setting_name]
        except KeyError:
            if _database_guild_configuration is not None:
                logger.error(
                    'Complete database guild configuration %s is missing %s.',
                    guild_id,
                    setting_name,
                )
                raise exceptions.CheckFailedError(
                    'The active guild configuration is incomplete.'
                )
            return config['default'][setting_name]

    else:
        if _database_guild_configuration is not None:
            raise exceptions.CheckFailedError(
                'A guild ID is required for database-backed configuration.'
            )
        return config['default'][setting_name]


def guild_configuration_ready() -> bool:
    """Return whether the explicitly selected source is published for reads."""

    return (
        guild_configuration_source == 'static'
        or _database_guild_configuration is not None
    )


def database_guild_configuration(guild_id: int):
    """Return one immutable published database record, or ``None``."""

    if (
            guild_configuration_source != 'database'
            or _database_guild_configuration is None
    ):
        return None
    try:
        normalized = int(guild_id)
    except (TypeError, ValueError):
        return None
    return _database_guild_configuration.guilds.get(normalized)


def activate_database_guild_configuration(snapshot) -> None:
    """Atomically expose one already validated immutable database snapshot."""

    global _database_guild_configuration, config, application_command_policy
    if guild_configuration_source != 'database':
        raise RuntimeError(
            'Database guild configuration was not selected before startup.'
        )
    if not isinstance(
            snapshot,
            guild_configuration_runtime.GuildConfigurationRuntimeSnapshot,
    ):
        raise TypeError('A validated database runtime snapshot is required.')
    if _database_guild_configuration is not None:
        if _database_guild_configuration != snapshot:
            raise RuntimeError(
                'Database guild configuration is already published.'
            )
        return
    # Commands are still startup-gated here. Publish the single source
    # reference first; the compatibility mapping and command policy are views
    # contained in that same immutable snapshot.
    _database_guild_configuration = snapshot
    config = snapshot.legacy_config
    application_command_policy = snapshot.command_policy


def configured_role_ids(guild_id: int, setting_name: str) -> tuple[int, ...]:
    """Return stable configured IDs when database authority is active."""

    snapshot = _database_guild_configuration
    if snapshot is None:
        return ()
    try:
        return snapshot.guilds[int(guild_id)].role_ids[setting_name]
    except KeyError as exc:
        raise exceptions.CheckFailedError(
            'The active guild role configuration is unavailable.'
        ) from exc


def resolve_configured_role(guild, setting_name: str, index: int = 0):
    """Resolve one configured role by stable ID or the retained static name."""

    role_ids = configured_role_ids(int(guild.id), setting_name)
    if role_ids:
        if index < 0 or index >= len(role_ids):
            return None
        get_role = getattr(guild, 'get_role', None)
        if callable(get_role):
            return get_role(role_ids[index])
        return discord.utils.get(getattr(guild, 'roles', ()), id=role_ids[index])
    configured = guild_setting(int(guild.id), setting_name)
    values = (
        tuple(configured or ())
        if setting_name != 'inactive_role'
        else (() if configured is None else (configured,))
    )
    if index < 0 or index >= len(values):
        return None
    return discord.utils.get(getattr(guild, 'roles', ()), name=values[index])


def servers_included_in_global_lb():
    # returns [int, int] list of server IDs for the servers that have include_in_global_lb marked as True
    return [server for server, settings in config.items() if settings.get('include_in_global_lb', False)]


def get_matching_roles(discord_member, configured_roles):
    """Match configured role names/IDs without broadening either source."""

    member_roles = tuple(getattr(discord_member, 'roles', ()) or ())
    member_names = {str(role.name) for role in member_roles}
    member_ids = {int(role.id) for role in member_roles}
    return {
        configured
        for configured in configured_roles
        if (
            isinstance(configured, int) and not isinstance(configured, bool)
            and configured in member_ids
        ) or (
            isinstance(configured, str) and configured in member_names
        )
    }


def _permission_roles(guild_id: int, setting_name: str):
    role_ids = configured_role_ids(guild_id, setting_name)
    return role_ids or guild_setting(guild_id, setting_name)


levels_info = ('***Level 1*** - *Join ranked games up to 3 players, unranked games up to 6 players. Host games up to 3 players.*\n\n'
               '***Level 2*** - *Join ranked games up to 6 players, unranked games up to 12 players. Host ranked games up to 4 players, unranked games up to 6 players.* (__Complete 2 games to attain, ranked or unranked__)\n\n'
               '***Level 3*** - *No restrictions on games* (__Complete 10 games to attain, ranked or unranked__)\n')


def get_user_level(member):

    if member.id == owner_id:
        return 7
    if is_mod(member):
        return 6
    if is_staff(member):
        return 5
    if get_matching_roles(member, _permission_roles(member.guild.id, 'user_roles_level_4')):
        return 4  # advanced matchmaking abilities (leave own match, join others to match). can use settribes in bulk
    if get_matching_roles(member, _permission_roles(member.guild.id, 'user_roles_level_3')):
        return 3  # host/join any
    if get_matching_roles(member, _permission_roles(member.guild.id, 'user_roles_level_2')):
        return 2  # join ranked games up to 6p, unranked up to 12p
    if get_matching_roles(member, _permission_roles(member.guild.id, 'user_roles_level_1')):
        return 1  # join ranked games up to 3p, unranked up to 6p. no hosting
    return 0


def can_user_join_game(user_level: int, game_size: int, is_ranked: bool = True, is_host: bool = True):
    # return bool_permission_given, str_error_message
    if is_host:
        if user_level <= 1 and game_size > 3:
            return False, f'You can only host games with a maximum of 3 players.\n{levels_info}'
        if user_level <= 2:
            if game_size > 4 and is_ranked:
                return False, f'You can only host ranked games of up to 4 players. More active players have permissons to host large games.\n{levels_info}'
            if game_size > 6:
                return False, f'You can only host unranked games of up to 6 players. More active players have permissons to host large games.\n{levels_info}'

    if user_level <= 1:
        if (is_ranked and game_size > 3) or (not is_ranked and game_size > 6):
            return False, f'You are a restricted user (*level 1*) - complete a few more ELO games to have more permissions.\n{levels_info}'
        if user_level <= 2:
            if (is_ranked and game_size > 6) or (not is_ranked and game_size > 12):
                return False, f'You are a restricted user (*level 2*) - complete a few more ELO games to have more permissions.\n{levels_info}'

    return True, None  # Game allowed


def is_staff(member):

    if member.id == owner_id:
        return True
    helper_roles = _permission_roles(member.guild.id, 'helper_roles')
    mod_roles = _permission_roles(member.guild.id, 'mod_roles')

    target_match = get_matching_roles(member, helper_roles + mod_roles)
    return len(target_match) > 0


def is_mod(member):

    if member.id == owner_id:
        return True
    mod_roles = _permission_roles(member.guild.id, 'mod_roles')

    target_match = get_matching_roles(member, mod_roles)
    return len(target_match) > 0


def is_superuser(member):
    return int(member.id) in superuser_ids


def is_superuser_check():
    def predicate(ctx):
        return is_superuser(ctx.author)
    return commands.check(predicate)


def is_staff_check():
    # restrict commands to is_staff with syntax like @settings.is_staff_check()

    def predicate(ctx):
        return is_staff(ctx.author)
    return commands.check(predicate)


def is_mod_check():
    # restrict commands to is_staff with syntax like @settings.is_mod_check()

    def predicate(ctx):
        return is_mod(ctx.author)
    return commands.check(predicate)


def draft_check():
    def predicate(ctx: commands.Context):
        return is_mod(ctx.author) or is_staff(ctx.author) or discord.utils.get(ctx.guild.roles, name='Drafter') in ctx.author.roles
    return commands.check(predicate)


def on_polychampions():

    def predicate(ctx):
        return ctx.guild.id == server_ids['polychampions'] or ctx.guild.id == server_ids['test']
    return commands.check(predicate)


def guild_has_setting(setting_name: str):

    def predicate(ctx):
        return bool(guild_setting(ctx.guild.id, setting_name))
    return commands.check(predicate)


def in_bot_channel():
    async def predicate(ctx):
        if guild_setting(ctx.guild.id, 'bot_channels') is None:
            return True
        if is_mod(ctx.author):
            return True
        if ctx.message.channel.id in guild_setting(ctx.guild.id, 'bot_channels') + guild_setting(ctx.guild.id, 'bot_channels_private'):
            return True
        else:
            if ctx.invoked_with == 'help' and ctx.command.name != 'help':
                # Silently fail check when help cycles through every bot command for a check.
                pass
            else:
                channel_tags = [f'<#{chan_id}>' for chan_id in guild_setting(ctx.guild.id, 'bot_channels')]
                logger.debug(f'in_bot_channel: {channel_tags} {guild_setting(ctx.guild.id, "bot_channels")}')
                await ctx.send(f'This command can only be used in a designated ELO bot channel. Try: {" ".join(channel_tags)}')
            return False
    return commands.check(predicate)


async def is_bot_channel_strict(ctx):
    if guild_setting(ctx.guild.id, 'bot_channels_strict') is None:
        if guild_setting(ctx.guild.id, 'bot_channels') is None:
            return True
        else:
            chan_list = guild_setting(ctx.guild.id, 'bot_channels')
    else:
        chan_list = guild_setting(ctx.guild.id, 'bot_channels_strict')
    if is_mod(ctx.author):
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


def in_bot_channel_strict():
    async def predicate(ctx):
        return await is_bot_channel_strict(ctx)
    return commands.check(predicate)
