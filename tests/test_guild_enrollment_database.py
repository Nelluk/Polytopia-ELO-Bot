"""Explicitly gated, fully rolled-back P10.7 PostgreSQL enrollment proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

from modules import guild_configuration_storage as storage
from modules import operator_guild_enrollment_workers as workers
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_7_ENROLLMENT_INTEGRATION'
SNAPSHOT_ENV = 'POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.7 database verification',
)
class GuildEnrollmentDatabaseTests(unittest.TestCase):
    def test_first_revision_and_audit_are_verified_then_outer_transaction_rolls_back(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        self.assertEqual(profile.guild_configuration_source, 'database')
        snapshot_path = Path(os.environ.get(SNAPSHOT_ENV, ''))
        self.assertTrue(snapshot_path.is_absolute() and snapshot_path.is_file())
        discord_snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))

        import psycopg2

        connection = psycopg2.connect(
            dbname=profile.database_name,
            user=profile.database_user,
            password=profile.database_password,
            host=profile.database_host,
            port=profile.database_port,
        )
        target_guild_id = 8_900_000_000_000_000 + uuid.uuid4().int % 1_000_000
        try:
            connection.set_session(
                readonly=False,
                autocommit=False,
                isolation_level='REPEATABLE READ',
            )
            with connection.cursor() as cursor:
                storage.validate_live_identity(
                    storage.StorageTarget(
                        environment=profile.environment,
                        database_name=profile.database_name,
                        database_user=profile.database_user,
                        expected_application_id=profile.expected_bot_id,
                        background_tasks_enabled=profile.background_tasks_enabled,
                        api_enabled=profile.api_enabled,
                        bullet_enabled=profile.bullet_enabled,
                    ),
                    actual_database=profile.database_name,
                    actual_user=profile.database_user,
                )
                cursor.execute(
                    f'SELECT registry.guild_id, registry.active_revision, '
                    f'registry.generation, revision.document_digest FROM '
                    f'"{storage.REGISTRY_TABLE}" AS registry JOIN '
                    f'"{storage.REVISION_TABLE}" AS revision ON '
                    'revision.guild_id = registry.guild_id AND '
                    'revision.revision_number = registry.active_revision '
                    "WHERE registry.enrollment_state = 'active' "
                    'ORDER BY registry.guild_id'
                )
                rows = tuple(cursor.fetchall())
                self.assertTrue(rows)
                current_records = tuple(SimpleNamespace(
                    guild_id=row[0], revision=row[1], generation=row[2],
                    document_digest=row[3],
                ) for row in rows)
                current_ids = tuple(row[0] for row in rows)
                self.assertEqual(
                    tuple(sorted(value['guild_id'] for value in discord_snapshot['guilds'])),
                    current_ids,
                )
                discord_snapshot['guilds'].append({
                    'guild_id': target_guild_id,
                    'guild_name': 'P10.7 rolled-back enrollment',
                    'roles': [{
                        'id': target_guild_id,
                        'name': '@everyone',
                        'managed': False,
                        'is_default': True,
                    }],
                    'channels': [],
                })
                request = workers.request_from_profile(
                    profile=profile,
                    requester_id=int(workers.settings.owner_id),
                    invoking_guild_id=current_ids[0],
                    target_guild_id=target_guild_id,
                    target_guild_name='P10.7 rolled-back enrollment',
                    template=workers.BASIC_PREFIX_TEMPLATE,
                    guild_type='standard',
                    include_in_global_leaderboard=None,
                    bot_permissions=tuple(sorted(workers.REQUIRED_BOT_PERMISSIONS)),
                    current_runtime_records=current_records,
                    forbidden_guild_ids=(),
                    discord_snapshot=discord_snapshot,
                )
                preview = workers._preview(request)
                workers._validate_current_runtime(cursor, request)
                workers._target_absent(cursor, target_guild_id)
                enrollment = workers._insert_enrollment(cursor, request, preview)
                self.assertEqual(
                    (enrollment.revision, enrollment.generation, enrollment.event_number),
                    (1, 1, 1),
                )
                cursor.execute(
                    f'SELECT enrollment_state, active_revision, generation FROM '
                    f'"{storage.REGISTRY_TABLE}" WHERE guild_id = %s',
                    (target_guild_id,),
                )
                self.assertEqual(cursor.fetchone(), ('active', 1, 1))
                cursor.execute(
                    f'SELECT source_kind, document_digest FROM '
                    f'"{storage.REVISION_TABLE}" WHERE guild_id = %s',
                    (target_guild_id,),
                )
                self.assertEqual(cursor.fetchone(), (
                    'owner_activation', enrollment.document_digest,
                ))
                cursor.execute(
                    f'SELECT event_type, revision_number, generation, '
                    f'document_digest FROM "{storage.AUDIT_TABLE}" '
                    'WHERE guild_id = %s',
                    (target_guild_id,),
                )
                self.assertEqual(cursor.fetchone(), (
                    workers.ENROLLMENT_EVENT_TYPE, 1, 1,
                    enrollment.document_digest,
                ))
        finally:
            connection.rollback()
            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT 1 FROM "{storage.REGISTRY_TABLE}" WHERE guild_id = %s',
                    (target_guild_id,),
                )
                self.assertIsNone(cursor.fetchone())
            connection.close()


if __name__ == '__main__':
    unittest.main()
