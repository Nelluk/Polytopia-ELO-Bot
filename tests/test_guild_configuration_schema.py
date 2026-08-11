"""Offline contract tests for the future dynamic guild configuration store."""

from __future__ import annotations

import ast
import copy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import unittest

from modules.guild_configuration_schema import (
    LEGACY_DEFAULT_KEYS,
    MIGRATED_LEGACY_KEYS,
    OBSOLETE_LEGACY_KEYS,
    SCHEMA_VERSION,
    GuildConfigurationDocument,
    GuildConfigurationError,
    canonical_document_json,
    document_digest,
    document_to_mapping,
    materialize_legacy_document,
    validate_document,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def valid_value(*, guild_id: int = 100) -> dict:
    return {
        'schema_version': SCHEMA_VERSION,
        'guild_id': guild_id,
        'identity': {
            'display_name': 'Example Guild',
            'command_prefix': '$',
        },
        'permissions': {
            'helper_role_ids': [201, 202],
            'mod_role_ids': [202],
            'user_role_ids_level_1': [guild_id],
            'user_role_ids_level_2': [guild_id],
            'user_role_ids_level_3': [guild_id],
            'user_role_ids_level_4': [],
            'inactive_role_id': 203,
        },
        'teams': {
            'require_teams': True,
            'allow_teams': True,
            'allow_uneven_teams': False,
            'max_team_size': 7,
        },
        'visibility': {
            'include_in_global_leaderboard': True,
        },
        'channels': {
            'bot_channel_ids': [301, 302],
            'strict_bot_channel_ids': None,
            'private_bot_channel_ids': [303],
            'newbie_message_channel_ids': [],
            'match_challenge_channel_ids': [304],
            'ranked_game_channel_id': 305,
            'unranked_game_channel_id': 306,
            'steam_game_channel_id': None,
            'log_channel_id': 307,
            'game_announce_channel_id': 308,
            'staff_help_channel_id': 309,
            'game_category_ids': [401, 402],
        },
        'command_capabilities': ['tools_support', 'core_user'],
    }


def legacy_defaults() -> dict:
    return {
        'helper_roles': ['Helper'],
        'mod_roles': ['Mod'],
        'user_roles_level_4': [],
        'user_roles_level_3': ['@everyone'],
        'user_roles_level_2': ['@everyone'],
        'user_roles_level_1': ['@everyone'],
        'inactive_role': None,
        'display_name': 'Default Guild',
        'require_teams': False,
        'allow_teams': True,
        'allow_uneven_teams': False,
        'max_team_size': 1,
        'command_prefix': '!',
        'include_in_global_lb': False,
        'match_challenge_channel': None,
        'bot_channels_private': [],
        'bot_channels_strict': None,
        'bot_channels': [],
        'newbie_message_channels': [],
        'match_challenge_channels': [],
        'ranked_game_channel': None,
        'unranked_game_channel': None,
        'steam_game_channel': None,
        'log_channel': None,
        'game_announce_channel': None,
        'staff_help_channel': None,
        'game_channel_categories': [],
    }


class GuildConfigurationSchemaTests(unittest.TestCase):
    def test_valid_document_is_complete_frozen_and_deterministic(self):
        document = validate_document(valid_value())

        self.assertIsInstance(document, GuildConfigurationDocument)
        self.assertEqual(document.guild_id, 100)
        self.assertEqual(document.permissions.helper_role_ids, (201, 202))
        self.assertEqual(document.channels.strict_bot_channel_ids, None)
        self.assertEqual(
            document.command_capabilities,
            ('core_user', 'tools_support'),
        )
        with self.assertRaises(FrozenInstanceError):
            document.guild_id = 999
        with self.assertRaises(FrozenInstanceError):
            document.identity.display_name = 'Changed'

    def test_exact_objects_and_json_list_shapes_are_required(self):
        for path, key in (
            ((), 'unexpected'),
            (('identity',), 'unexpected'),
            (('permissions',), 'unexpected'),
            (('teams',), 'unexpected'),
            (('visibility',), 'unexpected'),
            (('channels',), 'unexpected'),
        ):
            value = valid_value()
            target = value
            for part in path:
                target = target[part]
            target[key] = 'unsafe'
            with self.subTest(path=path), self.assertRaisesRegex(
                GuildConfigurationError,
                'unknown',
            ):
                validate_document(value)

        missing = valid_value()
        del missing['channels']['log_channel_id']
        with self.assertRaisesRegex(GuildConfigurationError, 'missing'):
            validate_document(missing)

        tuple_list = valid_value()
        tuple_list['permissions']['helper_role_ids'] = (201,)
        with self.assertRaisesRegex(GuildConfigurationError, 'JSON list'):
            validate_document(tuple_list)

    def test_scalar_types_bounds_and_team_cross_rule_fail_closed(self):
        cases = (
            (('schema_version',), True, 'schema version'),
            (('guild_id',), True, 'positive integer'),
            (('identity', 'display_name'), ' padded ', 'trimmed'),
            (('identity', 'command_prefix'), 'too-long', 'at most'),
            (('identity', 'command_prefix'), '! !', 'whitespace'),
            (('teams', 'allow_teams'), 1, 'boolean'),
            (('teams', 'max_team_size'), 17, 'at most 16'),
        )
        for path, replacement, pattern in cases:
            value = valid_value()
            target = value
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = replacement
            with self.subTest(path=path), self.assertRaisesRegex(
                GuildConfigurationError,
                pattern,
            ):
                validate_document(value)

        contradiction = valid_value()
        contradiction['teams']['allow_teams'] = False
        with self.assertRaisesRegex(GuildConfigurationError, 'require_teams'):
            validate_document(contradiction)

    def test_id_lists_preserve_semantic_order_and_reject_unsafe_values(self):
        document = validate_document(valid_value())
        self.assertEqual(document.permissions.helper_role_ids, (201, 202))
        self.assertEqual(document.channels.bot_channel_ids, (301, 302))

        duplicate = valid_value()
        duplicate['channels']['bot_channel_ids'] = [301, 301]
        with self.assertRaisesRegex(GuildConfigurationError, 'duplicate'):
            validate_document(duplicate)

        invalid = valid_value()
        invalid['permissions']['mod_role_ids'] = [False]
        with self.assertRaisesRegex(GuildConfigurationError, 'positive integer'):
            validate_document(invalid)

        too_many = valid_value()
        too_many['channels']['game_category_ids'] = list(range(1, 52))
        with self.assertRaisesRegex(GuildConfigurationError, 'at most 50'):
            validate_document(too_many)

    def test_capabilities_are_known_unique_sorted_and_route_complete(self):
        document = validate_document(valid_value())
        self.assertEqual(
            document.command_capabilities,
            ('core_user', 'tools_support'),
        )

        duplicate = valid_value()
        duplicate['command_capabilities'] = ['core_user', 'core_user']
        with self.assertRaisesRegex(GuildConfigurationError, 'duplicate'):
            validate_document(duplicate)

        unknown = valid_value()
        unknown['command_capabilities'] = ['root_from_database']
        with self.assertRaisesRegex(GuildConfigurationError, 'unknown'):
            validate_document(unknown)

        missing_route = valid_value()
        missing_route['channels']['staff_help_channel_id'] = None
        with self.assertRaisesRegex(GuildConfigurationError, 'tools_support'):
            validate_document(missing_route)

    def test_everyone_is_allowed_only_in_user_permission_tiers(self):
        for field in ('helper_role_ids', 'mod_role_ids'):
            value = valid_value()
            value['permissions'][field] = [value['guild_id']]
            with self.subTest(field=field), self.assertRaisesRegex(
                GuildConfigurationError,
                '@everyone',
            ):
                validate_document(value)

        inactive = valid_value()
        inactive['permissions']['inactive_role_id'] = inactive['guild_id']
        with self.assertRaisesRegex(GuildConfigurationError, '@everyone'):
            validate_document(inactive)

        document = validate_document(valid_value())
        self.assertEqual(
            document.permissions.user_role_ids_level_1,
            (document.guild_id,),
        )

    def test_round_trip_returns_copies_and_digest_binds_complete_document(self):
        source = valid_value()
        document = validate_document(source)
        mapping = document_to_mapping(document)
        self.assertEqual(validate_document(mapping), document)
        self.assertEqual(json.loads(canonical_document_json(document)), mapping)

        source['permissions']['helper_role_ids'].append(998)
        self.assertEqual(document.permissions.helper_role_ids, (201, 202))

        mapping['permissions']['helper_role_ids'].append(999)
        self.assertEqual(document.permissions.helper_role_ids, (201, 202))

        key_reordered = valid_value()
        key_reordered = dict(reversed(tuple(key_reordered.items())))
        key_reordered['permissions'] = dict(reversed(tuple(
            key_reordered['permissions'].items()
        )))
        self.assertEqual(
            document_digest(validate_document(key_reordered)),
            document_digest(document),
        )

        reordered = valid_value()
        reordered['command_capabilities'].reverse()
        self.assertEqual(
            document_digest(validate_document(reordered)),
            document_digest(document),
        )

        role_reordered = valid_value()
        role_reordered['permissions']['helper_role_ids'].reverse()
        self.assertNotEqual(
            document_digest(validate_document(role_reordered)),
            document_digest(document),
        )

        another_guild = valid_value(guild_id=101)
        self.assertNotEqual(
            document_digest(validate_document(another_guild)),
            document_digest(document),
        )

    def test_legacy_inventory_matches_the_tracked_example(self):
        source = (PROJECT_ROOT / 'server_settings-EXAMPLE.py').read_text(
            encoding='utf-8'
        )
        tree = ast.parse(source)
        server_list = None
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == 'server_list'
                    for target in node.targets
                )
            ):
                server_list = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(server_list)
        self.assertEqual(set(server_list['default']), LEGACY_DEFAULT_KEYS)
        self.assertEqual(len(LEGACY_DEFAULT_KEYS), 27)
        self.assertEqual(len(MIGRATED_LEGACY_KEYS), 26)
        self.assertEqual(OBSOLETE_LEGACY_KEYS, {'match_challenge_channel'})

    def test_legacy_materialization_is_complete_and_does_not_mutate_inputs(self):
        defaults = legacy_defaults()
        overrides = {
            'display_name': 'Configured Guild',
            'helper_roles': ['Helper', 'Mod'],
            'inactive_role': 'Inactive',
            'bot_channels': [501, 502],
            'staff_help_channel': 503,
            'max_team_size': 4,
        }
        role_ids = {'Helper': 601, 'Mod': [602], 'Inactive': 603}
        original_defaults = copy.deepcopy(defaults)
        original_overrides = copy.deepcopy(overrides)

        document = materialize_legacy_document(
            guild_id=100,
            defaults=defaults,
            overrides=overrides,
            role_ids_by_name=role_ids,
            command_capabilities=('tools_support', 'core_user'),
        )

        self.assertEqual(document.identity.display_name, 'Configured Guild')
        self.assertEqual(document.permissions.helper_role_ids, (601, 602))
        self.assertEqual(document.permissions.mod_role_ids, (602,))
        self.assertEqual(document.permissions.inactive_role_id, 603)
        self.assertEqual(document.permissions.user_role_ids_level_1, (100,))
        self.assertEqual(document.channels.bot_channel_ids, (501, 502))
        self.assertEqual(document.channels.staff_help_channel_id, 503)
        self.assertEqual(document.teams.max_team_size, 4)
        self.assertEqual(defaults, original_defaults)
        self.assertEqual(overrides, original_overrides)

    def test_legacy_role_resolution_is_exact_and_unambiguous(self):
        with self.assertRaisesRegex(GuildConfigurationError, 'does not resolve'):
            materialize_legacy_document(
                guild_id=100,
                defaults=legacy_defaults(),
                overrides={},
                role_ids_by_name={'Mod': 602},
            )

        with self.assertRaisesRegex(GuildConfigurationError, 'exactly one'):
            materialize_legacy_document(
                guild_id=100,
                defaults=legacy_defaults(),
                overrides={},
                role_ids_by_name={'Helper': (601, 699), 'Mod': 602},
            )

        aliases = legacy_defaults()
        aliases['helper_roles'] = ['Helper', 'Alias']
        with self.assertRaisesRegex(GuildConfigurationError, 'same role ID'):
            materialize_legacy_document(
                guild_id=100,
                defaults=aliases,
                overrides={},
                role_ids_by_name={'Helper': 601, 'Alias': 601, 'Mod': 602},
            )

    def test_legacy_shape_unknowns_and_obsolete_value_are_rejected(self):
        missing = legacy_defaults()
        del missing['display_name']
        with self.assertRaisesRegex(GuildConfigurationError, 'missing'):
            materialize_legacy_document(
                guild_id=100,
                defaults=missing,
                overrides={},
                role_ids_by_name={'Helper': 601, 'Mod': 602},
            )

        with self.assertRaisesRegex(GuildConfigurationError, 'unknown fields'):
            materialize_legacy_document(
                guild_id=100,
                defaults=legacy_defaults(),
                overrides={'database_password': 'do-not-store'},
                role_ids_by_name={'Helper': 601, 'Mod': 602},
            )

        obsolete = legacy_defaults()
        obsolete['match_challenge_channel'] = 999
        with self.assertRaisesRegex(GuildConfigurationError, 'must be cleared'):
            materialize_legacy_document(
                guild_id=100,
                defaults=obsolete,
                overrides={},
                role_ids_by_name={'Helper': 601, 'Mod': 602},
            )

        with self.assertRaisesRegex(GuildConfigurationError, 'must be a sequence'):
            materialize_legacy_document(
                guild_id=100,
                defaults=legacy_defaults(),
                overrides={},
                role_ids_by_name={'Helper': 601, 'Mod': 602},
                command_capabilities='core_user',
            )

    def test_legacy_everyone_cannot_grant_staff_or_inactive_authority(self):
        for field, replacement in (
            ('helper_roles', ['@everyone']),
            ('mod_roles', ['@everyone']),
            ('inactive_role', '@everyone'),
        ):
            defaults = legacy_defaults()
            defaults[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                GuildConfigurationError,
                '@everyone',
            ):
                materialize_legacy_document(
                    guild_id=100,
                    defaults=defaults,
                    overrides={},
                    role_ids_by_name={'Helper': 601, 'Mod': 602},
                )


if __name__ == '__main__':
    unittest.main()
