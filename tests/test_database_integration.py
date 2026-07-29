"""Explicitly gated integration tests for the development PostgreSQL database."""

import asyncio
from contextlib import contextmanager, nullcontext
import datetime
from io import BytesIO
import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import warnings


INTEGRATION_FLAG = 'POLYBOT_RUN_DB_INTEGRATION'
RUN_DATABASE_INTEGRATION = os.environ.get(INTEGRATION_FLAG) == '1'

# discord.py 2.7.1 still imports Python 3.12's deprecated stdlib audioop
# module. Keep the integration gate strict except for this exact upstream
# warning, matching the default compatibility suite.
warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)


@unittest.skipUnless(
    RUN_DATABASE_INTEGRATION,
    f'set {INTEGRATION_FLAG}=1 to run development-database integration tests',
)
class DevelopmentDatabaseIntegrationTests(unittest.TestCase):
    """Exercise production-shaped code only after a strict database preflight."""

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
                'Integration tests require the polytopia_dev database and '
                'polybot_dev role.'
            )
        if cls.profile.background_tasks_enabled or cls.profile.api_enabled:
            raise RuntimeError(
                'Integration tests require background tasks and the API to '
                'remain disabled.'
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
                database_name, database_user = cursor.fetchone()
        finally:
            connection.close()

        if (
            database_name != cls.profile.database_name
            or database_user != cls.profile.database_user
        ):
            raise RuntimeError(
                'PostgreSQL session identity does not match the development '
                'runtime profile.'
            )

        import settings
        from modules import models

        cls.settings = settings
        cls.models = models
        cls.models.db.connect(reuse_if_open=True)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'models') and not cls.models.db.is_closed():
            cls.models.db.close()

    def setUp(self):
        self.models.db.connect(reuse_if_open=True)
        database_name, database_user = self.models.db.execute_sql(
            'SELECT current_database(), current_user'
        ).fetchone()
        self.assertEqual(database_name, 'polytopia_dev')
        self.assertEqual(database_user, 'polybot_dev')

    @contextmanager
    def rollback_scope(self):
        with self.models.db.atomic() as transaction:
            try:
                yield
            finally:
                transaction.rollback()

    def test_model_import_initialized_expected_schema(self):
        expected_tables = {
            'apiapplication',
            'configuration',
            'discordmember',
            'game',
            'gameside',
            'lineup',
            'player',
            'team',
            'tribe',
        }
        rows = self.models.db.execute_sql(
            'SELECT table_name FROM information_schema.tables '
            "WHERE table_schema = 'public'"
        ).fetchall()
        actual_tables = {row[0] for row in rows}
        self.assertTrue(expected_tables.issubset(actual_tables))

    def test_representative_write_is_rolled_back(self):
        marker = f'phase6-rollback-{uuid.uuid4()}'
        with self.rollback_scope():
            record = self.models.GameLog.create(
                guild_id=self.profile.allowed_guild_ids[0],
                message=marker,
            )
            self.assertGreater(record.id, 0)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message == marker
                ).count(),
                1,
            )

        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message == marker
            ).count(),
            0,
        )

    def test_newgame_worker_creates_complete_graph_and_rolls_back(self):
        from modules import game_workers

        marker = f'P2.1 integration {uuid.uuid4().hex}'
        id_base = 800_000_000_000_000_000
        host_id = id_base + (uuid.uuid4().int % 10_000_000)
        opponent_id = id_base + (uuid.uuid4().int % 10_000_000)
        request = game_workers.NewGameRequest(
            guild_id=self.profile.allowed_guild_ids[0],
            name=marker,
            is_ranked=False,
            is_mobile=True,
            mod_override=False,
            requester_id=host_id,
            requester_name='p21-host',
            requester_nick=None,
            requester_description=f'**p21-host** (`{host_id}`)',
            invoked_with='newgameunranked',
            escaped_game_name=marker,
            sides=(
                (
                    game_workers.NewGameParticipant(
                        discord_id=host_id,
                        discord_name='p21-host',
                        discord_nick=None,
                        display_name='P2.1 Host',
                        role_names=(),
                    ),
                ),
                (
                    game_workers.NewGameParticipant(
                        discord_id=opponent_id,
                        discord_name='p21-opponent',
                        discord_nick=None,
                        display_name='P2.1 Opponent',
                        role_names=(),
                    ),
                ),
            ),
        )

        with self.rollback_scope():
            # The gated test already owns the connection/outer rollback.
            # Offline tests independently prove worker connection ownership.
            with mock.patch.object(
                self.models.db,
                'connection_context',
                return_value=nullcontext(),
            ):
                result = game_workers.create_new_game(request)

            game = self.models.Game.get_by_id(result.game_id)
            self.assertEqual(game.name, marker.title()[:35])
            self.assertEqual(game.host.discord_member.discord_id, host_id)
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game == game
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

        self.assertEqual(
            self.models.Game.get_or_none(
                self.models.Game.id == result.game_id
            ),
            None,
        )
        self.assertEqual(
            self.models.DiscordMember.select().where(
                self.models.DiscordMember.discord_id.in_(
                    (host_id, opponent_id)
                )
            ).count(),
            0,
        )
        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count(),
            0,
        )

    def test_newgame_executor_failure_rolls_back_worker_connection(self):
        from modules import exceptions, game_workers

        marker = f'P2.1 worker rollback {uuid.uuid4().hex}'
        id_base = 810_000_000_000_000_000
        participant_one_id = id_base + (uuid.uuid4().int % 10_000_000)
        participant_two_id = id_base + (uuid.uuid4().int % 10_000_000)
        missing_requester_id = id_base + (uuid.uuid4().int % 10_000_000)
        request = game_workers.NewGameRequest(
            guild_id=self.profile.allowed_guild_ids[0],
            name=marker,
            is_ranked=False,
            is_mobile=True,
            mod_override=False,
            requester_id=missing_requester_id,
            requester_name='missing-host',
            requester_nick=None,
            requester_description=(
                f'**missing-host** (`{missing_requester_id}`)'
            ),
            invoked_with='newgameunranked',
            escaped_game_name=marker,
            sides=(
                (
                    game_workers.NewGameParticipant(
                        discord_id=participant_one_id,
                        discord_name='p21-participant-one',
                        discord_nick=None,
                        display_name='P2.1 Participant One',
                        role_names=(),
                    ),
                ),
                (
                    game_workers.NewGameParticipant(
                        discord_id=participant_two_id,
                        discord_name='p21-participant-two',
                        discord_nick=None,
                        display_name='P2.1 Participant Two',
                        role_names=(),
                    ),
                ),
            ),
        )

        async def run_worker():
            task = asyncio.create_task(
                game_workers.run_new_game_creation(request)
            )
            while not task.done():
                await asyncio.sleep(0.05)
            return await task

        with self.assertRaisesRegex(
            exceptions.CheckFailedError,
            'registered game host',
        ):
            asyncio.run(run_worker())

        self.models.db.connect(reuse_if_open=True)
        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.name == marker.title()[:35]
            ).count(),
            0,
        )
        self.assertEqual(
            self.models.DiscordMember.select().where(
                self.models.DiscordMember.discord_id.in_(
                    (participant_one_id, participant_two_id)
                )
            ).count(),
            0,
        )
        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count(),
            0,
        )

    def test_bot_extensions_load_with_background_tasks_disabled(self):
        import bot as bot_module

        extension_names = (
            'modules.games',
            'modules.customhelp',
            'modules.matchmaking',
            'modules.administration',
            'modules.misc',
            'modules.league',
            'modules.api_cog',
            'modules.bullet',
            'modules.antiscam',
        )

        async def load_extensions():
            bot_module.configure_runtime_arguments(['--skip_tasks'])
            instance = bot_module.MyBot()
            try:
                for extension_name in extension_names:
                    if extension_name == 'modules.bullet':
                        spreadsheet_config = mock.mock_open(
                            read_data='{"spreadsheet_key": "offline-test"}'
                        )
                        with mock.patch('builtins.open', spreadsheet_config):
                            await instance.load_extension(extension_name)
                    else:
                        await instance.load_extension(extension_name)
                self.assertEqual(
                    set(instance.extensions),
                    set(extension_names),
                )
                self.assertFalse(self.settings.run_tasks)

                matchmaking_cog = instance.get_cog('matchmaking')
                league_cog = instance.get_cog('league')
                self.assertFalse(
                    matchmaking_cog.task_purge_expired_games.is_running()
                )
                self.assertFalse(
                    league_cog.task_send_polychamps_invite.is_running()
                )
                self.assertFalse(
                    league_cog.task_draft_reminders.is_running()
                )
            finally:
                await instance.close()
                self.settings.bot = None

        asyncio.run(load_extensions())

    def test_api_application_and_database_route_request(self):
        from modules import api

        discord_id = 8_800_000_000_000_000 + uuid.uuid4().int % 1_000_000
        with self.rollback_scope():
            self.models.DiscordMember.create(
                discord_id=discord_id,
                name='Phase 6 API User',
                polytopia_name='Offline API User',
            )

            async def offline_scopes():
                return ['users:read', 'games:read']

            api.server.dependency_overrides[api.get_scopes] = offline_scopes

            async def request_user():
                messages = []
                request_sent = False

                async def receive():
                    nonlocal request_sent
                    if request_sent:
                        return {'type': 'http.disconnect'}
                    request_sent = True
                    return {
                        'type': 'http.request',
                        'body': b'',
                        'more_body': False,
                    }

                async def send(message):
                    messages.append(message)

                path = f'/users/{discord_id}'
                await api.server(
                    {
                        'type': 'http',
                        'asgi': {'version': '3.0'},
                        'http_version': '1.1',
                        'method': 'GET',
                        'scheme': 'http',
                        'path': path,
                        'raw_path': path.encode('ascii'),
                        'query_string': b'',
                        'headers': [(b'host', b'offline.test')],
                        'client': ('127.0.0.1', 1),
                        'server': ('offline.test', 80),
                        'root_path': '',
                    },
                    receive,
                    send,
                )
                status = next(
                    message['status'] for message in messages
                    if message['type'] == 'http.response.start'
                )
                body = b''.join(
                    message.get('body', b'') for message in messages
                    if message['type'] == 'http.response.body'
                )
                return status, json.loads(body)

            try:
                status, response = asyncio.run(request_user())
            finally:
                api.server.dependency_overrides.clear()

            self.assertEqual(status, 200)
            self.assertEqual(response['discord_id'], discord_id)
            self.assertEqual(response['mobile_name'], 'Offline API User')
            self.assertEqual(response['games'], {})

    def test_database_backed_draft_graph_embed_and_card_rendering(self):
        import discord
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import pyplot as plt
        import numpy as np
        import pandas as pd
        from PIL import Image
        from scipy import signal

        from modules import image_storage, imgen

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex[:10]
        team_name = f'Phase 6 Team {suffix}'
        base_discord_id = (
            8_700_000_000_000_000 + uuid.uuid4().int % 1_000_000
        )

        with self.rollback_scope():
            team = self.models.Team.create(
                name=team_name,
                guild_id=guild_id,
                emoji='🧪',
                image_url='https://offline.test/team.png',
                league_tier=1,
            )
            first_member = self.models.DiscordMember.create(
                discord_id=base_discord_id,
                name='Phase 6 One',
                polytopia_name='PhaseSixOne',
            )
            second_member = self.models.DiscordMember.create(
                discord_id=base_discord_id + 1,
                name='Phase 6 Two',
                polytopia_name='PhaseSixTwo',
            )
            first_player = self.models.Player.create(
                discord_member=first_member,
                guild_id=guild_id,
                name='Phase 6 One',
                team=team,
            )
            second_player = self.models.Player.create(
                discord_member=second_member,
                guild_id=guild_id,
                name='Phase 6 Two',
                team=team,
            )
            game = self.models.Game.create(
                guild_id=guild_id,
                host=first_player,
                name='Phase 6 Draft',
                notes='transactional integration fixture',
                is_pending=True,
                size=[1, 1],
                expiration=datetime.datetime.now()
                + datetime.timedelta(hours=24),
            )
            first_side = self.models.GameSide.create(
                game=game,
                team=team,
                size=1,
                position=1,
                sidename='Alpha',
            )
            second_side = self.models.GameSide.create(
                game=game,
                size=1,
                position=2,
                sidename='Beta',
            )
            self.models.Lineup.create(
                game=game,
                gameside=first_side,
                player=first_player,
            )
            self.models.Lineup.create(
                game=game,
                gameside=second_side,
                player=second_player,
            )

            draft_order = game.draft_order()
            self.assertEqual(len(draft_order), 2)
            self.assertEqual(
                {pick['player'].id for pick in draft_order},
                {first_player.id, second_player.id},
            )

            embed, content = game.embed(prefix='$')
            self.assertIsInstance(embed, discord.Embed)
            self.assertIn(f'Open Game {game.id}', embed.title)
            self.assertIn('This match is now full', content)
            self.assertIn(f'`$start {game.id} Name of Game`', content)

            history_start = datetime.datetime(2026, 1, 1, 12, 0)
            for day, elo in ((0, 1000), (2, 1025), (4, 1015)):
                history_game = self.models.Game.create(
                    guild_id=guild_id,
                    name=f'History {day} {suffix}',
                    completed_ts=history_start + datetime.timedelta(days=day),
                    is_completed=True,
                    is_confirmed=True,
                    size=[2, 2],
                )
                self.models.GameSide.create(
                    game=history_game,
                    team=team,
                    size=2,
                    position=1,
                    team_elo_after_game=elo,
                )

            history_query = (
                self.models.GameSide
                .select(
                    self.models.Game.completed_ts,
                    self.models.GameSide.team_elo_after_game.alias('elo'),
                )
                .join(self.models.Game)
                .where(
                    (self.models.GameSide.team == team)
                    & self.models.GameSide.team_elo_after_game.is_null(False)
                )
                .order_by(self.models.Game.completed_ts)
            )
            history = pd.DataFrame(history_query.dicts())
            resampled = (
                history
                .set_index('completed_ts')
                .resample('D')
                .mean()
                .interpolate()
                .reset_index()
            )
            smoothed = signal.savgol_filter(
                resampled['elo'].values,
                window_length=3,
                polyorder=2,
            )
            self.assertTrue(np.isfinite(smoothed).all())

            figure, axis = plt.subplots()
            try:
                axis.plot(resampled['completed_ts'], smoothed)
                graph = BytesIO()
                figure.savefig(graph, format='png')
                self.assertTrue(
                    graph.getvalue().startswith(b'\x89PNG\r\n\x1a\n')
                )
            finally:
                plt.close(figure)

            source_images = [
                Image.new('RGBA', (100, 100), '#00ff00'),
                Image.new('RGBA', (100, 100), '#0000ff'),
            ]
            with mock.patch.object(
                    imgen, 'fetch_image', side_effect=source_images):
                promotion = imgen.arrow_card(
                    'PROMOTION',
                    team.name,
                    'left',
                    'right',
                    [('u', '#00ff00')],
                )
            try:
                self.assertGreater(promotion.fp.getbuffer().nbytes, 0)
            finally:
                promotion.close()

            source_images = [
                Image.new('RGBA', (100, 100), '#00ff00'),
                Image.new('RGBA', (100, 100), '#0000ff'),
            ]
            with mock.patch.object(
                    imgen, 'fetch_image', side_effect=source_images):
                demotion = imgen.arrow_card(
                    'DEMOTION',
                    team.name,
                    'left',
                    'right',
                    [('r', '#ff0000'), ('l', '#00ff00')],
                )
            try:
                self.assertGreater(demotion.fp.getbuffer().nbytes, 0)
            finally:
                demotion.close()

            fake_member = SimpleNamespace(
                id=first_member.discord_id,
                name=first_member.name,
                guild=SimpleNamespace(id=guild_id),
                display_avatar=SimpleNamespace(
                    replace=lambda **_kwargs: 'offline-avatar'
                ),
            )
            fake_role = SimpleNamespace(
                name=team.name,
                colour=discord.Colour.blue(),
                color=discord.Colour.blue(),
            )
            source_images = [
                Image.new('RGBA', (100, 100), '#00ff00'),
                Image.new('RGBA', (100, 100), '#0000ff'),
            ]
            with mock.patch.object(
                    image_storage, 'resolve_image',
                    return_value=team.image_url):
                with mock.patch.object(
                        imgen, 'fetch_image', side_effect=source_images):
                    draft_card = imgen.player_draft_card(
                        fake_member,
                        fake_role,
                    )
            try:
                self.assertIn(team.name, draft_card.filename)
                self.assertGreater(draft_card.fp.getbuffer().nbytes, 0)
            finally:
                draft_card.close()

        self.assertEqual(
            self.models.Team.select().where(
                self.models.Team.name == team_name
            ).count(),
            0,
        )


if __name__ == '__main__':
    unittest.main()
