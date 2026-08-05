"""Strictly gated real-schema coverage for P4.3 commit/rollback semantics."""

import os
import unittest
from unittest import mock
import uuid


INTEGRATION_FLAG = 'POLYBOT_RUN_DB_INTEGRATION'
RUN_DATABASE_INTEGRATION = os.environ.get(INTEGRATION_FLAG) == '1'
BETA_GUILD_ID = 478571892832206869


@unittest.skipUnless(
    RUN_DATABASE_INTEGRATION,
    f'set {INTEGRATION_FLAG}=1 to run development-database integration tests',
)
class GamePingDevelopmentSchemaTests(unittest.TestCase):
    """Use only the exact development profile and rollback-isolated data."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get('POLYBOT_ENV') != 'development':
            raise RuntimeError(
                f'{INTEGRATION_FLAG}=1 requires POLYBOT_ENV=development'
            )

        import psycopg2
        from runtime_config import get_runtime_profile

        cls.profile = get_runtime_profile()
        if (
            cls.profile.environment != 'development'
            or cls.profile.database_name != 'polytopia_dev'
            or cls.profile.database_user != 'polybot_dev'
        ):
            raise RuntimeError(
                'P4.3 integration requires the polytopia_dev database and '
                'polybot_dev role.'
            )
        if cls.profile.background_tasks_enabled or cls.profile.api_enabled:
            raise RuntimeError(
                'P4.3 integration requires background tasks and the API disabled.'
            )

        connection = psycopg2.connect(
            dbname=cls.profile.database_name,
            user=cls.profile.database_user,
            password=cls.profile.database_password,
            host=cls.profile.database_host,
            port=cls.profile.database_port,
        )
        try:
            connection.set_session(readonly=True, autocommit=True)
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_database(), current_user')
                identity = cursor.fetchone()
        finally:
            connection.close()
        if identity != ('polytopia_dev', 'polybot_dev'):
            raise RuntimeError('The integration session identity is unsafe.')

        import settings
        from modules import game_ping_workers, models

        cls.workers = game_ping_workers
        cls.models = models
        cls.settings = settings
        cls.models.db.connect(reuse_if_open=True)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'models') and not cls.models.db.is_closed():
            cls.models.db.close()

    def setUp(self):
        self.models.db.connect(reuse_if_open=True)
        database, role = self.models.db.execute_sql(
            'SELECT current_database(), current_user'
        ).fetchone()
        self.assertEqual((database, role), ('polytopia_dev', 'polybot_dev'))

    def _request_for_real_incomplete_game(self, nonce):
        lineup = (
            self.models.Lineup
            .select(self.models.Lineup, self.models.Player, self.models.DiscordMember)
            .join(self.models.Player)
            .join(self.models.DiscordMember)
            .join_from(self.models.Lineup, self.models.Game)
            .where(
                (self.models.Game.guild_id == BETA_GUILD_ID)
                & (self.models.Game.is_confirmed == 0)
                & (self.models.Player.guild_id == BETA_GUILD_ID)
            )
            .order_by(self.models.Game.id)
            .first()
        )
        if lineup is None:
            self.skipTest('No registered incomplete development game fixture exists.')
        player_id = int(lineup.player.discord_member.discord_id)
        bot_channels = tuple(
            self.settings.guild_setting(BETA_GUILD_ID, 'bot_channels') or ()
        )
        if not bot_channels:
            self.skipTest('The development guild has no configured bot channel.')
        requester = self.workers.MemberSnapshot(
            guild_id=BETA_GUILD_ID,
            discord_id=player_id,
            display_name='P4.3 integration actor',
            name='p4-3-integration-actor',
            role_ids=(),
            role_names=(),
            level=7,
            is_staff=True,
            is_mod=True,
            description=f'P4.3 integration actor (`{player_id}`)',
        )
        facts = self.workers.ChannelFacts(
            guild_id=BETA_GUILD_ID,
            channel_id=int(bot_channels[0]),
            bot_channel_ids=(int(bot_channels[0]),),
            private_bot_channel_ids=(),
            participant_permissions=(),
        )
        return self.workers.GamePingCommitRequest(
            guild_id=BETA_GUILD_ID,
            requester=requester,
            target_id=player_id,
            target_description=requester.description,
            scope='single',
            game_ids=(int(lineup.game_id),),
            channel_facts=facts,
            text=nonce,
            attachments=(),
            invoked_with='/game ping',
        )

    def test_commit_and_forced_audit_failure_roll_back_on_real_schema(self):
        nonce = f'p4-3-schema-{uuid.uuid4().hex}'
        request = self._request_for_real_incomplete_game(nonce)
        original_write = self.models.GameLog.write

        def write_then_fail(*args, **kwargs):
            original_write(*args, **kwargs)
            raise RuntimeError('forced P4.3 audit failure')

        with mock.patch.object(
            self.models.GameLog,
            'write',
            side_effect=write_then_fail,
        ):
            with self.assertRaises(RuntimeError):
                self.workers.commit_notification(request)

        self.assertEqual(
            self.models.GameLog.select()
            .where(self.models.GameLog.message.contains(nonce))
            .count(),
            0,
        )

        try:
            result = self.workers.commit_notification(request)
            self.assertEqual(result.game_ids, request.game_ids)
            self.assertEqual(
                self.models.GameLog.select()
                .where(self.models.GameLog.message.contains(nonce))
                .count(),
                1,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                (
                    self.models.GameLog.delete()
                    .where(self.models.GameLog.message.contains(nonce))
                    .execute()
                )


if __name__ == '__main__':
    unittest.main()
