"""Gated transactional P10.9 development PostgreSQL verification."""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from modules import guild_configuration_delegation_storage as delegation
from modules import guild_configuration_storage as storage
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_9_DELEGATION_INTEGRATION'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.9 database verification',
)
class GuildConfigurationDelegationDatabaseTests(unittest.TestCase):
    def test_policy_and_audit_are_verified_then_fully_rolled_back(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT, environ=os.environ, create_directories=False,
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
                delegation._validate_live_connection(cursor, target)
                self.assertTrue(delegation.validate_delegation_schema(
                    delegation.inspect_delegation_schema(cursor)
                ))
                guild_id = profile.allowed_guild_ids[0]
                before = delegation.select_delegation(
                    cursor, guild_id, for_update=True,
                )
                manager_role_id = guild_id + 1
                if (
                        before is not None
                        and before.manager_role_ids == (manager_role_id,)
                        and before.allow_activation
                ):
                    manager_role_id += 1
                applied = delegation.put_delegation(
                    cursor,
                    guild_id=guild_id,
                    expected_version=(
                        None if before is None else before.policy_version
                    ),
                    manager_role_ids=(manager_role_id,),
                    allow_activation=True,
                    actor='integration:p10.9',
                )
                self.assertEqual(
                    delegation.select_delegation(cursor, guild_id), applied
                )
                cursor.execute(
                    f'SELECT event_type, details FROM "{storage.AUDIT_TABLE}" '
                    'WHERE guild_id = %s ORDER BY event_number DESC LIMIT 1',
                    (guild_id,),
                )
                event_type, details = cursor.fetchone()
                self.assertEqual(event_type, delegation.EVENT_TYPE)
                self.assertEqual(details['policy_version'], applied.policy_version)
        finally:
            connection.rollback()
            connection.close()


if __name__ == '__main__':
    unittest.main()
