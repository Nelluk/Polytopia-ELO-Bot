"""Explicitly gated, fully rolled-back P10.6b3 PostgreSQL rollback proof."""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    document_digest,
    document_to_mapping,
    validate_document,
)
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_6B3_ROLLBACK_INTEGRATION'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.6b3 database verification',
)
class GuildConfigurationRollbackDatabaseTests(unittest.TestCase):
    def test_rollback_clones_prior_revision_then_outer_transaction_rolls_back(self):
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
                original_revision, original_generation, original, original_digest = (
                    drafts.select_active_configuration(
                        cursor,
                        guild_id,
                        for_update=True,
                    )
                )
                mapping = document_to_mapping(original)
                mapping['identity']['display_name'] = (
                    original.identity.display_name + ' rollback integration'
                )
                candidate = validate_document(mapping)
                created = drafts.put_draft(
                    cursor,
                    guild_id=guild_id,
                    base_revision=original_revision,
                    base_generation=original_generation,
                    document=candidate,
                    actor='integration:p10.6b3-setup',
                )
                activation = drafts.activate_draft(
                    cursor,
                    draft=created,
                    active_revision=original_revision,
                    active_generation=original_generation,
                    active_document_digest=original_digest,
                    actor='integration:p10.6b3-setup',
                    changed_paths=('identity.display_name',),
                )
                source_document, source_digest = drafts.select_revision(
                    cursor,
                    guild_id,
                    original_revision,
                )
                rollback = drafts.rollback_to_revision(
                    cursor,
                    guild_id=guild_id,
                    active_revision=activation.revision,
                    active_generation=activation.generation,
                    active_document_digest=activation.document_digest,
                    source_revision=original_revision,
                    source_document=source_document,
                    source_document_digest=source_digest,
                    actor='integration:p10.6b3',
                    changed_paths=('identity.display_name',),
                )
                self.assertEqual(rollback.source_revision, original_revision)
                self.assertGreater(rollback.revision, activation.revision)
                self.assertEqual(rollback.generation, activation.generation + 1)
                self.assertEqual(rollback.document_digest, original_digest)
                selected = drafts.select_active_configuration(
                    cursor,
                    guild_id,
                    for_update=False,
                )
                self.assertEqual(
                    (selected[0], selected[1], selected[3]),
                    (rollback.revision, rollback.generation, original_digest),
                )
                cursor.execute(
                    f'SELECT source_kind, parent_revision FROM '
                    f'"{storage.REVISION_TABLE}" '
                    'WHERE guild_id = %s AND revision_number = %s',
                    (guild_id, rollback.revision),
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (drafts.ROLLBACK_SOURCE_KIND, activation.revision),
                )
                cursor.execute(
                    f'SELECT event_type, revision_number, generation, details '
                    f'FROM "{storage.AUDIT_TABLE}" '
                    'WHERE guild_id = %s AND event_number = %s',
                    (guild_id, rollback.event_number),
                )
                event_type, revision_number, generation, details = cursor.fetchone()
                self.assertEqual(event_type, drafts.ROLLBACK_EVENT_TYPE)
                self.assertEqual(revision_number, rollback.revision)
                self.assertEqual(generation, rollback.generation)
                self.assertEqual(details['source_revision'], original_revision)
                self.assertEqual(details['previous_revision'], activation.revision)
                self.assertEqual(details['changed_paths'], ['identity.display_name'])
        finally:
            connection.rollback()
            connection.close()


if __name__ == '__main__':
    unittest.main()
