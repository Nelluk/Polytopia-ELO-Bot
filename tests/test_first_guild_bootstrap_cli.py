"""Focused CLI coverage for the installation-neutral first-guild bootstrap."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import bootstrap_first_guild_configuration as command
from tests import test_guild_configuration_storage as fixtures


APPLICATION_ID = 900000000000000999
GUILD_ID = fixtures.GUILD_ID


def profile():
    return SimpleNamespace(
        environment='development',
        database_name='independent_dev',
        database_user='independent_bot',
        database_password='private',
        database_host='database',
        database_port=5432,
        expected_bot_id=APPLICATION_ID,
        allowed_guild_ids=(GUILD_ID,),
        background_tasks_enabled=False,
        api_enabled=False,
        bullet_enabled=False,
    )


def snapshot():
    value = fixtures.snapshot()
    value['application_id'] = APPLICATION_ID
    return value


class FirstGuildBootstrapCliTests(unittest.TestCase):
    def test_snapshot_uses_environment_default_and_unpacks_owner_inventory(self):
        output_path = (
            command.PROJECT_ROOT
            / 'logs/development/guild-configuration/discord-snapshot.json'
        )
        capture = mock.AsyncMock(return_value=(snapshot(), {'owners': []}))
        with mock.patch.object(
            command,
            '_profile',
            return_value=profile(),
        ), mock.patch.object(
            command.snapshots,
            '_capture_snapshot',
            capture,
        ), mock.patch.object(
            command.snapshots,
            '_write_snapshot',
            return_value=output_path,
        ) as write_snapshot:
            result = command.main([
                'snapshot', '--guild-id', str(GUILD_ID),
            ])

        self.assertEqual(result, 0)
        written_value = write_snapshot.call_args.args[1]
        self.assertEqual(written_value['application_id'], APPLICATION_ID)
        self.assertNotIsInstance(written_value, tuple)
        self.assertEqual(
            write_snapshot.call_args.kwargs['environment'],
            'development',
        )

    def test_plan_loads_snapshot_under_selected_environment(self):
        target = command._target(profile())
        with mock.patch.object(
            command.snapshots,
            '_load_snapshot',
            return_value=snapshot(),
        ) as load_snapshot:
            value = command._plan(profile(), target, 'private.json')

        self.assertEqual(value.guild_id, GUILD_ID)
        load_snapshot.assert_called_once_with(
            'private.json', environment='development'
        )


if __name__ == '__main__':
    unittest.main()
