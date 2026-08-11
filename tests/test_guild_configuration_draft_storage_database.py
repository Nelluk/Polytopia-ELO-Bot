"""Explicitly gated transactional P10.6b1 development PostgreSQL verification."""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import document_digest
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_6B1_DRAFT_INTEGRATION'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.6b1 database verification',
)
class GuildConfigurationDraftStorageDatabaseTests(unittest.TestCase):
    def test_draft_lifecycle_is_validated_then_fully_rolled_back(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
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
                readonly=False, autocommit=False,
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
                        cursor, guild_id, for_update=True
                    )
                )
                self.assertEqual(digest, document_digest(document))
                created = drafts.put_draft(
                    cursor, guild_id=guild_id, base_revision=revision,
                    base_generation=generation, document=document,
                    actor='integration:p10.6b1',
                )
                selected = drafts.select_draft(
                    cursor, guild_id, active_only=True, for_update=True
                )
                self.assertEqual(selected, created)
                updated = drafts.replace_draft(
                    cursor, guild_id=guild_id,
                    expected_version=created.draft_version,
                    expected_digest=created.document_digest,
                    base_revision=revision, base_generation=generation,
                    document=document, actor='integration:p10.6b1',
                )
                self.assertEqual(updated.draft_version, created.draft_version + 1)
                drafts.expire_draft(
                    cursor, guild_id=guild_id,
                    expected_version=updated.draft_version,
                    expected_digest=updated.document_digest,
                    actor='integration:p10.6b1',
                )
                self.assertIsNone(drafts.select_draft(
                    cursor, guild_id, active_only=True, for_update=False
                ))
        finally:
            connection.rollback()
            connection.close()


if __name__ == '__main__':
    unittest.main()
