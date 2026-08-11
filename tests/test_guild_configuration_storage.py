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


def bundle() -> storage.ImportBundle:
    return storage.build_import_bundle(
        target=target(),
        server_settings=server_settings(),
        allowed_guild_ids=(GUILD_ID,),
        discord_snapshot=snapshot(),
    )


class TargetAndSnapshotTests(unittest.TestCase):
    def test_target_is_exactly_development_and_effects_disabled(self):
        storage.validate_target(target())
        for changes, pattern in (
            ({'environment': 'production'}, 'development-only'),
            ({'database_name': 'polytopia2'}, 'polytopia_dev'),
            ({'database_user': 'prod'}, 'polybot_dev'),
            ({'expected_application_id': 1}, 'application'),
            ({'background_tasks_enabled': True}, 'disabled'),
            ({'api_enabled': True}, 'disabled'),
            ({'bullet_enabled': True}, 'disabled'),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(
                storage.GuildConfigurationStorageError,
                pattern,
            ):
                storage.validate_target(target(**changes))

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

    def test_bundle_mapping_is_complete_and_returns_copies(self):
        value = bundle()
        mapping = storage.bundle_to_mapping(value)
        self.assertEqual(mapping['bundle_digest'], value.bundle_digest)
        self.assertEqual(len(mapping['planned_schema_statements']), 4)
        mapping['guilds'][0]['document']['permissions']['helper_role_ids'].append(999)
        self.assertEqual(value.imports[0].document.permissions.helper_role_ids, (201, 202))


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
