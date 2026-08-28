"""Focused offline coverage for owner-facing guild types."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from modules import guild_types
from modules import operator_guild_enrollment_workers as enrollment
from modules import guild_configuration_storage as storage
from tests import test_guild_configuration_runtime as runtime_fixtures
from tests import test_guild_configuration_storage as fixtures


GUILD_ID = fixtures.GUILD_ID
OWNER_ID = int(enrollment.settings.owner_id)


class GuildTypePolicyTests(unittest.TestCase):
    def test_type_is_derived_from_existing_protected_team_keys(self):
        standard = enrollment.basic_prefix_document(
            guild_id=GUILD_ID,
            guild_name='Standard Guild',
        )
        team = guild_types.apply_guild_type(standard, guild_types.TEAM)
        league = guild_types.apply_guild_type(team, guild_types.LEAGUE)

        self.assertEqual(
            tuple(guild_types.guild_type_for_document(value) for value in (
                standard, team, league,
            )),
            guild_types.GUILD_TYPES,
        )
        self.assertFalse(standard.teams.allow_teams)
        self.assertTrue(team.teams.allow_teams)
        self.assertFalse(team.teams.require_teams)
        self.assertTrue(league.teams.require_teams)

    def test_type_derives_commands_but_preserves_operational_overlay(self):
        current = fixtures.bundle().imports[0].document
        league = guild_types.apply_guild_type(current, guild_types.LEAGUE)
        standard = guild_types.apply_guild_type(league, guild_types.STANDARD)

        self.assertIn('team', league.command_capabilities)
        self.assertIn('league', league.command_capabilities)
        self.assertIn('house', league.command_capabilities)
        self.assertNotIn('team', standard.command_capabilities)
        self.assertNotIn('league', standard.command_capabilities)
        self.assertIn('squad', standard.command_capabilities)
        self.assertIn('operator', standard.command_capabilities)
        self.assertIn('tools_support', standard.command_capabilities)

    def test_global_leaderboard_is_independent_of_type(self):
        current = enrollment.basic_prefix_document(
            guild_id=GUILD_ID,
            guild_name='Guild',
        )
        enabled = guild_types.apply_guild_type(
            current,
            guild_types.STANDARD,
            include_in_global_leaderboard=True,
        )
        team = guild_types.apply_guild_type(enabled, guild_types.TEAM)

        self.assertTrue(enabled.visibility.include_in_global_leaderboard)
        self.assertTrue(team.visibility.include_in_global_leaderboard)


class ExistingGuildPreviewTests(unittest.TestCase):
    @staticmethod
    def profile():
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
            guild_configuration_source='database',
        )

    def request(self):
        current = runtime_fixtures.snapshot().guilds[GUILD_ID]
        return enrollment.request_from_profile(
            profile=self.profile(),
            requester_id=OWNER_ID,
            invoking_guild_id=GUILD_ID,
            target_guild_id=GUILD_ID,
            target_guild_name=current.document.identity.display_name,
            template=enrollment.BASIC_PREFIX_TEMPLATE,
            guild_type=guild_types.STANDARD,
            include_in_global_leaderboard=None,
            bot_permissions=tuple(sorted(enrollment.REQUIRED_BOT_PERMISSIONS)),
            current_runtime_records=(current,),
            forbidden_guild_ids=(GUILD_ID,),
            discord_snapshot=fixtures.snapshot(),
        )

    def test_enroll_command_can_preview_existing_type_change_without_resetting(self):
        current = runtime_fixtures.snapshot().guilds[GUILD_ID]
        request = self.request()
        preview = enrollment._preview(request)

        self.assertTrue(preview.existing)
        self.assertEqual(preview.guild_type, guild_types.STANDARD)
        self.assertTrue(preview.confirmation.startswith(f'UPDATE GUILD {GUILD_ID} '))
        self.assertEqual(
            preview.document.teams.max_team_size,
            current.document.teams.max_team_size,
        )
        self.assertEqual(
            preview.document.visibility.include_in_global_leaderboard,
            current.document.visibility.include_in_global_leaderboard,
        )
        self.assertFalse(preview.document.teams.allow_teams)
        self.assertIn('squad', preview.document.command_capabilities)
        self.assertNotIn('team', preview.document.command_capabilities)

    def test_existing_type_change_creates_one_revision_without_discord_sync(self):
        request = self.request()
        preview = enrollment._preview(request)
        current = runtime_fixtures.snapshot().guilds[GUILD_ID]
        draft = mock.sentinel.draft
        activation = SimpleNamespace(
            revision=current.revision + 1,
            generation=current.generation + 1,
            event_number=9,
            document_digest=preview.document_digest,
            actor=f'discord:{OWNER_ID}',
            document=preview.document,
        )
        with mock.patch.object(
            enrollment.drafts,
            'select_revision',
            return_value=(current.document, current.document_digest),
        ), mock.patch.object(
            enrollment.drafts, 'select_draft', return_value=None,
        ), mock.patch.object(
            enrollment.drafts, 'put_draft', return_value=draft,
        ) as put, mock.patch.object(
            enrollment.drafts, 'activate_draft', return_value=activation,
        ) as activate:
            result = enrollment._update_enrollment(
                mock.sentinel.cursor,
                request,
                preview,
            )

        self.assertFalse(result.created)
        self.assertEqual(result.revision, current.revision + 1)
        put.assert_called_once()
        activate.assert_called_once()
        self.assertNotIn('command_plan_digest', activate.call_args.kwargs)

    def test_existing_type_change_expires_unchanged_abandoned_draft(self):
        request = self.request()
        preview = enrollment._preview(request)
        current = runtime_fixtures.snapshot().guilds[GUILD_ID]
        existing_draft = SimpleNamespace(
            base_revision=current.revision,
            base_generation=current.generation,
            document_digest=current.document_digest,
            draft_version=13,
        )
        activation = SimpleNamespace(
            revision=current.revision + 1,
            generation=current.generation + 1,
            event_number=9,
            document_digest=preview.document_digest,
            actor=f'discord:{OWNER_ID}',
            document=preview.document,
        )
        with mock.patch.object(
            enrollment.drafts, 'select_revision',
            return_value=(current.document, current.document_digest),
        ), mock.patch.object(
            enrollment.drafts, 'select_draft', return_value=existing_draft,
        ), mock.patch.object(
            enrollment.drafts, 'expire_draft',
        ) as expire, mock.patch.object(
            enrollment.drafts, 'put_draft', return_value=mock.sentinel.draft,
        ), mock.patch.object(
            enrollment.drafts, 'activate_draft', return_value=activation,
        ):
            enrollment._update_enrollment(
                mock.sentinel.cursor,
                request,
                preview,
            )

        expire.assert_called_once_with(
            mock.sentinel.cursor,
            guild_id=GUILD_ID,
            expected_version=13,
            expected_digest=current.document_digest,
            actor=f'discord:{OWNER_ID}',
        )

    def test_existing_type_change_preserves_draft_with_unsaved_changes(self):
        request = self.request()
        preview = enrollment._preview(request)
        current = runtime_fixtures.snapshot().guilds[GUILD_ID]
        existing_draft = SimpleNamespace(
            base_revision=current.revision,
            base_generation=current.generation,
            document_digest='f' * 64,
            draft_version=13,
        )
        with mock.patch.object(
            enrollment.drafts, 'select_revision',
            return_value=(current.document, current.document_digest),
        ), mock.patch.object(
            enrollment.drafts, 'select_draft', return_value=existing_draft,
        ), mock.patch.object(
            enrollment.drafts, 'expire_draft',
        ) as expire, self.assertRaisesRegex(
            enrollment.OperatorGuildEnrollmentConflict, 'unsaved changes'
        ):
            enrollment._update_enrollment(
                mock.sentinel.cursor,
                request,
                preview,
            )

        expire.assert_not_called()


if __name__ == '__main__':
    unittest.main()
