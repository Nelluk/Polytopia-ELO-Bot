"""Minimal one-guild inventory for database-backed configuration.

Copy this file to ``server_settings.py`` and replace the placeholder guild ID.
Ordinary guild settings and command capabilities are stored in PostgreSQL.
"""

SERVER_GUILD_ID = 123456789012345678

# A few compatibility paths still use these historical shortcut names. For a
# standalone installation they can all safely refer to the same guild.
server_shortcut_ids = {
    'main': SERVER_GUILD_ID,
    'polychampions': SERVER_GUILD_ID,
    'test': SERVER_GUILD_ID,
}

# Database authority supplies the active command policy. These static values
# remain empty so a missing database configuration fails closed.
application_command_capabilities = {}
application_command_all_guild_capabilities = ()
polyelo_feedback_route = {}

# Database authority still uses this inventory to bind the process to its
# intended Discord guild before loading the active configuration graph. The
# complete conservative defaults also keep legacy static import available as
# an explicit migration/recovery path.
server_list = {
    'default': {
        'helper_roles': [],
        'mod_roles': [],
        'user_roles_level_4': [],
        'user_roles_level_3': [],
        'user_roles_level_2': ['@everyone'],
        'user_roles_level_1': [],
        'inactive_role': None,
        'display_name': 'PolyBot Server',
        'require_teams': False,
        'allow_teams': False,
        'allow_uneven_teams': False,
        'max_team_size': 2,
        'command_prefix': '$',
        'include_in_global_lb': False,
        'match_challenge_channel': None,
        'bot_channels_private': [],
        'bot_channels_strict': None,
        'bot_channels': None,
        'newbie_message_channels': [],
        'match_challenge_channels': [],
        'ranked_game_channel': None,
        'unranked_game_channel': None,
        'steam_game_channel': None,
        'log_channel': None,
        'game_announce_channel': None,
        'staff_help_channel': None,
        'game_channel_categories': [],
    },
    SERVER_GUILD_ID: {
        'display_name': 'My PolyBot Server',
    },
}
