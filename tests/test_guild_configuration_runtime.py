"""Focused offline coverage for P10.5 database runtime authority."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import re
from types import MappingProxyType, SimpleNamespace
import unittest

from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_shadow as shadow
from tests import test_guild_configuration_storage as fixtures
import settings


GUILD_ID = fixtures.GUILD_ID
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def matched_result():
    imported = fixtures.bundle().imports[0]
    stored = shadow.StoredGuildConfiguration(
        guild_id=GUILD_ID,
        storage_schema_version=1,
        enrollment_state='active',
        active_revision=1,
        generation=1,
        document=imported.document,
        document_digest=imported.document_digest,
        source_digest=imported.source_digest,
    )
    return shadow.GuildConfigurationShadowResult(
        status=shadow.STATUS_MATCHED,
        expected_guild_ids=(GUILD_ID,),
        stored_guild_ids=(GUILD_ID,),
        matched_guild_ids=(GUILD_ID,),
        stored_configurations=(stored,),
    )


def snapshot():
    return runtime.build_runtime_snapshot(
        result=matched_result(),
        discord_snapshot=fixtures.snapshot(),
        allowed_guild_ids=(GUILD_ID,),
    )


class RuntimeSnapshotTests(unittest.TestCase):
    def test_matched_graph_builds_complete_immutable_legacy_view(self):
        value = snapshot()
        imported = fixtures.bundle().imports[0]
        guild = value.guilds[GUILD_ID]

        self.assertEqual(value.source, 'database')
        self.assertEqual(guild.document, imported.document)
        self.assertEqual(guild.document_digest, imported.document_digest)
        self.assertEqual(guild.revision, 1)
        self.assertEqual(guild.generation, 1)
        self.assertEqual(
            value.command_policy.capabilities_for_guild(GUILD_ID),
            imported.document.command_capabilities,
        )
        self.assertEqual(
            set(guild.legacy_settings),
            {
                'display_name', 'command_prefix', 'helper_roles', 'mod_roles',
                'user_roles_level_1', 'user_roles_level_2',
                'user_roles_level_3', 'user_roles_level_4', 'inactive_role',
                'require_teams', 'allow_teams', 'allow_uneven_teams',
                'max_team_size', 'include_in_global_lb', 'bot_channels',
                'bot_channels_strict', 'bot_channels_private',
                'newbie_message_channels', 'match_challenge_channels',
                'ranked_game_channel', 'unranked_game_channel',
                'steam_game_channel', 'log_channel', 'game_announce_channel',
                'staff_help_channel', 'game_channel_categories',
            },
        )
        helper_id = imported.document.permissions.helper_role_ids[0]
        self.assertEqual(guild.role_ids['helper_roles'][0], helper_id)
        self.assertIsInstance(guild.legacy_settings, MappingProxyType)
        self.assertIsInstance(value.guilds, MappingProxyType)
        with self.assertRaises(TypeError):
            guild.legacy_settings['command_prefix'] = '!'
        with self.assertRaises(FrozenInstanceError):
            guild.generation = 2

    def test_nonmatched_incomplete_or_deleted_role_blocks_publication(self):
        value = matched_result()
        mismatch = shadow.GuildConfigurationShadowResult(
            status=shadow.STATUS_MISMATCH,
        )
        with self.assertRaisesRegex(
            runtime.GuildConfigurationRuntimeError,
            'not_promotion_ready',
        ):
            runtime.build_runtime_snapshot(
                result=mismatch,
                discord_snapshot=fixtures.snapshot(),
                allowed_guild_ids=(GUILD_ID,),
            )

        without_stored = shadow.GuildConfigurationShadowResult(
            status=value.status,
            expected_guild_ids=value.expected_guild_ids,
            stored_guild_ids=value.stored_guild_ids,
            matched_guild_ids=value.matched_guild_ids,
        )
        with self.assertRaisesRegex(
            runtime.GuildConfigurationRuntimeError,
            'stored_inventory_incomplete',
        ):
            runtime.build_runtime_snapshot(
                result=without_stored,
                discord_snapshot=fixtures.snapshot(),
                allowed_guild_ids=(GUILD_ID,),
            )

        discord_snapshot = fixtures.snapshot()
        helper_id = fixtures.bundle().imports[0].document.permissions.helper_role_ids[0]
        discord_snapshot['guilds'][0]['roles'] = [
            role for role in discord_snapshot['guilds'][0]['roles']
            if role['id'] != helper_id
        ]
        with self.assertRaisesRegex(
            runtime.GuildConfigurationRuntimeError,
            'configured_role_unavailable',
        ):
            runtime.build_runtime_snapshot(
                result=value,
                discord_snapshot=discord_snapshot,
                allowed_guild_ids=(GUILD_ID,),
            )

    def test_runtime_consumers_do_not_bypass_the_settings_facade(self):
        permitted_direct_membership = {
            'bot.py',
            'modules/antiscam.py',
        }
        offenders = []
        for path in (
                PROJECT_ROOT / 'bot.py',
                *(PROJECT_ROOT / 'modules').glob('*.py')):
            source = path.read_text(encoding='utf-8')
            if re.search(r'\bsettings\.config\b', source) and str(path.relative_to(PROJECT_ROOT)) not in permitted_direct_membership:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
            if (
                    'server_settings.server_list' in source
                    and path.name not in {
                        'guild_configuration_storage.py',
                    }
                    and path.name != 'settings.py'
            ):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [])


class SettingsAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.original = (
            settings.guild_configuration_source,
            settings._database_guild_configuration,
            settings.config,
            settings.application_command_policy,
        )

    def tearDown(self):
        (
            settings.guild_configuration_source,
            settings._database_guild_configuration,
            settings.config,
            settings.application_command_policy,
        ) = self.original

    def test_database_source_is_unready_until_one_snapshot_is_published(self):
        settings.guild_configuration_source = 'database'
        settings._database_guild_configuration = None
        self.assertFalse(settings.guild_configuration_ready())
        self.assertIsNone(settings.database_guild_configuration(GUILD_ID))

        value = snapshot()
        settings.activate_database_guild_configuration(value)
        self.assertTrue(settings.guild_configuration_ready())
        self.assertIs(
            settings.database_guild_configuration(GUILD_ID),
            value.guilds[GUILD_ID],
        )
        self.assertIsNone(settings.database_guild_configuration(GUILD_ID + 1))
        self.assertIs(settings.config, value.legacy_config)
        self.assertEqual(
            settings.guild_setting(GUILD_ID, 'command_prefix'),
            value.guilds[GUILD_ID].document.identity.command_prefix,
        )
        settings.activate_database_guild_configuration(value)

        different = runtime.GuildConfigurationRuntimeSnapshot(
            source='changed',
            guilds=value.guilds,
            legacy_config=value.legacy_config,
            command_policy=value.command_policy,
        )
        with self.assertRaisesRegex(RuntimeError, 'already published'):
            settings.activate_database_guild_configuration(different)

    def test_permissions_and_role_resolution_use_stable_database_ids(self):
        settings.guild_configuration_source = 'database'
        settings._database_guild_configuration = None
        value = snapshot()
        settings.activate_database_guild_configuration(value)
        helper_id = value.guilds[GUILD_ID].role_ids['helper_roles'][0]
        renamed_role = SimpleNamespace(id=helper_id, name='Renamed Helper')
        member = SimpleNamespace(
            id=settings.owner_id + 100,
            guild=SimpleNamespace(id=GUILD_ID),
            roles=(renamed_role,),
        )
        guild = SimpleNamespace(
            id=GUILD_ID,
            roles=(renamed_role,),
            get_role=lambda role_id: renamed_role if role_id == helper_id else None,
        )

        self.assertTrue(settings.is_staff(member))
        self.assertIs(
            settings.resolve_configured_role(guild, 'helper_roles'),
            renamed_role,
        )
        self.assertNotEqual(
            settings.guild_setting(GUILD_ID, 'helper_roles')[0],
            renamed_role.name,
        )

    def test_static_source_refuses_database_publication(self):
        settings.guild_configuration_source = 'static'
        settings._database_guild_configuration = None
        self.assertTrue(settings.guild_configuration_ready())
        with self.assertRaisesRegex(RuntimeError, 'not selected'):
            settings.activate_database_guild_configuration(snapshot())


if __name__ == '__main__':
    unittest.main()
