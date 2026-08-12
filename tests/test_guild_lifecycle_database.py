"""Explicitly gated, rolled-back P10.8 lifecycle transition proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_storage as storage
from modules import operator_guild_lifecycle_workers as workers
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_8_GUILD_LIFECYCLE_INTEGRATION'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.8 database verification',
)
class GuildLifecycleDatabaseTests(unittest.TestCase):
    def test_suspension_state_audit_and_history_preservation_roll_back_together(self):
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
                cursor.execute(
                    'SELECT pg_advisory_xact_lock(%s)',
                    (drafts.DRAFT_ADVISORY_LOCK_KEY,),
                )
                guild_id = profile.allowed_guild_ids[0]
                revision, generation, document, digest = (
                    drafts.select_active_configuration(
                        cursor,
                        guild_id,
                        for_update=True,
                    )
                )
                cursor.execute(
                    f'SELECT COUNT(*) FROM "{storage.REVISION_TABLE}" '
                    'WHERE guild_id = %s',
                    (guild_id,),
                )
                revision_count = cursor.fetchone()[0]
                cursor.execute(
                    f'SELECT COUNT(*) FROM "{drafts.DRAFT_TABLE}" '
                    'WHERE guild_id = %s',
                    (guild_id,),
                )
                draft_count = cursor.fetchone()[0]
                preview = workers.GuildLifecyclePreview(
                    action=workers.SUSPEND,
                    guild_id=guild_id,
                    guild_name=document.identity.display_name,
                    current_state=workers.ACTIVE,
                    desired_state=workers.SUSPENDED,
                    revision=revision,
                    generation=generation,
                    desired_generation=generation + 1,
                    document_digest=digest,
                    command_capabilities=document.command_capabilities,
                    write_required=True,
                    document=document,
                )
                request = workers.GuildLifecycleRequest(
                    operation=workers.COMMIT,
                    action=workers.SUSPEND,
                    requester_id=int(workers.settings.owner_id),
                    invoking_guild_id=guild_id + 1,
                    target_guild_id=guild_id,
                    target_guild_name=document.identity.display_name,
                    current_runtime=(),
                    target=target,
                    database_password=profile.database_password,
                    expected_state=workers.ACTIVE,
                    expected_revision=revision,
                    expected_generation=generation,
                    expected_document_digest=digest,
                    command_plan_digest='c' * 64,
                    confirmation_text=(
                        f'SUSPEND GUILD {guild_id} {digest} {"c" * 64}'
                    ),
                )
                transition = workers._transition(cursor, request, preview)
                self.assertEqual(transition.generation, generation + 1)
                cursor.execute(
                    f'SELECT enrollment_state, active_revision, generation '
                    f'FROM "{storage.REGISTRY_TABLE}" WHERE guild_id = %s',
                    (guild_id,),
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (workers.SUSPENDED, revision, generation + 1),
                )
                cursor.execute(
                    f'SELECT event_type, revision_number, generation, details '
                    f'FROM "{storage.AUDIT_TABLE}" WHERE guild_id = %s '
                    'AND event_number = %s',
                    (guild_id, transition.event_number),
                )
                event_type, event_revision, event_generation, details = cursor.fetchone()
                if isinstance(details, str):
                    details = json.loads(details)
                self.assertEqual(event_type, 'suspension')
                self.assertEqual(event_revision, revision)
                self.assertEqual(event_generation, generation + 1)
                self.assertEqual(details['command_plan_digest'], 'c' * 64)
                cursor.execute(
                    f'SELECT COUNT(*) FROM "{storage.REVISION_TABLE}" '
                    'WHERE guild_id = %s',
                    (guild_id,),
                )
                self.assertEqual(cursor.fetchone()[0], revision_count)
                cursor.execute(
                    f'SELECT COUNT(*) FROM "{drafts.DRAFT_TABLE}" '
                    'WHERE guild_id = %s',
                    (guild_id,),
                )
                self.assertEqual(cursor.fetchone()[0], draft_count)
        finally:
            connection.rollback()
            connection.close()


if __name__ == '__main__':
    unittest.main()
