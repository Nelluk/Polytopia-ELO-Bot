import modules.exceptions as exceptions
import logging
import datetime
from discord.ext import commands
# import discord
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

server_ids = {'main': 283436219780825088, 'polychampions': 447883341463814144, 'test': 478571892832206869, 'beta': 274660262873661442}
owner_id = 272510639124250625  # Nelluk
bot = None
run_tasks = True  # if set as False via command line option, tasks should check this and skip
team_elo_reset_date = '1/1/2019'

# bot invite URL https://discordapp.com/oauth2/authorize?client_id=484067640302764042&scope=bot
# bot invite URL for beta bot https://discordapp.com/oauth2/authorize?client_id=479029527553638401&scope=bot

config = {'default':
                     {'helper_roles': ['Helper'],
                      'mod_roles': ['Mod'],
                      'user_roles_level_4': [],  # power user/can do some fancy matchmaking things
                      'user_roles_level_3': ['@everyone'],  # full user, host/join anything
                      'user_roles_level_2': ['@everyone'],  # normal user, can't host all match sizes
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
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
                      'game_channel_categories': []},
        478571892832206869:                           # Nelluk Test Server (discord server ID)
                     {'helper_roles': ['testers'],
                      'mod_roles': ['role1'],
                      'display_name': 'Development Server',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '/',
                      'bot_channels_strict': [479292913080336397],
                      'bot_channels': [479292913080336397, 481558031281160212, 480078679930830849],  # 397 Bot Spam,  849 Admin Spam
                      # 'match_challenge_channels': [481558031281160212, 478571893272870913],  # 212 Testroom1
                      # 'newbie_message_channels': [481558031281160212, 396069729657421824, 413721247260868618],
                      'ranked_game_channel': 479292913080336397,
                      'unranked_game_channel': 481558031281160212,
                      'game_request_channel': 480078679930830849,
                      'game_announce_channel': 481558031281160212,
                      'game_channel_categories': [493149162238640161, 493149183155503105]},
        572885616656908288:                           # Ronin team Server
                     {'helper_roles': ['Team Co-Leader'],
                      'mod_roles': ['Admin', 'Team Leader'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Ronin',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      # 'bot_channels_private': [],
                      'bot_channels_strict': [573112316174925834],
                      'bot_channels': [573112316174925834],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'match_challenge_channels': [],
                      'game_request_channel': None,  # $staffhelp output
                      'game_announce_channel': None,  # elo-drafts
                      'game_channel_categories': [572888210959499264]},
        448323425971470336:                           # Sparkies team server
                     {'helper_roles': ['Team Co-Leader'],
                      'mod_roles': ['Team Leader'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Sparkies',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [448493503702630400],
                      'bot_channels': [448493503702630400],
                      'game_channel_categories': [574712176535666689]},
        492753802450173987:                           # Crawfish team server
                     {'helper_roles': ['Crawfish'],
                      'mod_roles': ['Horse of the Sea', 'Lord of the Deep'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Crawfish',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [573126655539937306],
                      'bot_channels': [573126655539937306],
                      'game_channel_categories': [573125716535803905]},
        465333914312114177:                           # Bombers team server
                     {'helper_roles': ['Bomber'],
                      'mod_roles': ['Leader', 'Coleader'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Bombers',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [573136597894168577],
                      'bot_channels': [573136597894168577],
                      'game_channel_categories': [573135845221990401]},
        466331712591233035:                           # Wildfire team server
                     {'helper_roles': ['Wildfire 🔥'],
                      'mod_roles': ['Mood', 'Admoon', 'Inner Circle'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Wildfire',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [573139313097965568],
                      'bot_channels': [573139313097965568],
                      'game_channel_categories': [476824275852984331]},
        448608100199563274:                           # Lightning team server
                     {'helper_roles': ['Lightning Team Member'],
                      'mod_roles': ['Leader', 'Co Leader'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Lightning',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [448608290012790794],
                      'bot_channels': [448608290012790794],
                      'game_channel_categories': [573168256001769502]},
        573171339620515862:                           # Mallards team server
                     {'helper_roles': ['@everyone'],
                      'mod_roles': ['Mod'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Mallards',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [573312345699516427],
                      'bot_channels': [573312345699516427],
                      'game_channel_categories': [573304161911963648]},
        573272736085049375:                           # Cosmonauts team server
                     {'helper_roles': ['@everyone'],
                      'mod_roles': ['The Emperess'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Cosmonauts',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [573387785306767360],
                      'bot_channels': [573387785306767360],
                      'game_channel_categories': [573272941517602819]},
        541012224534118431:                           # Plague team server
                     {'helper_roles': ['The Plague'],
                      'mod_roles': ['Leader', 'Co-Leader', 'Sleeping Giant'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Plague',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [573894372173807616],
                      'bot_channels': [573894372173807616],
                      'game_channel_categories': [573895174309150745]},
        447883341463814144:                           # Polychampions
                     {'helper_roles': ['Helper', 'ELO-Helper', 'Team Leader'],
                      'mod_roles': ['Mod'],
                      'user_roles_level_4': ['Team Co-Leader', 'ELO Hero'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'PolyChampions',
                      'require_teams': True,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': True,
                      'bot_channels_private': [487304043786665986, 469027618289614850, 531636068739710997, 447902433964851210],  # 986 elo-staff-talk, 850 dont-timeout, 997 novas, 210 s4-drafts
                      'bot_channels_strict': [487303307224940545, 448317497473630229],  # 545 elo-commands, 229 bot-commands
                      'bot_channels': [487303307224940545, 448317497473630229, 452639822616723457],  # 457 elo-chalenges
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'match_challenge_channels': [452639822616723457],  # elo-challenges
                      'game_request_channel': 487304043786665986,  # $staffhelp output
                      'game_announce_channel': 487302138704429087,  # elo-drafts
                      'game_channel_categories': [488421911529914368, 514141474229846018, 519131761733795841, 550414365044637712, 563717211362164736, 568093671912636420, 574669105752440842]},  # elo-games-i, ii, iii, iv, v, vi, vii
          283436219780825088:                           # Main Server
                     {'helper_roles': ['ELO-Helper', 'Bot Master', 'Director'],
                      'mod_roles': ['MOD', 'Manager'],
                      'user_roles_level_4': ['Archer', 'Defender', 'Ship', 'Catapult', 'Knight', 'Swordsman', 'Tridention', 'Battleship', 'Mind Bender', 'Giant', 'Crab', 'Dragon', 'ELO Hero'],
                      'user_roles_level_3': ['ELO Player', 'ELO Veteran'],  # full user
                      'user_roles_level_2': ['Rider', 'Boat', 'ELO Rookie'],  # normal user
                      'user_roles_level_1': ['Member', 'Warrior'],  # restricted user/newbie
                      'display_name': 'Polytopia',
                      'allow_uneven_teams': True,
                      'require_teams': False,
                      'allow_teams': False,
                      'max_team_size': 1,
                      'command_prefix': '$',
                      'include_in_global_lb': True,
                      'bot_channels_private': [418175357137453058],  # 058 testchamber
                      'bot_channels_strict': [403724174532673536],  # 536 BotCommands
                      'bot_channels': [403724174532673536, 511316081160355852, 511906353476927498, 396069729657421824],  # 498 unranked-games, 852 ranked-games, 824 multi-discussion
                      'newbie_message_channels': [396069729657421824, 413721247260868618],  # multi-discussion, #friend-codes
                      'ranked_game_channel': 511316081160355852,
                      'unranked_game_channel': 511906353476927498,
                      'match_challenge_channels': [511316081160355852, 511906353476927498],
                      'game_request_channel': 418175357137453058,
                      'game_announce_channel': 505523961812090900,
                      'game_channel_categories': [546527176380645395, 551747728548298758, 551748058690617354, 560104969580183562]},
        274660262873661442:                           # Beta Server
                     {'helper_roles': ['ELO Helper', 'Bot Master', 'iOS', 'Android'],
                      'mod_roles': ['MOD', 'Manager'],
                      'display_name': 'Polytopia Beta',
                      'allow_uneven_teams': True,
                      'require_teams': False,
                      'allow_teams': False,
                      'max_team_size': 4,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [514473680529784853],
                      'bot_channels': [313387761405263873],
                      # 'newbie_message_channels': [396069729657421824, 413721247260868618, 418326008526143508, 418326044077064192],  # multi-discussion, friend codes NA/euro/asia
                      # 'ranked_game_channel': 511316081160355852,
                      # 'unranked_game_channel': 511906353476927498,
                      # 'match_challenge_channels': [511316081160355852, 511906353476927498],
                      'game_request_channel': None,
                      # 'game_announce_channel': 505523961812090900,
                      'game_channel_categories': []},
        507848578048196614:                           # Jets Server
                     {'helper_roles': ['Co-Leader'],
                      'mod_roles': ['Admin', 'Bot Admin'],
                      'user_roles_level_4': ['', 'Jets'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Jets',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [520307432166260746],
                      'bot_channels': [520307432166260746],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'match_challenge_channels': [],
                      'game_request_channel': None,  # $staffhelp output
                      'game_announce_channel': None,  # elo-drafts
                      'game_channel_categories': [507946542024359938]},  # Jets vs Jets
        # 568090839545413635:                           # Polympire, small server run by WhyDoYouDide
        #              {'helper_roles': ['Bot-helper'],
        #               'mod_roles': ['Admin', 'Bot-mod'],
        #               'user_roles_level_4': ['@everyone'],  # power user
        #               'user_roles_level_3': ['@everyone'],  # power user
        #               'user_roles_level_2': ['@everyone'],  # normal user
        #               'user_roles_level_1': ['@everyone'],  # restricted user/newbie
        #               'display_name': 'Polympire',
        #               'require_teams': False,
        #               'allow_teams': True,
        #               'allow_uneven_teams': True,
        #               'max_team_size': 6,
        #               'command_prefix': '$',
        #               'include_in_global_lb': False,
        #               'bot_channels_private': [],
        #               'bot_channels_strict': [568091652506255377],
        #               'bot_channels': [568091652506255377],
        #               'ranked_game_channel': None,
        #               'unranked_game_channel': None,
        #               'match_challenge_channels': [],
        #               'game_channel_categories': [568404156352561163]},
        576962604124209180:                           # Small server run by PinkPigmyPuff#7107
                     {'helper_roles': ['ELO-Helper'],
                      'mod_roles': ['MOD'],
                      'user_roles_level_4': ['PolyPlayer!'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'PinkPigmyPuff Polytopia',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 2,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [579779127641374720],
                      'bot_channels': [579779127641374720],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_announce_channel': 579728109842989058,
                      'match_challenge_channels': [],
                      'game_channel_categories': []},
        419286093360529420:                           # Pooltopia, run by Bomber
                     {'helper_roles': ['pooltopia'],
                      'mod_roles': ['mod', 'pooltopian'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Pooltopia',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 3,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [511371377966579712],
                      'bot_channels': [511371377966579712],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'match_challenge_channels': [],
                      'game_channel_categories': []}

          }

lobbies = [{'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': True, 'notes': '**Newbie game** - 1075 elo max'},
           {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': False, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': True, 'remake_partial': False, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': False, 'remake_partial': True, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': False, 'remake_partial': False, 'notes': ''},
           # {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 531567102042308609], 'notes': 'Newbie 2v2 game, Novas welcome <:novas:531568047824306188>'},
           # {'guild': 447883341463814144, 'size_str': '3v3', 'size': [3, 3], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 531567102042308609], 'notes': 'Newbie 3v3 game, Novas welcome <:novas:531568047824306188>'},
           # {'guild': 447883341463814144, 'size_str': '3v3', 'size': [3, 3], 'ranked': True, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'},
           {'guild': 478571892832206869, 'size_str': '3v3', 'size': [3, 3], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 480350546172182530], 'notes': 'Test lobby for role locks'},
           {'guild': 478571892832206869, 'size_str': 'FFA', 'size': [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], 'ranked': True, 'exp': 95, 'remake_partial': True, 'notes': 'Open to all'}]

discord_id_ban_list = [
    436330481341169675,  # Mr Bucky
    481581027685564416,  # Shadow Knight
    # 396699990577119244,  # Skrealder
    # 481525222072254484,  # testaccount1
    342341358218117121,  # Caesar Augustas Trajan
    327433644589187072,  # Spacebar/Robit
    359831073737146369,  # Epi
    427018182310756352,  # Freeze
]

poly_id_ban_list = [
    'MvSRS2t5vWLUyyuu',  # Caesar Augustas Trajan
    'AfMDTSO3yareZN2E',  # Freeze
    'qIqw1okeZZgaFpUL',  # Remalin (skre alt)
    '815D2hK94mN7StoL',  # Skrealder
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


def servers_included_in_global_lb():
    return [server for server, settings in config.items() if settings.get('include_in_global_lb', False)]


def get_matching_roles(discord_member, list_of_role_names):
        # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.polytopia_id
        member_roles = [x.name for x in discord_member.roles]
        return set(member_roles).intersection(list_of_role_names)


levels_info = ('***Level 1*** - *Join ranked games up to 3 players, unranked games up to 6 players. Host games up to 3 players.*\n\n'
               '***Level 2*** - *Join ranked games up to 6 players, unranked games up to 12 players. Host ranked games up to 4 players, unranked games up to 6 players.* (__Complete 2 games to attain, ranked or unranked__)\n\n'
               '***Level 3*** - *No restrictions on games* (__Complete 10 games to attain, ranked or unranked__)\n')


def get_user_level(ctx, user=None):
    user = ctx.author if not user else user

    if user.id == owner_id:
        return 7
    if is_mod(ctx, user=user):
        return 6
    if is_staff(ctx, user=user):
        return 5
    if get_matching_roles(user, guild_setting(ctx.guild.id, 'user_roles_level_4')):
        return 4  # advanced matchmaking abilities (leave own match, join others to match). can use settribes in bulk
    if get_matching_roles(user, guild_setting(ctx.guild.id, 'user_roles_level_3')):
        return 3  # host/join any
    if get_matching_roles(user, guild_setting(ctx.guild.id, 'user_roles_level_2')):
        return 2  # join ranked games up to 6p, unranked up to 12p
    if get_matching_roles(user, guild_setting(ctx.guild.id, 'user_roles_level_1')):
        return 1  # join ranked games up to 3p, unranked up to 6p. no hosting
    return 0


def is_staff(ctx, user=None):
    user = ctx.author if not user else user

    if user.id == owner_id:
        return True
    helper_roles = guild_setting(ctx.guild.id, 'helper_roles')
    mod_roles = guild_setting(ctx.guild.id, 'mod_roles')

    target_match = get_matching_roles(user, helper_roles + mod_roles)
    return len(target_match) > 0


def is_mod(ctx, user=None):
    user = ctx.author if not user else user

    if ctx.author.id == owner_id:
        return True
    mod_roles = guild_setting(ctx.guild.id, 'mod_roles')

    target_match = get_matching_roles(user, mod_roles)
    return len(target_match) > 0


# async def is_user(ctx, user=None):
#     user = ctx.author if not user else user

#     if ctx.guild.id == server_ids['main']:
#         minimum_role = discord.utils.get(ctx.guild.roles, name='Rider')
#         if user.top_role < minimum_role:
#             if ctx.invoked_with != 'help':
#                 await ctx.send('You must attain *"Rider"* role to use this command. Please participate in the server more.')
#             return False
#     return True


# def is_user_check():
#     # restrict commands to is_staff with syntax like @settings.is_staff_check()

#     def predicate(ctx):
#         return is_user(ctx)
#     return commands.check(predicate)


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
        return ctx.guild.id == server_ids['polychampions'] or ctx.guild.id == server_ids['test']
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
