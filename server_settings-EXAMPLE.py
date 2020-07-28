# make a copy of this file called server_settings.py

server_shortcut_ids = {'main': 283436219780825088, 'polychampions': 447883341463814144, 'test': 478571892832206869}
# this is a convenience list of frequently-referred to servers in the code.
# main and polychampions are the primary game discord servers and should remain as they are
# test can be a development server. the bot will treat that as having the same abilities as polychampions


# server_list is a dict of dicts. the first dict is the default value for any setting not specified at the server level.
# each subsequent dict is one server that the bot is configured for, with the key value being the server ID.
# the bot will leave any server that is not represented here.
server_list = {'default':
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
                      'bot_channels': [],  # list of channel IDs that are bot channels
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
                      'helper_roles': ['Staff'],
                      'inactive_role': 'Inactive',
                      'display_name': 'Development Server',
                      'require_teams': False,
                      'allow_teams': True,
                      'allow_uneven_teams': True,
                      'max_team_size': 1,
                      'command_prefix': '/',
                      'bot_channels_strict': [479292913080336397],
                      'bot_channels': [479292913080336397, 481558031281160212, 480078679930830849],  # 397 Bot Spam,  849 Admin Spam
                      'game_request_channel': 480078679930830849,
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
                      'game_channel_categories': [546527176380645395, 551747728548298758, 551748058690617354, 560104969580183562, 590592163751002124, 598599707148943361, 628288610235449364, 628288644729405452]}
}
