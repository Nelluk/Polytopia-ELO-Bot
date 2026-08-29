"""Test-guild-only server settings for POLYBOT_ENV=development.

Copy to server_settings_dev.py and replace all placeholder IDs. Do not add a
production guild to server_list or server_shortcut_ids.
"""

TEST_GUILD_ID = 123456789012345678
TEST_BOT_CHANNEL_ID = 123456789012345679

# Code currently expects these three shortcut names. In development they all
# resolve to the one isolated test guild.
server_shortcut_ids = {
    'main': TEST_GUILD_ID,
    'polychampions': TEST_GUILD_ID,
    'test': TEST_GUILD_ID,
}

# Default-deny: explicit development-guild registration is performed by
# scripts/manage_application_commands.py, separately from launching the bot.
# Example (after replacing TEST_GUILD_ID):
# application_command_capabilities = {
#     TEST_GUILD_ID: ('core_user',),
# }
application_command_capabilities = {}
# Optional capability names applied to every allowed guild in this runtime
# profile. Keep empty until a real cross-guild command root is ready to deploy.
application_command_all_guild_capabilities = ()

server_list = {
    'default': {
        'helper_roles': ['Helper'],
        'mod_roles': ['Mod'],
        'user_roles_level_4': [],
        'user_roles_level_3': ['@everyone'],
        'user_roles_level_2': ['@everyone'],
        'user_roles_level_1': ['@everyone'],
        'inactive_role': None,
        'display_name': 'Development Server',
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
        'bot_channels': [TEST_BOT_CHANNEL_ID],
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
    TEST_GUILD_ID: {
        'display_name': 'Development Test Guild',
        'command_prefix': '$',
        'bot_channels': [TEST_BOT_CHANNEL_ID],
    },
}
polyelo_feedback_route = {}
