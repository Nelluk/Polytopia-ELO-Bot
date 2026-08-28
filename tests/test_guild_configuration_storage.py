"""Offline coverage for P10.3 development configuration persistence."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from modules import guild_configuration_storage as storage
from modules import guild_configuration_bootstrap as bootstrap
from modules import guild_configuration_delegation_storage as delegation
from modules import guild_configuration_draft_storage as drafts
from modules import guild_types
from scripts import manage_guild_configuration_storage as script


GUILD_ID = storage.DEVELOPMENT_BETA_GUILD_ID


def defaults() -> dict:
    return {
        'helper_roles': ['Helper', 'Beta Lab Staff'],
        'mod_roles': ['Mod'],
        'user_roles_level_4': [],
        'user_roles_level_3': ['@everyone'],
        'user_roles_level_2': ['@everyone'],
        'user_roles_level_1': ['@everyone'],
        'inactive_role': 'Inactive',
        'display_name': 'Development Server',
        'require_teams': False,
        'allow_teams': True,
        'allow_uneven_teams': True,
        'max_team_size': 1,
        'command_prefix': '!',
        'include_in_global_lb': False,
        'match_challenge_channel': None,
        'bot_channels_private': [],
        'bot_channels_strict': [301],
        'bot_channels': [300, 301],
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


def server_settings():
    return SimpleNamespace(
        server_list={
            'default': defaults(),
            GUILD_ID: {
                'display_name': 'Development Test Guild',
                'include_in_global_lb': True,
            },
        },
        application_command_capabilities={
            GUILD_ID: ('core_user', 'tools_support'),
        },
        application_command_all_guild_capabilities=('operator',),
    )


def target(**changes) -> storage.StorageTarget:
    values = {
        'environment': 'development',
        'database_name': 'polytopia_dev',
        'database_user': 'polybot_dev',
        'expected_application_id': storage.DEVELOPMENT_BETA_APPLICATION_ID,
        'background_tasks_enabled': False,
        'api_enabled': False,
        'bullet_enabled': False,
    }
    values.update(changes)
    return storage.StorageTarget(**values)


def production_target(**changes) -> storage.StorageTarget:
    values = {
        'environment': storage.PRODUCTION_ENVIRONMENT,
        'database_name': storage.PRODUCTION_DATABASE,
        'database_user': storage.PRODUCTION_ROLE,
        'expected_application_id': storage.PRODUCTION_APPLICATION_ID,
        'background_tasks_enabled': True,
        'api_enabled': False,
        'bullet_enabled': True,
    }
    values.update(changes)
    return storage.StorageTarget(**values)


def snapshot() -> dict:
    return {
        'schema_version': storage.SNAPSHOT_SCHEMA_VERSION,
        'kind': 'guild_configuration_discord_snapshot',
        'environment': 'development',
        'application_id': storage.DEVELOPMENT_BETA_APPLICATION_ID,
        'guilds': [{
            'guild_id': GUILD_ID,
            'guild_name': 'Nelluk Test Server',
            'roles': [
                {'id': GUILD_ID, 'name': '@everyone', 'managed': False, 'is_default': True},
                {'id': 201, 'name': 'Helper', 'managed': False, 'is_default': False},
                {'id': 202, 'name': 'Beta Lab Staff', 'managed': False, 'is_default': False},
                {'id': 203, 'name': 'Mod', 'managed': False, 'is_default': False},
                {'id': 204, 'name': 'Inactive', 'managed': False, 'is_default': False},
            ],
            'channels': [
                {'id': 300, 'name': 'admin-spam', 'type': 'text', 'category_id': None},
                {'id': 301, 'name': 'bot-spam', 'type': 'text', 'category_id': None},
                {
                    'id': storage.DEVELOPMENT_STAFF_HELP_CHANNEL_ID,
                    'name': 'staffhelp-mirror',
                    'type': 'text',
                    'category_id': None,
                },
            ],
        }],
    }


def owner_inventory() -> dict:
    return {
        'schema_version': 1,
        'kind': 'guild_configuration_owner_inventory',
        'environment': 'development',
        'application_id': storage.DEVELOPMENT_BETA_APPLICATION_ID,
        'owners': [{
            'guild_id': GUILD_ID,
            'guild_name': 'Nelluk Test Server',
            'owner_id': 900000000000000123,
            'owner_name': 'guild-owner',
        }],
    }


def bundle() -> storage.ImportBundle:
    return storage.build_import_bundle(
        target=target(),
        server_settings=server_settings(),
        allowed_guild_ids=(GUILD_ID,),
        discord_snapshot=snapshot(),
    )


class TargetAndSnapshotTests(unittest.TestCase):
    def test_target_is_one_of_the_two_exact_reviewed_profiles(self):
        storage.validate_target(target())
        storage.validate_target(production_target())
        for value in (
            target(database_name='polytopia2'),
            target(background_tasks_enabled=True),
            production_target(database_user='unexpected'),
            production_target(expected_application_id=1),
            production_target(api_enabled=True),
        ):
            with self.subTest(target=value), self.assertRaisesRegex(
                storage.GuildConfigurationStorageError,
                'exact reviewed',
            ):
                storage.validate_target(value)

    def test_production_snapshot_must_match_production_environment_and_app(self):
        value = snapshot()
        value['environment'] = storage.PRODUCTION_ENVIRONMENT
        value['application_id'] = storage.PRODUCTION_APPLICATION_ID
        storage.validate_discord_snapshot(
            value,
            target=production_target(),
            allowed_guild_ids=(GUILD_ID,),
        )
        value['environment'] = storage.DEVELOPMENT_ENVIRONMENT
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'environment',
        ):
            storage.validate_discord_snapshot(
                value,
                target=production_target(),
                allowed_guild_ids=(GUILD_ID,),
            )

    def test_production_rejects_partial_schema_and_first_guild_paths(self):
        with self.assertRaisesRegex(
            drafts.GuildConfigurationDraftStorageError,
            'atomically',
        ):
            drafts.apply_draft_schema(
                mock.Mock(),
                target=production_target(),
                plan=mock.sentinel.plan,
                confirmation='no',
            )
        with self.assertRaisesRegex(
            delegation.GuildConfigurationDelegationStorageError,
            'atomically',
        ):
            delegation.apply_delegation_schema(
                mock.Mock(),
                target=production_target(),
                plan=mock.sentinel.plan,
                confirmation='no',
            )
        production_snapshot = snapshot()
        production_snapshot['environment'] = storage.PRODUCTION_ENVIRONMENT
        production_snapshot['application_id'] = storage.PRODUCTION_APPLICATION_ID
        with self.assertRaisesRegex(
            bootstrap.FirstGuildBootstrapError,
            'development-only',
        ):
            bootstrap.build_first_guild_plan(
                target=production_target(),
                allowed_guild_ids=(GUILD_ID,),
                discord_snapshot=production_snapshot,
            )

    def test_live_identity_must_match_configured_development_target(self):
        storage.validate_live_identity(
            target(), actual_database='polytopia_dev', actual_user='polybot_dev'
        )
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError, 'identity mismatch'
        ):
            storage.validate_live_identity(
                target(), actual_database='polytopia2', actual_user='polybot_dev'
            )

    def test_public_reference_validator_accepts_exact_snapshot_and_rejects_drift(self):
        document = bundle().imports[0].document
        exact = storage.validate_discord_snapshot(
            snapshot(),
            target=target(),
            allowed_guild_ids=(GUILD_ID,),
        )[GUILD_ID]
        storage.validate_document_references(document, exact)

        changed = copy.deepcopy(exact)
        changed['roles'] = tuple(
            role for role in changed['roles'] if role['id'] != 201
        )
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'absent from the exact guild',
        ):
            storage.validate_document_references(document, changed)

    def test_snapshot_requires_exact_shape_guild_and_default_role(self):
        result = storage.validate_discord_snapshot(
            snapshot(), target=target(), allowed_guild_ids=(GUILD_ID,)
        )
        self.assertEqual(tuple(result), (GUILD_ID,))

        for mutator, pattern in (
            (lambda value: value.update({'unexpected': True}), 'shape mismatch'),
            (lambda value: value.__setitem__('application_id', 1), 'application'),
            (
                lambda value: value['guilds'][0].__setitem__('guild_id', 999),
                '@everyone',
            ),
            (
                lambda value: value['guilds'][0]['roles'][0].__setitem__('is_default', False),
                '@everyone',
            ),
        ):
            value = snapshot()
            mutator(value)
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                storage.GuildConfigurationStorageError, pattern
            ):
                storage.validate_discord_snapshot(
                    value, target=target(), allowed_guild_ids=(GUILD_ID,)
                )


class ImportBundleTests(unittest.TestCase):
    def test_bundle_materializes_effective_route_roles_and_capabilities(self):
        value = bundle()
        self.assertEqual(value.schema_version, storage.IMPORT_SCHEMA_VERSION)
        self.assertEqual(len(value.imports), 1)
        imported = value.imports[0]
        self.assertEqual(imported.guild_id, GUILD_ID)
        self.assertEqual(imported.document.identity.display_name, 'Development Test Guild')
        self.assertEqual(imported.document.permissions.helper_role_ids, (201, 202))
        self.assertEqual(imported.document.permissions.mod_role_ids, (203,))
        self.assertEqual(imported.document.permissions.inactive_role_id, 204)
        self.assertEqual(
            imported.document.channels.staff_help_channel_id,
            storage.DEVELOPMENT_STAFF_HELP_CHANNEL_ID,
        )
        self.assertEqual(
            imported.document.command_capabilities,
            ('core_user', 'operator', 'tools_support'),
        )
        self.assertRegex(value.bundle_digest, r'^[0-9a-f]{64}$')
        self.assertEqual(value.confirmation, f'P10.3 APPLY {value.bundle_digest}')

    def test_bundle_is_deterministic_and_ignores_unreferenced_role_changes(self):
        first = bundle()
        value = snapshot()
        value['guilds'][0]['roles'].append({
            'id': 999,
            'name': 'Unrelated',
            'managed': False,
            'is_default': False,
        })
        second = storage.build_import_bundle(
            target=target(),
            server_settings=server_settings(),
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=value,
        )
        self.assertEqual(first, second)

    def test_bundle_does_not_mutate_static_or_snapshot_inputs(self):
        settings = server_settings()
        inventory = snapshot()
        settings_before = copy.deepcopy(settings.server_list)
        snapshot_before = copy.deepcopy(inventory)
        storage.build_import_bundle(
            target=target(),
            server_settings=settings,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=inventory,
        )
        self.assertEqual(settings.server_list, settings_before)
        self.assertEqual(inventory, snapshot_before)

    def test_exact_guild_type_overrides_clean_up_legacy_team_policy(self):
        value = storage.build_import_bundle(
            target=target(),
            server_settings=server_settings(),
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot(),
            guild_type_overrides={GUILD_ID: 'standard'},
        )
        document = value.imports[0].document
        self.assertFalse(document.teams.allow_teams)
        self.assertFalse(document.teams.require_teams)
        self.assertEqual(
            document.command_capabilities,
            ('core_user', 'guild_admin', 'operator', 'squad', 'tools_support'),
        )
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'allowlist exactly',
        ):
            storage.build_import_bundle(
                target=target(),
                server_settings=server_settings(),
                allowed_guild_ids=(GUILD_ID,),
                discord_snapshot=snapshot(),
                guild_type_overrides={},
            )

    def test_ambiguous_or_managed_permission_roles_fail_closed(self):
        ambiguous = snapshot()
        ambiguous['guilds'][0]['roles'].append({
            'id': 205, 'name': 'Helper', 'managed': False, 'is_default': False,
        })
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'exactly one'):
            storage.build_import_bundle(
                target=target(), server_settings=server_settings(),
                allowed_guild_ids=(GUILD_ID,), discord_snapshot=ambiguous,
            )

        managed = snapshot()
        managed['guilds'][0]['roles'][1]['managed'] = True
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'managed'):
            storage.build_import_bundle(
                target=target(), server_settings=server_settings(),
                allowed_guild_ids=(GUILD_ID,), discord_snapshot=managed,
            )

    def test_missing_or_wrong_type_channel_fails_closed(self):
        missing = snapshot()
        missing['guilds'][0]['channels'] = [
            row for row in missing['guilds'][0]['channels'] if row['id'] != 301
        ]
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'absent'):
            storage.build_import_bundle(
                target=target(), server_settings=server_settings(),
                allowed_guild_ids=(GUILD_ID,), discord_snapshot=missing,
            )

        wrong = snapshot()
        wrong['guilds'][0]['channels'][0]['type'] = 'category'
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'category IDs'):
            storage.build_import_bundle(
                target=target(), server_settings=server_settings(),
                allowed_guild_ids=(GUILD_ID,), discord_snapshot=wrong,
            )

    def test_live_reference_cleanup_preserves_only_effective_safe_matches(self):
        live = snapshot()
        live['guilds'][0]['roles'] = [
            role for role in live['guilds'][0]['roles']
            if role['name'] != 'Helper'
        ]
        live['guilds'][0]['roles'].extend((
            {
                'id': 205, 'name': 'helper', 'managed': False,
                'is_default': False,
            },
            {
                'id': 206, 'name': 'Mod', 'managed': False,
                'is_default': False,
            },
        ))
        configured = server_settings()
        configured.application_command_capabilities = {
            GUILD_ID: ('core_user',),
        }
        configured.server_list[GUILD_ID] = {
            **configured.server_list[GUILD_ID],
            'bot_channels': [999],
        }

        report = storage.build_live_reference_cleanup_report(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=live,
        )
        value = storage.build_import_bundle(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=live,
            normalize_live_references=True,
        )

        document = value.imports[0].document
        self.assertEqual(document.permissions.helper_role_ids, (202,))
        self.assertEqual(document.permissions.mod_role_ids, (203, 206))
        self.assertEqual(document.channels.bot_channel_ids, ())
        self.assertNotIn('tools_support', document.command_capabilities)
        guild = report['guilds'][0]
        self.assertEqual(guild['severity'], 'review_before_cutover')
        self.assertEqual(guild['remaining']['helper_role_count'], 1)
        self.assertEqual(guild['remaining']['mod_role_count'], 2)
        self.assertEqual(guild['remaining']['bot_channel_count'], 0)
        self.assertEqual(
            {issue['kind'] for issue in guild['issues']},
            {'case_only_role', 'ambiguous_role_name', 'missing_channel'},
        )

    def test_live_reference_cleanup_drops_everyone_from_staff_access_only(self):
        configured = server_settings()
        configured.server_list[GUILD_ID] = {
            **configured.server_list[GUILD_ID],
            'helper_roles': ['@everyone'],
            'mod_roles': ['@everyone', 'Mod'],
            'inactive_role': '@everyone',
        }

        report = storage.build_live_reference_cleanup_report(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot(),
        )
        value = storage.build_import_bundle(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot(),
            normalize_live_references=True,
        )

        document = value.imports[0].document
        self.assertEqual(document.permissions.helper_role_ids, ())
        self.assertEqual(document.permissions.mod_role_ids, (203,))
        self.assertEqual(document.permissions.user_role_ids_level_1, (GUILD_ID,))
        self.assertIsNone(document.permissions.inactive_role_id)
        guild = report['guilds'][0]
        self.assertEqual(guild['severity'], 'review_before_cutover')
        self.assertEqual(guild['remaining']['helper_role_count'], 0)
        self.assertEqual(guild['remaining']['mod_role_count'], 1)
        self.assertEqual(
            [
                (issue['field'], issue['kind'], issue['resolution'])
                for issue in guild['issues']
            ],
            [
                ('helper_roles', 'unsafe_default_role', 'dropped'),
                ('mod_roles', 'unsafe_default_role', 'dropped'),
                ('inactive_role', 'unsafe_default_role', 'dropped'),
            ],
        )

    def test_live_reference_cleanup_unwraps_singleton_scalar_channel(self):
        configured = server_settings()
        configured.server_list[GUILD_ID] = {
            **configured.server_list[GUILD_ID],
            'ranked_game_channel': [300],
        }

        report = storage.build_live_reference_cleanup_report(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot(),
        )
        value = storage.build_import_bundle(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot(),
            normalize_live_references=True,
        )

        self.assertEqual(
            value.imports[0].document.channels.ranked_game_channel_id,
            300,
        )
        issue = next(
            issue for issue in report['guilds'][0]['issues']
            if issue['field'] == 'ranked_game_channel'
        )
        self.assertEqual(issue['kind'], 'singleton_channel_list')
        self.assertEqual(issue['resolution'], 'singleton_unwrapped')
        self.assertEqual(issue['resolved_channel_id'], 300)

    def test_live_reference_cleanup_deduplicates_channel_lists(self):
        configured = server_settings()
        configured.server_list[GUILD_ID] = {
            **configured.server_list[GUILD_ID],
            'bot_channels': [300, 300],
        }

        report = storage.build_live_reference_cleanup_report(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot(),
        )
        value = storage.build_import_bundle(
            target=target(),
            server_settings=configured,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot(),
            normalize_live_references=True,
        )

        self.assertEqual(
            value.imports[0].document.channels.bot_channel_ids,
            (300,),
        )
        issue = next(
            issue for issue in report['guilds'][0]['issues']
            if issue['field'] == 'bot_channels'
        )
        self.assertEqual(issue['kind'], 'duplicate_channel_id')
        self.assertEqual(issue['resolution'], 'duplicate_dropped')

    def test_bundle_mapping_is_complete_and_returns_copies(self):
        value = bundle()
        mapping = storage.bundle_to_mapping(value)
        self.assertEqual(mapping['bundle_digest'], value.bundle_digest)
        self.assertEqual(len(mapping['planned_schema_statements']), 4)
        mapping['guilds'][0]['document']['permissions']['helper_role_ids'].append(999)
        self.assertEqual(value.imports[0].document.permissions.helper_role_ids, (201, 202))

        production = storage.bundle_to_mapping(
            value,
            target=production_target(),
        )
        self.assertEqual(len(production['planned_schema_statements']), 6)
        self.assertEqual(
            production['confirmation'],
            storage.confirmation_for_target(value, production_target()),
        )
        self.assertTrue(
            production['confirmation'].startswith(
                'PRODUCTION GUILD CONFIGURATION APPLY '
            )
        )
        self.assertEqual(
            production['online_static_staging_confirmation'],
            storage.online_staging_confirmation_for_target(
                value,
                production_target(),
            ),
        )
        self.assertEqual(
            storage.bundle_from_mapping(
                production,
                target=production_target(),
            ),
            value,
        )
        tampered = copy.deepcopy(production)
        tampered['guilds'][0]['document']['teams']['allow_teams'] = False
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'digest differs',
        ):
            storage.bundle_from_mapping(
                tampered,
                target=production_target(),
            )


class SchemaContractTests(unittest.TestCase):
    def test_absent_schema_is_distinct_from_exact_schema(self):
        self.assertFalse(storage.validate_schema_inventory(
            storage.SchemaInventory((), (), ())
        ))
        exact = storage.SchemaInventory(
            tuple(sorted(storage.STORAGE_TABLES)),
            storage.EXPECTED_COLUMNS,
            storage.EXPECTED_CONSTRAINTS,
        )
        self.assertTrue(storage.validate_schema_inventory(exact))

    def test_partial_or_drifted_schema_is_rejected(self):
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'partial'):
            storage.validate_schema_inventory(storage.SchemaInventory(
                (storage.REGISTRY_TABLE,), (), ()
            ))
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'columns'):
            storage.validate_schema_inventory(storage.SchemaInventory(
                tuple(sorted(storage.STORAGE_TABLES)),
                storage.EXPECTED_COLUMNS[:-1],
                storage.EXPECTED_CONSTRAINTS,
            ))
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'constraints'):
            storage.validate_schema_inventory(storage.SchemaInventory(
                tuple(sorted(storage.STORAGE_TABLES)),
                storage.EXPECTED_COLUMNS,
                storage.EXPECTED_CONSTRAINTS[:-1],
            ))

    def test_schema_sql_is_additive_bounded_and_has_no_drop(self):
        sql = '\n'.join(storage.CREATE_SCHEMA_STATEMENTS).upper()
        self.assertEqual(len(storage.CREATE_SCHEMA_STATEMENTS), 4)
        self.assertEqual(sql.count('CREATE TABLE'), 3)
        self.assertNotIn('DROP ', sql)
        self.assertNotIn('DELETE FROM', sql)
        self.assertNotIn('TRUNCATE ', sql)
        for table in storage.STORAGE_TABLES:
            self.assertIn(table.upper(), sql)


class DummyCursor:
    def __init__(self):
        self.statements = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement == 'SHOW transaction_read_only':
            self.row = ('off',)

    def fetchone(self):
        return self.row


class DummyConnection:
    def __init__(self):
        self.cursor_value = DummyCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class ApplyAndVerifyTests(unittest.TestCase):
    def exact_schema(self):
        return storage.SchemaInventory(
            tuple(sorted(storage.STORAGE_TABLES)),
            storage.EXPECTED_COLUMNS,
            storage.EXPECTED_CONSTRAINTS,
        )

    def test_confirmation_fails_before_opening_cursor(self):
        connection = mock.Mock()
        with self.assertRaisesRegex(storage.GuildConfigurationStorageError, 'confirmation'):
            storage.apply_storage(
                connection, target=target(), bundle=bundle(), confirmation='wrong'
            )
        connection.cursor.assert_not_called()

    def test_production_apply_requires_production_confirmation(self):
        connection = mock.Mock()
        value = bundle()
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'PRODUCTION GUILD CONFIGURATION APPLY',
        ):
            storage.apply_storage(
                connection,
                target=production_target(),
                bundle=value,
                confirmation=value.confirmation,
                production_mode=storage.PRODUCTION_MODE_MAINTENANCE,
            )
        connection.cursor.assert_not_called()

    def test_production_apply_requires_explicit_operation_mode(self):
        connection = mock.Mock()
        value = bundle()
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'explicit maintenance or online-static-stage mode',
        ):
            storage.apply_storage(
                connection,
                target=production_target(),
                bundle=value,
                confirmation=storage.confirmation_for_target(
                    value,
                    production_target(),
                ),
            )
        connection.cursor.assert_not_called()

    def test_online_stage_requires_its_distinct_confirmation(self):
        connection = mock.Mock()
        value = bundle()
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'ONLINE STATIC STAGE',
        ):
            storage.apply_storage(
                connection,
                target=production_target(),
                bundle=value,
                confirmation=storage.confirmation_for_target(
                    value,
                    production_target(),
                ),
                production_mode=storage.PRODUCTION_MODE_ONLINE_STATIC_STAGE,
            )
        connection.cursor.assert_not_called()

    def test_production_apply_creates_all_schema_and_uses_production_actor(self):
        connection = DummyConnection()
        value = bundle()
        confirmation = storage.confirmation_for_target(
            value,
            production_target(),
        )
        with mock.patch.object(
            storage,
            '_session_identity',
            return_value=(storage.PRODUCTION_DATABASE, storage.PRODUCTION_ROLE),
        ), mock.patch.object(
            storage, '_schema_inventory', return_value=self.exact_schema()
        ), mock.patch.object(
            storage, '_ensure_production_auxiliary_schema', return_value=True,
        ) as auxiliary, mock.patch.object(
            storage, '_registry_rows', return_value=()
        ), mock.patch.object(
            storage, '_insert_import'
        ) as insert, mock.patch.object(
            storage, '_verify_cursor', return_value=(GUILD_ID,)
        ) as verify:
            result = storage.apply_storage(
                connection,
                target=production_target(),
                bundle=value,
                confirmation=confirmation,
                production_mode=storage.PRODUCTION_MODE_MAINTENANCE,
            )
        self.assertTrue(result.schema_created)
        auxiliary.assert_called_once_with(connection.cursor_value)
        insert.assert_called_once_with(
            connection.cursor_value,
            value.imports[0],
            actor=storage.PRODUCTION_IMPORT_ACTOR,
        )
        verify.assert_called_once_with(
            connection.cursor_value,
            value,
            expected_actor=storage.PRODUCTION_IMPORT_ACTOR,
        )
        self.assertEqual(connection.commits, 1)

    def test_fresh_apply_creates_schema_imports_verifies_and_commits(self):
        connection = DummyConnection()
        value = bundle()
        with mock.patch.object(
            storage, '_session_identity', return_value=('polytopia_dev', 'polybot_dev')
        ), mock.patch.object(
            storage, '_schema_inventory', side_effect=[
                storage.SchemaInventory((), (), ()), self.exact_schema(),
            ]
        ), mock.patch.object(
            storage, '_registry_rows', return_value=()
        ), mock.patch.object(storage, '_insert_import') as insert, mock.patch.object(
            storage, '_verify_cursor', return_value=(GUILD_ID,)
        ):
            result = storage.apply_storage(
                connection, target=target(), bundle=value,
                confirmation=value.confirmation,
            )

        self.assertTrue(result.schema_created)
        self.assertEqual(result.imported_guild_ids, (GUILD_ID,))
        self.assertEqual(result.unchanged_guild_ids, ())
        insert.assert_called_once_with(connection.cursor_value, value.imports[0])
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        statements = [row[0] for row in connection.cursor_value.statements]
        for statement in storage.CREATE_SCHEMA_STATEMENTS:
            self.assertIn(statement, statements)

    def test_exact_repeat_is_verified_noop(self):
        connection = DummyConnection()
        value = bundle()
        existing = ((GUILD_ID, 1, 'active', 1, 1),)
        with mock.patch.object(
            storage, '_session_identity', return_value=('polytopia_dev', 'polybot_dev')
        ), mock.patch.object(
            storage, '_schema_inventory', return_value=self.exact_schema()
        ), mock.patch.object(
            storage, '_registry_rows', return_value=existing
        ), mock.patch.object(storage, '_insert_import') as insert, mock.patch.object(
            storage, '_verify_cursor', return_value=(GUILD_ID,)
        ):
            result = storage.apply_storage(
                connection, target=target(), bundle=value,
                confirmation=value.confirmation,
            )
        self.assertFalse(result.schema_created)
        self.assertEqual(result.imported_guild_ids, ())
        self.assertEqual(result.unchanged_guild_ids, (GUILD_ID,))
        insert.assert_not_called()
        self.assertEqual(connection.commits, 1)

    def test_failure_rolls_back_schema_and_import_transaction(self):
        connection = DummyConnection()
        value = bundle()
        with mock.patch.object(
            storage, '_session_identity', return_value=('polytopia_dev', 'polybot_dev')
        ), mock.patch.object(
            storage, '_schema_inventory', return_value=self.exact_schema()
        ), mock.patch.object(
            storage, '_registry_rows', return_value=()
        ), mock.patch.object(
            storage, '_insert_import', side_effect=RuntimeError('simulated insert failure')
        ):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                storage.apply_storage(
                    connection, target=target(), bundle=value,
                    confirmation=value.confirmation,
                )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_unexpected_registry_guild_rolls_back_without_insert(self):
        connection = DummyConnection()
        value = bundle()
        with mock.patch.object(
            storage, '_session_identity', return_value=('polytopia_dev', 'polybot_dev')
        ), mock.patch.object(
            storage, '_schema_inventory', return_value=self.exact_schema()
        ), mock.patch.object(
            storage, '_registry_rows', return_value=((999, 1, 'active', 1, 1),)
        ), mock.patch.object(storage, '_insert_import') as insert:
            with self.assertRaisesRegex(
                storage.GuildConfigurationStorageError, 'outside'
            ):
                storage.apply_storage(
                    connection, target=target(), bundle=value,
                    confirmation=value.confirmation,
                )
        insert.assert_not_called()
        self.assertEqual(connection.rollbacks, 1)

    def test_verify_is_read_only_and_uses_exact_bundle(self):
        connection = DummyConnection()
        value = bundle()
        with mock.patch.object(
            storage, '_session_identity', return_value=('polytopia_dev', 'polybot_dev')
        ), mock.patch.object(
            storage, '_verify_cursor', return_value=(GUILD_ID,)
        ) as verify:
            result = storage.verify_storage(connection, target=target(), bundle=value)
        verify.assert_called_once_with(connection.cursor_value, value)
        self.assertEqual(result.verified_guild_ids, (GUILD_ID,))
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 0)


class ScriptTests(unittest.TestCase):
    def profile(self):
        return SimpleNamespace(
            environment='development',
            database_name='polytopia_dev',
            database_user='polybot_dev',
            database_password='secret',
            database_host='localhost',
            database_port=5432,
            expected_bot_id=storage.DEVELOPMENT_BETA_APPLICATION_ID,
            background_tasks_enabled=False,
            api_enabled=False,
            bullet_enabled=False,
            allowed_guild_ids=(GUILD_ID,),
            server_settings=server_settings(),
        )

    def production_profile(self, *, source='static'):
        profile = self.profile()
        profile.environment = storage.PRODUCTION_ENVIRONMENT
        profile.database_name = storage.PRODUCTION_DATABASE
        profile.database_user = storage.PRODUCTION_ROLE
        profile.expected_bot_id = storage.PRODUCTION_APPLICATION_ID
        profile.background_tasks_enabled = True
        profile.bullet_enabled = True
        profile.guild_configuration_source = source
        profile.allowed_guild_ids = tuple(range(1, 48)) + (
            script.POLYCHAMPIONS_GUILD_ID,
            script.PCPLUS_GUILD_ID,
        )
        return profile

    def test_plan_is_offline_and_does_not_open_database_or_discord(self):
        value = bundle()
        emitted = []
        with mock.patch.dict(os.environ, {'POLYBOT_ENV': 'development'}, clear=True), \
                mock.patch.object(script, '_profile', return_value=self.profile()), \
                mock.patch.object(script, '_bundle', return_value=value), \
                mock.patch.object(script, '_connection') as connection, \
                mock.patch.object(script, '_capture_snapshot') as capture, \
                mock.patch.object(script, '_emit', side_effect=emitted.append):
            result = script.main(['plan', '--snapshot', script.DEFAULT_SNAPSHOT])
        self.assertEqual(result, 0)
        connection.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(emitted[0]['bundle_digest'], value.bundle_digest)

    def test_owner_inventory_is_exact_and_bound_to_allowed_guilds(self):
        value = script._validate_owner_inventory(
            owner_inventory(),
            profile=self.profile(),
        )
        self.assertEqual(value[GUILD_ID]['owner_name'], 'guild-owner')

        invalid = owner_inventory()
        invalid['owners'][0]['owner_id'] = 0
        with self.assertRaisesRegex(
            storage.GuildConfigurationStorageError,
            'row is invalid',
        ):
            script._validate_owner_inventory(
                invalid,
                profile=self.profile(),
            )

    def test_apply_requires_exact_environment_before_profile_or_connection(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(script, 'load_runtime_profile') as profile, \
                mock.patch.object(script, '_connection') as connection:
            result = script.main([
                'apply', '--snapshot', script.DEFAULT_SNAPSHOT, '--confirm', 'no',
            ])
        self.assertEqual(result, 2)
        profile.assert_not_called()
        connection.assert_not_called()

    def test_production_inventory_has_one_league_one_team_and_47_standard(self):
        profile = self.profile()
        profile.environment = storage.PRODUCTION_ENVIRONMENT
        profile.allowed_guild_ids = tuple(range(1, 48)) + (
            script.POLYCHAMPIONS_GUILD_ID,
            script.PCPLUS_GUILD_ID,
        )
        values = script._production_guild_types(profile)
        self.assertEqual(len(values), 49)
        self.assertEqual(values[script.POLYCHAMPIONS_GUILD_ID], 'league')
        self.assertEqual(values[script.PCPLUS_GUILD_ID], 'team')
        self.assertEqual(
            sum(value == 'standard' for value in values.values()),
            47,
        )

    def test_production_bundle_summary_enforces_accepted_policy(self):
        guild_ids = tuple(range(1, 46)) + tuple(
            script.PRODUCTION_GLOBAL_LEADERBOARD_GUILD_IDS
        ) + (
            script.POLYCHAMPIONS_GUILD_ID,
            script.PCPLUS_GUILD_ID,
        )
        guild_ids = tuple(sorted(set(guild_ids)))
        self.assertEqual(len(guild_ids), 49)
        base = bundle().imports[0].document
        imports = []
        for guild_id in guild_ids:
            guild_type = (
                guild_types.LEAGUE
                if guild_id == script.POLYCHAMPIONS_GUILD_ID
                else guild_types.TEAM
                if guild_id == script.PCPLUS_GUILD_ID
                else guild_types.STANDARD
            )
            document = guild_types.apply_guild_type(
                base,
                guild_type,
                include_in_global_leaderboard=(
                    guild_id
                    in script.PRODUCTION_GLOBAL_LEADERBOARD_GUILD_IDS
                ),
            )
            imports.append(storage.GuildImport(
                guild_id=guild_id,
                document=document,
                document_digest='a' * 64,
                source_digest='b' * 64,
            ))
        value = storage.ImportBundle(1, 1, tuple(imports), 'c' * 64)
        summary = script._validate_production_bundle(value)
        self.assertEqual(summary['guild_count'], 49)
        self.assertEqual(summary['standard_guild_count'], 47)
        self.assertEqual(
            summary['team_guild_ids'],
            [script.PCPLUS_GUILD_ID],
        )
        self.assertEqual(
            summary['league_guild_ids'],
            [script.POLYCHAMPIONS_GUILD_ID],
        )

    def test_production_apply_requires_maintenance_before_database_connection(self):
        profile = self.production_profile()
        with mock.patch.dict(
                os.environ, {'POLYBOT_ENV': 'production'}, clear=True), \
                mock.patch.object(script, '_profile', return_value=profile), \
                mock.patch.object(
                    script, '_bundle', return_value=bundle()
                ) as build_bundle, \
                mock.patch.object(script, '_connection') as connection:
            result = script.main([
                'apply', '--snapshot', script.PRODUCTION_DEFAULT_SNAPSHOT,
                '--confirm', bundle().confirmation,
            ])
        self.assertEqual(result, 2)
        build_bundle.assert_not_called()
        connection.assert_not_called()

    def test_acknowledged_production_apply_uses_no_beta_writer_lock(self):
        profile = self.production_profile()
        value = bundle()
        result_value = storage.StorageResult(
            True, (GUILD_ID,), (), (GUILD_ID,), value.bundle_digest,
        )
        connection_value = mock.Mock()
        with mock.patch.dict(
                os.environ, {'POLYBOT_ENV': 'production'}, clear=True), \
                mock.patch.object(script, '_profile', return_value=profile), \
                mock.patch.object(script, '_bundle', return_value=value), \
                mock.patch.object(
                    script, '_connection', return_value=connection_value,
                ), mock.patch.object(
                    script.storage, 'apply_storage', return_value=result_value,
                ) as apply, mock.patch.object(
                    script.beta_database_writer_lock, 'BetaDatabaseWriterLock',
                ) as beta_lock:
            result = script.main([
                'apply',
                '--snapshot', script.PRODUCTION_DEFAULT_SNAPSHOT,
                '--confirm', storage.confirmation_for_target(
                    value, production_target()
                ),
                '--production-maintenance',
            ])
        self.assertEqual(result, 0)
        beta_lock.assert_not_called()
        apply.assert_called_once_with(
            connection_value,
            target=production_target(),
            bundle=value,
            confirmation=storage.confirmation_for_target(
                value,
                production_target(),
            ),
            production_mode=storage.PRODUCTION_MODE_MAINTENANCE,
        )
        connection_value.close.assert_called_once()

    def test_online_stage_refuses_database_authority_before_bundle_or_connection(self):
        profile = self.production_profile(source='database')
        with mock.patch.dict(
                os.environ, {'POLYBOT_ENV': 'production'}, clear=True), \
                mock.patch.object(script, '_profile', return_value=profile), \
                mock.patch.object(script, '_bundle') as build_bundle, \
                mock.patch.object(script, '_connection') as connection:
            result = script.main([
                'stage',
                '--snapshot', script.PRODUCTION_DEFAULT_SNAPSHOT,
                '--confirm', 'irrelevant',
            ])
        self.assertEqual(result, 2)
        build_bundle.assert_not_called()
        connection.assert_not_called()

    def test_online_stage_uses_distinct_confirmation_and_no_beta_writer_lock(self):
        profile = self.production_profile()
        value = bundle()
        result_value = storage.StorageResult(
            True, (GUILD_ID,), (), (GUILD_ID,), value.bundle_digest,
        )
        connection_value = mock.Mock()
        confirmation = storage.online_staging_confirmation_for_target(
            value,
            production_target(),
        )
        with mock.patch.dict(
                os.environ, {'POLYBOT_ENV': 'production'}, clear=True), \
                mock.patch.object(script, '_profile', return_value=profile), \
                mock.patch.object(script, '_bundle', return_value=value), \
                mock.patch.object(
                    script, '_connection', return_value=connection_value,
                ), mock.patch.object(
                    script.storage, 'apply_storage', return_value=result_value,
                ) as apply, mock.patch.object(
                    script.beta_database_writer_lock, 'BetaDatabaseWriterLock',
                ) as beta_lock:
            result = script.main([
                'stage',
                '--snapshot', script.PRODUCTION_DEFAULT_SNAPSHOT,
                '--confirm', confirmation,
            ])
        self.assertEqual(result, 0)
        beta_lock.assert_not_called()
        apply.assert_called_once_with(
            connection_value,
            target=production_target(),
            bundle=value,
            confirmation=confirmation,
            production_mode=storage.PRODUCTION_MODE_ONLINE_STATIC_STAGE,
        )
        connection_value.close.assert_called_once()

    def test_snapshot_path_is_private_bounded_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(script, 'PROJECT_ROOT', root):
                path = script._write_snapshot(script.DEFAULT_SNAPSHOT, snapshot())
                self.assertTrue(path.is_file())
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                loaded = script._load_snapshot(script.DEFAULT_SNAPSHOT)
                self.assertEqual(loaded, snapshot())
                with self.assertRaisesRegex(
                    storage.GuildConfigurationStorageError, 'guild-configuration'
                ):
                    script._write_snapshot('source.json', snapshot())

    def test_snapshot_path_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'logs').symlink_to(root / 'elsewhere')
            with mock.patch.object(script, 'PROJECT_ROOT', root):
                with self.assertRaisesRegex(
                    storage.GuildConfigurationStorageError, 'symlink'
                ):
                    script._write_snapshot(script.DEFAULT_SNAPSHOT, snapshot())


if __name__ == '__main__':
    unittest.main()
