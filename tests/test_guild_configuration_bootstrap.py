"""Focused offline coverage for first-guild bootstrap."""

from __future__ import annotations

import copy
import dataclasses
import unittest
from unittest import mock

from modules import guild_configuration_bootstrap as bootstrap
from modules import guild_configuration_storage as storage
from tests import test_guild_configuration_storage as fixtures


GUILD_ID = storage.DEVELOPMENT_BETA_GUILD_ID


def plan():
    value = fixtures.snapshot()
    value['guilds'][0]['guild_name'] = 'Fresh Development Guild'
    return bootstrap.build_first_guild_plan(
        target=fixtures.target(),
        allowed_guild_ids=(GUILD_ID,),
        discord_snapshot=value,
    )


class FirstGuildPlanTests(unittest.TestCase):
    def test_plan_is_deterministic_safe_standard_and_model_free(self):
        first = plan()
        second = plan()

        self.assertEqual(first, second)
        self.assertEqual(first.guild_id, GUILD_ID)
        self.assertEqual(first.guild_name, 'Fresh Development Guild')
        self.assertEqual(
            first.document.command_capabilities,
            ('core_user', 'guild_admin', 'operator', 'squad'),
        )
        self.assertEqual(first.document.identity.command_prefix, '$')
        self.assertEqual(
            first.document.permissions.user_role_ids_level_2,
            (GUILD_ID,),
        )
        self.assertEqual(first.document.permissions.user_role_ids_level_1, ())
        self.assertEqual(first.document.permissions.user_role_ids_level_3, ())
        self.assertFalse(first.document.teams.allow_teams)
        self.assertFalse(first.document.teams.require_teams)
        self.assertEqual(first.document.teams.max_team_size, 2)
        self.assertFalse(
            first.document.visibility.include_in_global_leaderboard
        )
        self.assertIsNone(first.document.channels.bot_channel_ids)
        self.assertRegex(first.plan_digest, r'^[0-9a-f]{64}$')
        self.assertEqual(
            first.confirmation,
            f'BOOTSTRAP FIRST GUILD {GUILD_ID} {first.plan_digest}',
        )
        rendered = bootstrap.plan_to_mapping(first)
        self.assertFalse(rendered['application_commands_synchronized'])
        self.assertEqual(
            rendered['command_capabilities'],
            ['core_user', 'guild_admin', 'operator', 'squad'],
        )

    def test_plan_requires_one_exact_discord_observed_guild(self):
        snapshot = fixtures.snapshot()
        for allowed, mutation in (
            ((GUILD_ID, 999), None),
            ((999,), None),
            ((GUILD_ID,), lambda value: value.__setitem__('application_id', 1)),
        ):
            value = copy.deepcopy(snapshot)
            if mutation is not None:
                mutation(value)
            with self.subTest(allowed=allowed), self.assertRaises(
                bootstrap.FirstGuildBootstrapError
            ):
                bootstrap.build_first_guild_plan(
                    target=fixtures.target(),
                    allowed_guild_ids=allowed,
                    discord_snapshot=value,
                )

    def test_plan_accepts_independent_development_target(self):
        target = fixtures.target(
            database_name='independent_dev',
            database_user='independent_bot',
            expected_application_id=900000000000000999,
        )
        snapshot = fixtures.snapshot()
        snapshot['application_id'] = target.expected_application_id
        value = bootstrap.build_first_guild_plan(
            target=target,
            allowed_guild_ids=(GUILD_ID,),
            discord_snapshot=snapshot,
        )
        self.assertEqual(value.guild_id, GUILD_ID)


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commit = mock.Mock()
        self.rollback = mock.Mock()

    def cursor(self):
        context = mock.MagicMock()
        context.__enter__.return_value = self._cursor
        return context


class FirstGuildApplyTests(unittest.TestCase):
    def _connection(self):
        cursor = mock.MagicMock()
        cursor.fetchone.side_effect = [
            ('polytopia_dev', 'polybot_dev'),
            ('off',),
        ]
        return _Connection(cursor), cursor

    def test_wrong_confirmation_is_connection_free(self):
        connection, _ = self._connection()
        with self.assertRaisesRegex(
            bootstrap.FirstGuildBootstrapError,
            'exact confirmation',
        ):
            bootstrap.apply_first_guild_bootstrap(
                connection,
                target=fixtures.target(),
                plan=plan(),
                confirmation='wrong',
            )
        connection.commit.assert_not_called()
        connection.rollback.assert_not_called()

    def test_forged_schema_evidence_is_connection_free(self):
        connection, _ = self._connection()
        original = plan()
        forged = dataclasses.replace(
            original,
            base_schema_digest='f' * 64,
            plan_digest=bootstrap._canonical_digest({
                'schema_version': original.schema_version,
                'guild_id': original.guild_id,
                'document_digest': original.document_digest,
                'source_digest': original.source_digest,
                'base_schema_digest': 'f' * 64,
                'draft_schema_digest': original.draft_schema_digest,
                'delegation_schema_digest': original.delegation_schema_digest,
            }),
        )
        with self.assertRaisesRegex(
            bootstrap.FirstGuildBootstrapError,
            'current target and schema contract',
        ):
            bootstrap.apply_first_guild_bootstrap(
                connection,
                target=fixtures.target(),
                plan=forged,
                confirmation=forged.confirmation,
            )
        connection.commit.assert_not_called()
        connection.rollback.assert_not_called()

    def test_apply_commits_one_atomic_verified_graph(self):
        connection, cursor = self._connection()
        value = plan()
        with (
            mock.patch.object(
                bootstrap, '_validate_application_schema_is_empty'
            ) as application_empty,
            mock.patch.object(
                bootstrap,
                '_prepare_configuration_schemas',
                return_value=(True, True, True),
            ) as prepare,
            mock.patch.object(bootstrap, '_insert_first_guild') as insert,
            mock.patch.object(bootstrap, '_verify_first_guild') as verify,
        ):
            result = bootstrap.apply_first_guild_bootstrap(
                connection,
                target=fixtures.target(),
                plan=value,
                confirmation=value.confirmation,
            )

        application_empty.assert_called_once_with(cursor)
        prepare.assert_called_once_with(cursor, target=fixtures.target())
        insert.assert_called_once_with(cursor, value)
        verify.assert_called_once_with(cursor, value)
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        self.assertEqual(result.guild_id, GUILD_ID)
        self.assertTrue(result.base_schema_created)
        self.assertFalse(result.application_commands_synchronized)
        self.assertIn(
            ('SELECT pg_advisory_xact_lock(%s)',
             (bootstrap.BOOTSTRAP_ADVISORY_LOCK_KEY,)),
            [call.args for call in cursor.execute.call_args_list],
        )

    def test_apply_rolls_back_schema_and_guild_on_failure(self):
        connection, _ = self._connection()
        value = plan()
        with (
            mock.patch.object(
                bootstrap, '_validate_application_schema_is_empty'
            ),
            mock.patch.object(
                bootstrap,
                '_prepare_configuration_schemas',
                return_value=(True, True, True),
            ),
            mock.patch.object(
                bootstrap,
                '_insert_first_guild',
                side_effect=RuntimeError('insert failed'),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, 'insert failed'):
                bootstrap.apply_first_guild_bootstrap(
                    connection,
                    target=fixtures.target(),
                    plan=value,
                    confirmation=value.confirmation,
                )
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()

    def test_application_freshness_rejects_any_existing_row(self):
        cursor = mock.MagicMock()
        cursor.fetchall.return_value = [(name,) for name in bootstrap.REQUIRED_TABLES]
        cursor.fetchone.side_effect = [(True,), *((False,) for _ in bootstrap.REQUIRED_TABLES)]
        bootstrap._validate_application_schema_is_empty(cursor)

        cursor = mock.MagicMock()
        cursor.fetchall.return_value = [(name,) for name in bootstrap.REQUIRED_TABLES]
        cursor.fetchone.side_effect = [
            (True,),
            (True,),
        ]
        with self.assertRaisesRegex(
            bootstrap.FirstGuildBootstrapError,
            'already contains data',
        ):
            bootstrap._validate_application_schema_is_empty(cursor)


if __name__ == '__main__':
    unittest.main()
