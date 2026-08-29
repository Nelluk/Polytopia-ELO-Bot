"""Minimal one-guild production settings.

Copy this file to ``server_settings.py`` and replace the two placeholder IDs.
Add optional channels and roles only after the bot starts successfully.
"""

SERVER_GUILD_ID = 123456789012345678
BOT_CHANNEL_ID = 123456789012345679

# A few compatibility paths still use these historical shortcut names. For a
# standalone installation they can all safely refer to the same guild.
server_shortcut_ids = {
    'main': SERVER_GUILD_ID,
    'polychampions': SERVER_GUILD_ID,
    'test': SERVER_GUILD_ID,
}

# Slash commands are default-deny. The self-hosting guide explains how to
# select capabilities and deploy them to this exact guild.
application_command_capabilities = {
    SERVER_GUILD_ID: ('core_user',),
}
application_command_all_guild_capabilities = ()
# ``tools_support`` exposes /staffhelp. Enable it only after configuring the
# private staff-help channel, Helper role, and operator-owned feedback route
# described in docs/DOCKER.md.
polyelo_feedback_route = {}

server_list = {
    'default': {
        'helper_roles': ['Helper'],
        'mod_roles': ['Mod'],
        'user_roles_level_4': [],
        'user_roles_level_3': ['@everyone'],
        'user_roles_level_2': ['@everyone'],
        'user_roles_level_1': ['@everyone'],
        'inactive_role': None,
        'display_name': 'PolyBot Server',
        'require_teams': False,
        'allow_teams': False,
        'allow_uneven_teams': False,
        # Historical internal key; user-facing name is "Maximum players per side".
        'max_team_size': 2,
        'command_prefix': '$',
        'include_in_global_lb': False,
        'match_challenge_channel': None,
        'bot_channels_private': [],
        'bot_channels_strict': [],
        'bot_channels': [BOT_CHANNEL_ID],
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
        'bot_channels': [BOT_CHANNEL_ID],
    },
}
