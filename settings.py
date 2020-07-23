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
# server_ids = {'main': 283436219780825088, 'polychampions': 478571892832206869, 'test': 478571892832206869, 'beta': 274660262873661442}

owner_id = 272510639124250625  # Nelluk
bot = None
run_tasks = True  # if set as False via command line option, tasks should check this and skip
team_elo_reset_date = '1/1/2020'

# bot invite URL https://discordapp.com/oauth2/authorize?client_id=484067640302764042&scope=bot
# bot invite URL for beta bot https://discordapp.com/oauth2/authorize?client_id=479029527553638401&scope=bot

config = {'default':
                     {'helper_roles': ['Helper'],
                      'mod_roles': ['Mod'],
                      'user_roles_level_4': [],  # power user/can do some fancy matchmaking things
                      'user_roles_level_3': ['@everyone'],  # full user, host/join anything
                      'user_roles_level_2': ['@everyone'],  # normal user, can't host all match sizes
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'inactive_role': None,
                      'display_name': 'Unknown Server',
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
                     {'mod_roles': ['role1'],
                      # 'helper_roles': ['role1'],
                      'inactive_role': 'Inactive',
                      'display_name': 'Development Server',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      # 'max_team_size': 6,
                      'max_team_size': 1,
                      'command_prefix': '/',
                      'bot_channels_strict': [479292913080336397],
                      'bot_channels': [479292913080336397, 481558031281160212, 480078679930830849],  # 397 Bot Spam,  849 Admin Spam
                      # 'match_challenge_channels': [481558031281160212, 478571893272870913],  # 212 Testroom1
                      # 'newbie_message_channels': [481558031281160212, 480078679930830849],
                      # 'ranked_game_channel': 479292913080336397,
                      # 'unranked_game_channel': 481558031281160212,
                      'game_request_channel': 480078679930830849,
                      # 'game_announce_channel': 481558031281160212,
                      'game_channel_categories': [493149162238640161, 493149183155503105]},
        447883341463814144:                           # Polychampions
                     {'helper_roles': ['Mod', 'Helper', 'ELO-Helper', 'Team Leader', 'Nova Coordinator'],
                      'mod_roles': ['Mod'],
                      'user_roles_level_4': ['Team Co-Leader', 'ELO Hero', 'ELO Elite', 'ELO Master', 'ELO Titan', 'Event Organizer', 'Team Recruiter'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'inactive_role': 'Inactive',
                      'display_name': 'PolyChampions',
                      'require_teams': True,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': True,
                      'bot_channels_private': [469027618289614850, 531636068739710997, 447902433964851210, 627601961046507541, 721522509782057051, 448116504563810305],  # 986 elo-staff-talk, 850 dont-timeout, 997 novas, 210 s4-drafts
                      'bot_channels_strict': [487303307224940545, 448317497473630229],  # 545 elo-commands, 229 bot-commands
                      'bot_channels': [487303307224940545, 448317497473630229],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'match_challenge_channels': [],
                      'game_request_channel': 448116504563810305,  # just-bot-things
                      'game_announce_channel': 487302138704429087,  # elo-drafts
                      'game_channel_categories': [488421911529914368, 514141474229846018, 519131761733795841, 550414365044637712, 563717211362164736, 568093671912636420, 574669105752440842, 689093537131790388]},  # elo-games-i, ii, iii, iv, v, vi, vii, viii
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
                      'mod_roles': ['Leadership', 'Lord of the Deep'],
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
                      'game_channel_categories': [682763015421952128]},
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
        615300195093184512:                           # Dragons/Narwhals team server
                     {'helper_roles': ['Co-Leader🐉'],
                      'mod_roles': ['Leader🐉', 'Co-Leader🐉'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Team Dragons',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_strict': [616757304485871712],
                      'bot_channels': [616757304485871712],
                      'game_channel_categories': [616073517410287721]},
        283436219780825088:                           # Main Server
                     {'helper_roles': ['ELO-Helper', 'Bot Master'],
                      'mod_roles': ['Admin', 'Manager'],
                      'user_roles_level_4': ['Moderator', 'Archer', 'Ice Archer', 'Defender', 'Ship', 'Catapult', 'Polytaur', 'Battle Sled', 'Swordsman', 'Tridention', 'Knight', 'Ice Fortress', 'Battleship', 'Navalon', 'Mind Bender', 'Giant', 'Crab', 'Gaami', 'Dragon', 'ELO Hero', 'ELO Elite', 'ELO Master', 'ELO Titan'],
                      'user_roles_level_3': ['ELO Player', 'ELO Veteran'],  # full user
                      'user_roles_level_2': ['Rider', 'Boat', 'ELO Rookie'],  # normal user
                      'user_roles_level_1': ['Member', 'Warrior'],  # restricted user/newbie
                      'display_name': 'Polytopia Main',
                      'allow_uneven_teams': True,
                      'require_teams': False,
                      'allow_teams': False,
                      'max_team_size': 2,
                      'command_prefix': '$',
                      'include_in_global_lb': True,
                      'bot_channels_private': [418175357137453058, 403724174532673536],  # 058 testchamber, 536 bot-commands
                      'bot_channels_strict': [635091071717867521],  # 521 elo-bot-commands
                      'bot_channels': [635091071717867521, 403724174532673536, 511316081160355852, 511906353476927498],  # 498 unranked-games, 852 ranked-games
                      'newbie_message_channels': [396069729657421824, 413721247260868618],  # multi-discussion, friend-codes
                      'ranked_game_channel': 511316081160355852,
                      'unranked_game_channel': 511906353476927498,
                      'match_challenge_channels': [511316081160355852, 511906353476927498],
                      'game_request_channel': 418175357137453058,
                      'game_announce_channel': 505523961812090900,
                      'game_channel_categories': [546527176380645395, 551747728548298758, 551748058690617354, 560104969580183562, 590592163751002124, 598599707148943361, 628288610235449364, 628288644729405452]},
        667409158806437919:                           # PolyFFA
                     {'helper_roles': ['MOD', 'helper'],
                      'mod_roles': ['MOD'],
                      'user_roles_level_4': ['''@everyone'''],  # power user
                      'user_roles_level_3': ['''@everyone'''],  # power user
                      'user_roles_level_2': ['''@everyone'''],  # normal user
                      'user_roles_level_1': ['''@everyone'''],  # restricted user/newbie
                      'inactive_role': None,
                      'display_name': 'PolyFFA',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 10,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [667458314338041886],  # 886 bot-private
                      'bot_channels_strict': [667414547061145601],  # 601 bot-commands
                      'bot_channels': [667414547061145601, 667455359283232769],  # 601 bot-commands 769 general
                      'ranked_game_channel': [667414547061145601],  # 601 bot-commands
                      'unranked_game_channel': None,
                      'match_challenge_channels': [667414547061145601],  # 601 bot-commands,
                      'game_request_channel': 667458314338041886,  # bot-private
                      'game_announce_channel': 667465781595996180,  # 180 elo-drafts
                      'game_channel_categories': [667458667716804609]},  # current games
        598698486384295946:                           # PolyConference run by Debussi
                     {'helper_roles': ['ELO-Helper'],
                      'mod_roles': ['MOD'],
                      'user_roles_level_4': ['Qualifier, League Member'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'PolyConference 2019',
                      'require_teams': False,
                      'allow_teams': False,
                      'allow_uneven_teams': True,
                      'max_team_size': 2,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [600413821445734543],
                      'bot_channels': [600413821445734543],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_announce_channel': None,
                      'match_challenge_channels': [],
                      'game_channel_categories': []},
        570621740653477898:                           # Luxidoor Palace
                     {'helper_roles': ['Advisor', 'Bot Commander', 'Arena Staff'],
                      'mod_roles': ['Sultan of Luxidoor', 'Emperor', 'Server Emperor'],
                      'user_roles_level_4': ['Luxidoor'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Grand Palace of Luxidoor',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 2,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [584208108075614219, 625783083081138207],
                      'bot_channels': [584208108075614219, 625783083081138207],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_announce_channel': None,
                      'match_challenge_channels': [],
                      'game_channel_categories': [625005163425300480]},
        614259582642159626:                           # AutumnGames run by SneakyTacts
                     {'helper_roles': [''],
                      'mod_roles': ['Director'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Autumn Games',
                      'require_teams': False,
                      'allow_teams': False,
                      'allow_uneven_teams': True,
                      'max_team_size': 2,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [623322009635389462, 639542118997819423],
                      'bot_channels': [623322009635389462, 639542118997819423],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_announce_channel': 647868483094183964,
                      'match_challenge_channels': [],
                      'game_channel_categories': [614259582642159627]},
        568090839545413635: {'display_name': 'Polympire'},                       # Polympire, small server run by WhyDoYouDide
        576962604124209180: {'display_name': 'Poly-Gyms'},                      # Poly-Gyms PinkPigmyPuff#7107
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
                      'game_channel_categories': []},
        442780546624520203:                           # Diplotopia
                     {'helper_roles': [''],
                      'mod_roles': ['Moderator', 'Administrator'],
                      'user_roles_level_4': ['@everyone'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Diplotopia',
                      'require_teams': False,
                      'allow_teams': False,
                      'allow_uneven_teams': True,
                      'max_team_size': 3,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [604336112999071766, 453347637622734888],
                      'bot_channels': [604336112999071766, 453347637622734888],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_announce_channel': 604336246646505492,
                      'match_challenge_channels': [],
                      'game_channel_categories': []},
        584524000521093120:                           # Polytopia League run by Smaker27
                     {'helper_roles': [''],
                      'mod_roles': ['MOD', 'Administrator'],
                      'user_roles_level_4': ['League Member'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'Polytopia League',
                      'require_teams': False,
                      'allow_teams': False,
                      'allow_uneven_teams': True,
                      'max_team_size': 3,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [614234606279065601, 614234606279065601],
                      'bot_channels': [614234606279065601, 614234606279065601],
                      'game_request_channel': 619748006681509910,  # bot-commands-2
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_announce_channel': 614234715221786634,
                      'match_challenge_channels': [],
                      'game_channel_categories': []},
        625819621748113408:                         # LigaRex event server by AnarchoRex. Not a full server, info mostly here just to put game_channel_categories in use
                    {'helper_roles': [''],
                      'mod_roles': ['MOD', 'Bot Shepherd'],
                      'user_roles_level_4': ['League Member'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'display_name': 'LigaRex',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [],
                      'bot_channels_strict': [625874241233092648],
                      'bot_channels': [625874241233092648],
                      'ranked_game_channel': None,
                      'unranked_game_channel': None,
                      'game_announce_channel': None,
                      'match_challenge_channels': [],
                      'game_channel_categories': [625874394786430977, 625874455893245953, 625876193744388136, 625876453795561485, 625876480425066497, 625876505884622858, 625876537757007882, 625876574050320406, 625876603351728148, 625876629289435146]},
        606284456474443786:                           # CustomPoly
                     {'helper_roles': ['helper', 'admin'],
                      'mod_roles': ['admin'],
                      'user_roles_level_4': ['customizer'],  # power user
                      'user_roles_level_3': ['@everyone'],  # power user
                      'user_roles_level_2': ['@everyone'],  # normal user
                      'user_roles_level_1': ['@everyone'],  # restricted user/newbie
                      'inactive_role': None,
                      'display_name': 'CustomPoly',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 6,
                      'command_prefix': '$',
                      'include_in_global_lb': False,
                      'bot_channels_private': [662390393048006656],  # 656 staff-bot-channel
                      'bot_channels_strict': [608093784898666497],  # 497 bot-commands
                      'bot_channels': [608093784898666497, 606284456478638081],  # 497 bot-commands 081 general
                      'ranked_game_channel': None,
                      'unranked_game_channel': 608093784898666497,  # 497 bot-commands 656 staff-bot-channel 081 general
                      'game_request_channel': 684944690893946972,  # $staffhelp output
                      'game_channel_categories': [618499589670043679]},  # 'ongoing games'
          }

lobbies = [{'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': True, 'notes': '**Newbie game** - 1075 elo max'},
           {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': True, 'remake_partial': False, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': True, 'remake_partial': False, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': '1v1', 'size': [1, 1], 'ranked': False, 'remake_partial': True, 'notes': ''},
           {'guild': 283436219780825088, 'size_str': 'FFA', 'size': [1, 1, 1], 'ranked': False, 'remake_partial': False, 'notes': ''},
           # {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'},
           # {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 531567102042308609], 'notes': 'Newbie 2v2 game, Novas welcome <:novas:531568047824306188>'},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841367103602768, 696841359616901150], 'notes': '**Newbie game**: Nova Red vs Nova Blue'},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841359616901150, 696841367103602768], 'notes': '**Newbie game**: Nova Blue vs Nova Red'},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841367103602768, 696841359616901150], 'notes': '**Newbie game**: Nova Red vs Nova Blue'},
           {'guild': 447883341463814144, 'size_str': '2v2', 'size': [2, 2], 'ranked': True, 'exp': 95, 'remake_partial': False, 'role_locks': [696841359616901150, 696841367103602768], 'notes': '**Newbie game**: Nova Blue vs Nova Red'},
           # {'guild': 447883341463814144, 'size_str': '3v3', 'size': [3, 3], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 531567102042308609], 'notes': 'Newbie 3v3 game, Novas welcome <:novas:531568047824306188>'},
           # {'guild': 447883341463814144, 'size_str': '3v3', 'size': [3, 3], 'ranked': True, 'exp': 95, 'remake_partial': False, 'notes': 'Open to all'},
           {'guild': 478571892832206869, 'size_str': '3v3', 'size': [3, 3], 'ranked': False, 'exp': 95, 'remake_partial': False, 'role_locks': [None, 480350546172182530], 'notes': ''},
           {'guild': 478571892832206869, 'size_str': 'FFA', 'size': [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], 'ranked': True, 'exp': 95, 'remake_partial': True, 'notes': 'Open to all'}]

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
    313427349775450112,  # SouthPenguinJay#3692
    616737820261875721,  # CoolGuyNotFoolGuy#0498 troll who blatantly lied about game confirmations
    735809555837091861,  # XaeroXD8401  points cheater
]

poly_id_ban_list = [
    'pKUaK61nd2BzNY65',  # BlueberryCraft#9080 (star hacker)
    'MvSRS2t5vWLUyyuu',  # Caesar Augustas Trajan
    'AfMDTSO3yareZN2E',  # Freeze
    # 'qIqw1okeZZgaFpUL',  # Remalin (skre alt)
    # '815D2hK94mN7StoL',  # Skrealder
    'fOEjbnrzO9tg1QYT',  # Doggo#8422
    '8ZWg85d9PlogdY1H',  # Stupid#7043
    'R5NregRkLycUsq7C',  # Just7609
    '9x85fWIxxkLyOMem',  # logs#4361
    'JU1Zb9jGO4H1I4Ls',  # SouthPenguinJay#3692
    'MhJJohJENaeBUz7H',  # CoolGuyNotFoolGuy#0498
    '20aih8HH5IcromHX',  # XaeroXD8401
]


generic_teams_short = [('Home', ':stadium:'), ('Away', ':airplane:')]  # For two-team games
generic_teams_long = [('Sharks', ':shark:'), ('Owls', ':owl:'), ('Eagles', ':eagle:'), ('Tigers', ':tiger:'),
                      ('Bears', ':bear:'), ('Koalas', ':koala:'), ('Dogs', ':dog:'), ('Bats', ':bat:'),
                      ('Lions', ':lion:'), ('Cats', ':cat:'), ('Birds', ':bird:'), ('Spiders', ':spider:')]

date_cutoff = datetime.datetime.today() - datetime.timedelta(days=90)  # Players who haven't played since cutoff are not included in leaderboards


def get_setting(setting_name):
    return config['default'][setting_name]


def guild_setting(guild_id: int, setting_name: str):
    # if guild_id = None, default block will be used

    if guild_id:

        try:
            settings_obj = config[guild_id]
        except KeyError:
            logger.warn(f'Unknown guild id {guild_id} requested for setting name {setting_name}.')
            raise exceptions.CheckFailedError('Unauthorized: This guild is not in the config.ini file.')
            # return config['default'][setting_name]

        try:
            return settings_obj[setting_name]
        except KeyError:
            return config['default'][setting_name]

    else:
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


def is_staff(ctx, user=None):
    user = ctx.author if not user else user

    if user.id == owner_id:
        return True
    helper_roles = guild_setting(ctx.guild.id, 'helper_roles')
    mod_roles = guild_setting(ctx.guild.id, 'mod_roles')

    target_match = get_matching_roles(user, helper_roles + mod_roles)
    return len(target_match) > 0


def is_mod(ctx_or_member, user=None):
    # if member passed as first arg, checks to see if member is a mod of the guild they are a member of
    # if ctx is passed, will check second arg user as a mod, or check ctx.member as a mod

    if type(ctx_or_member).__name__ == 'Context':
        user = ctx_or_member.author if not user else user
        guild = ctx_or_member.guild
    else:
        # Assuming Member object passed
        user = ctx_or_member
        guild = user.guild

    if user.id == owner_id:
        return True
    mod_roles = guild_setting(guild.id, 'mod_roles')

    target_match = get_matching_roles(user, mod_roles)
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


async def is_bot_channel_strict(ctx):
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


def in_bot_channel_strict():
    async def predicate(ctx):
        return await is_bot_channel_strict(ctx)
    return commands.check(predicate)
