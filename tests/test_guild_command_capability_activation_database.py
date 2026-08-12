"""Explicitly gated, rolled-back P10.6c capability-activation proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import document_to_mapping, validate_document
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_6C_COMMAND_CAPABILITY_INTEGRATION'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.6c database verification',
)
class GuildCommandCapabilityActivationDatabaseTests(unittest.TestCase):
    def test_capability_revision_and_plan_audit_roll_back_together(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        self.assertEqual(profile.guild_configuration_source, 'database')
        target = storage.StorageTarget(
            environment=profile.environment,
            database_name=profile.database_name,
            database_user=profile.database_user,
            expected_application_id=profile.expected_bot_id,
            background_tasks_enabled=profile.background_tasks_enabled,
            api_enabled=profile.api_enabled,
            bullet_enabled=profile.bullet_enabled,
        )
        storage.validate_target(target)

        import psycopg2

        connection = psycopg2.connect(
            dbname=profile.database_name,
            user=profile.database_user,
            password=profile.database_password,
            host=profile.database_host,
            port=profile.database_port,
        )
        try:
            connection.set_session(
                readonly=False,
                autocommit=False,
                isolation_level='REPEATABLE READ',
            )
            with connection.cursor() as cursor:
                drafts._validate_live_connection(cursor, target)
                self.assertTrue(drafts.validate_draft_schema(
                    drafts.inspect_draft_schema(cursor)
                ))
                guild_id = profile.allowed_guild_ids[0]
                revision, generation, document, digest = (
                    drafts.select_active_configuration(
                        cursor,
                        guild_id,
                        for_update=True,
                    )
                )
                mapping = document_to_mapping(document)
                capabilities = list(document.command_capabilities)
                if 'beta_testing' in capabilities:
                    capabilities.remove('beta_testing')
                else:
                    capabilities.append('beta_testing')
                mapping['command_capabilities'] = capabilities
                candidate = validate_document(mapping)
                created = drafts.put_draft(
                    cursor,
                    guild_id=guild_id,
                    base_revision=revision,
                    base_generation=generation,
                    document=candidate,
                    actor='integration:p10.6c',
                )
                plan_digest = 'c' * 64
                activation = drafts.activate_draft(
                    cursor,
                    draft=created,
                    active_revision=revision,
                    active_generation=generation,
                    active_document_digest=digest,
                    actor='integration:p10.6c',
                    changed_paths=('command_capabilities',),
                    command_plan_digest=plan_digest,
                )
                selected = drafts.select_active_configuration(
                    cursor,
                    guild_id,
                    for_update=False,
                )
                self.assertEqual(selected[2].command_capabilities, candidate.command_capabilities)
                self.assertEqual(selected[1], generation + 1)
                cursor.execute(
                    f'SELECT details FROM "{storage.AUDIT_TABLE}" '
                    'WHERE guild_id = %s AND event_number = %s',
                    (guild_id, activation.event_number),
                )
                details = cursor.fetchone()[0]
                if isinstance(details, str):
                    details = json.loads(details)
                self.assertEqual(details['command_plan_digest'], plan_digest)
                self.assertEqual(
                    details['changed_paths'],
                    ['command_capabilities'],
                )
        finally:
            connection.rollback()
            connection.close()


if __name__ == '__main__':
    unittest.main()
