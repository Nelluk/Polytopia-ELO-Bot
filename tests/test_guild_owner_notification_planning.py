"""Offline coverage for private, plan-only guild-owner notices."""

from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import guild_configuration_storage as storage
from scripts import plan_guild_owner_notifications as notifications
from tests import test_guild_configuration_storage as fixtures


def profile():
    return SimpleNamespace(
        environment=storage.PRODUCTION_ENVIRONMENT,
        database_name=storage.PRODUCTION_DATABASE,
        database_user=storage.PRODUCTION_ROLE,
        expected_bot_id=storage.PRODUCTION_APPLICATION_ID,
        background_tasks_enabled=True,
        api_enabled=False,
        bullet_enabled=True,
        allowed_guild_ids=(fixtures.GUILD_ID,),
    )


def import_plan(*, category='guild_administration_access', severity='review_before_cutover'):
    value = fixtures.bundle()
    mapping = storage.bundle_to_mapping(
        value,
        target=fixtures.production_target(),
    )
    mapping['production_migration_summary'] = {'guild_count': 1}
    mapping['production_cleanup_report'] = {
        'schema_version': 1,
        'policy': 'preserve_effective_static_references',
        'summary': {'guild_count': 1},
        'guilds': [{
            'guild_id': fixtures.GUILD_ID,
            'guild_name': 'Owner Test Guild',
            'owner': {
                'owner_id': 900000000000000123,
                'owner_name': 'guild-owner',
            },
            'severity': severity,
            'remaining': {
                'helper_role_count': 0,
                'mod_role_count': 1,
                'bot_channel_count': 1,
                'strict_bot_channel_count': 1,
                'private_bot_channel_count': 0,
            },
            'issues': [{
                'category': category,
                'field': 'helper_roles',
                'kind': 'missing_role',
                'configured_value': 'Old Helper',
                'resolution': 'dropped',
            }],
        }],
    }
    return mapping


class OwnerNotificationPlanningTests(unittest.TestCase):
    def test_review_plan_groups_private_recipient_without_sending(self):
        plan = notifications.build_plan(
            import_plan(),
            profile=profile(),
            scope='review',
            guild_ids='all',
        )

        self.assertEqual(plan['recipient_count'], 1)
        self.assertEqual(plan['guild_count'], 1)
        self.assertEqual(plan['messages_sent'], 0)
        self.assertFalse(plan['discord_connected'])
        self.assertFalse(plan['database_connected'])
        recipient = plan['recipients'][0]
        self.assertEqual(recipient['owner_id'], 900000000000000123)
        self.assertEqual(plan['message_count'], 1)
        message = recipient['messages'][0]
        self.assertLessEqual(
            len(message),
            notifications.MAX_MESSAGE_CHARACTERS,
        )
        self.assertIn('Owner Test Guild', message)
        self.assertIn('Old Helper', message)
        self.assertIn('/guild settings', message)

    def test_scope_filters_do_not_broaden_targets(self):
        plan = notifications.build_plan(
            import_plan(
                category='ordinary_user_access',
                severity='informational',
            ),
            profile=profile(),
            scope='access',
            guild_ids='all',
        )
        self.assertEqual(plan['recipient_count'], 0)
        self.assertEqual(plan['selected_guild_ids'], [])

    def test_singleton_channel_cleanup_notice_says_destination_is_preserved(self):
        text = notifications._issue_text({
            'field': 'ranked_game_channel',
            'kind': 'singleton_channel_list',
            'configured_value': [300],
            'resolution': 'singleton_unwrapped',
            'resolved_channel_id': 300,
        })

        self.assertIn('300', text)
        self.assertIn('preserved', text)
        self.assertNotIn('cleared', text)

    def test_cleanup_owner_identity_must_match_digest_bound_guild_set(self):
        mapping = import_plan()
        mapping['production_cleanup_report']['guilds'][0]['owner']['owner_id'] = 0
        with self.assertRaisesRegex(
            notifications.OwnerNotificationPlanError,
            'owner row',
        ):
            notifications.build_plan(
                mapping,
                profile=profile(),
                scope='all',
                guild_ids='all',
            )

    def test_main_refuses_before_profile_or_output_outside_production(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            notifications,
            'load_runtime_profile',
        ) as load_profile, mock.patch.object(
            notifications.manager,
            '_write_snapshot',
        ) as write:
            result = notifications.main(['plan'])
        self.assertEqual(result, 2)
        load_profile.assert_not_called()
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
