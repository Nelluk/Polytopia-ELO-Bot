import asyncio
import importlib
from importlib.metadata import version
from io import BytesIO
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest import mock
import warnings

# discord.py 2.7.1 still imports Python 3.12's deprecated stdlib audioop
# module. Keep the warnings-as-errors gate strict except for this narrow
# upstream warning while the project remains intentionally pinned to 3.12.
warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)
import discord
from discord.ext import commands
import fastapi
from peewee import (
    BooleanField,
    ForeignKeyField,
    IntegerField,
    Model,
    PostgresqlDatabase,
)
from PIL import Image
import pydantic

from modules import imgen
from scripts import dependency_inventory


class RuntimeDependencyCompatibilityTests(unittest.TestCase):
    def test_expected_runtime_dependencies_import(self):
        module_names = (
            'discord',
            'fastapi',
            'google.auth',
            'google.oauth2.service_account',
            'gspread',
            'gspread_asyncio',
            'gunicorn',
            'httptools',
            'matplotlib',
            'pandas',
            'peewee',
            'PIL',
            'psycopg2',
            'requests',
            'scipy',
            'uvicorn',
            'uvloop',
        )
        for module_name in module_names:
            with self.subTest(module=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))

    def test_dependency_inventory_works_without_pip(self):
        details = dependency_inventory.inventory()

        self.assertEqual(details['python'], sys.version.split()[0])
        self.assertEqual(details['executable'], sys.executable)
        package_names = {name.casefold() for name in details['packages']}
        for package in ('discord.py', 'fastapi', 'Pillow', 'peewee'):
            with self.subTest(package=package):
                self.assertIn(package.casefold(), package_names)

    def test_group_two_dependency_apis_construct_offline(self):
        import gspread
        import gspread_asyncio
        from google.oauth2.service_account import Credentials
        from psycopg2.errors import DuplicateObject

        credential_factory = lambda: mock.sentinel.credentials
        manager = gspread_asyncio.AsyncioGspreadClientManager(
            credential_factory
        )

        self.assertIs(manager.credentials_fn, credential_factory)
        self.assertTrue(
            issubclass(gspread.exceptions.GSpreadException, Exception)
        )
        self.assertTrue(
            callable(Credentials.from_service_account_info)
        )
        self.assertTrue(issubclass(DuplicateObject, Exception))

    def test_group_four_elo_graph_pipeline_renders_offline(self):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
        import pandas as pd
        from scipy import signal

        history = pd.DataFrame(
            {
                'completed_ts': pd.to_datetime(
                    ['2026-01-01', '2026-01-03', '2026-01-05']
                ),
                'elo': [1000.0, 1020.0, 1010.0],
            }
        )
        resampled = (
            history
            .set_index('completed_ts')
            .resample('D')
            .mean()
            .interpolate()
            .reset_index()
        )
        smoothed = signal.savgol_filter(
            resampled['elo'].values, window_length=3, polyorder=2
        )

        self.assertEqual(len(resampled), 5)
        self.assertTrue(np.isfinite(smoothed).all())

        figure, axis = plt.subplots()
        try:
            axis.plot(history['completed_ts'], history['elo'], 'o')
            axis.plot(resampled['completed_ts'], smoothed, '-')
            image = BytesIO()
            figure.savefig(image, format='png')
            self.assertTrue(image.getvalue().startswith(b'\x89PNG\r\n\x1a\n'))
        finally:
            plt.close(figure)

    def test_group_five_discord_and_aiohttp_construct_offline(self):
        import aiohttp

        async def construct_clients():
            client = discord.Client(intents=discord.Intents.default())
            try:
                async with aiohttp.ClientSession() as session:
                    self.assertFalse(session.closed)
                    self.assertGreater(session.timeout.total, 0)
            finally:
                await client.close()

        asyncio.run(construct_clients())

    def test_group_five_has_no_legacy_discord_loop_access(self):
        root = Path(__file__).resolve().parents[1]
        runtime_files = (
            'modules/administration.py',
            'modules/api.py',
            'modules/games.py',
            'modules/league.py',
            'modules/matchmaking.py',
            'modules/misc.py',
        )
        for relative_path in runtime_files:
            with self.subTest(path=relative_path):
                source = (root / relative_path).read_text(encoding='utf-8')
                self.assertNotIn('bot.loop', source)
                self.assertNotIn('Client(loop=', source)

    def test_group_seven_server_stack_configures_offline(self):
        from gunicorn.config import Config as GunicornConfig
        from gunicorn.workers.gasgi import ASGIWorker
        import httptools
        import uvicorn
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
        import uvloop

        self.assertEqual(version('gunicorn'), '26.0.0')
        self.assertEqual(version('httptools'), '0.8.0')
        self.assertEqual(version('uvicorn'), '0.51.0')
        self.assertEqual(version('uvloop'), '0.22.1')

        gunicorn_config = GunicornConfig()
        gunicorn_config.set('worker_class', 'asgi')
        gunicorn_config.set('asgi_loop', 'uvloop')
        gunicorn_config.set('asgi_lifespan', 'on')
        self.assertIs(gunicorn_config.worker_class, ASGIWorker)
        self.assertEqual(gunicorn_config.asgi_loop, 'uvloop')
        self.assertEqual(gunicorn_config.asgi_lifespan, 'on')

        async def offline_app(scope, receive, send):
            pass

        uvicorn_config = uvicorn.Config(
            offline_app,
            host='127.0.0.1',
            port=0,
            loop='uvloop',
            http='httptools',
            lifespan='off',
            interface='asgi3',
        )
        uvicorn_config.load()
        self.assertTrue(uvicorn_config.loaded)
        self.assertIs(uvicorn_config.http_protocol_class, HttpToolsProtocol)

        loop = uvloop.new_event_loop()
        try:
            self.assertEqual(type(loop).__module__, 'uvloop')
        finally:
            loop.close()

        parsed_url = httptools.parse_url(
            b'https://offline.test:8443/path?query=value'
        )
        self.assertEqual(parsed_url.schema, b'https')
        self.assertEqual(parsed_url.host, b'offline.test')
        self.assertEqual(parsed_url.port, 8443)
        self.assertEqual(parsed_url.path, b'/path')
        self.assertEqual(parsed_url.query, b'query=value')

    def test_bot_constructs_without_connecting_to_database_or_discord(self):
        stubs = {}
        for module_name in (
                'logging_config', 'modules.image_storage',
                'modules.initialize_data', 'modules.models',
                'modules.utilities'):
            stubs[module_name] = ModuleType(module_name)
        stubs['modules.initialize_data'].initialize_data = lambda: None
        runtime_profile = SimpleNamespace(
            background_tasks_enabled=False,
            discord_token='offline-test-token',
        )
        settings_stub = ModuleType('settings')
        settings_stub.runtime_profile = runtime_profile
        settings_stub.owner_id = 1
        settings_stub.bot = None
        settings_stub.config = {}
        settings_stub.run_tasks = False
        stubs['settings'] = settings_stub

        old_bot_module = sys.modules.pop('bot', None)
        try:
            with mock.patch.dict(sys.modules, stubs):
                bot_module = importlib.import_module('bot')
                instance = bot_module.MyBot()
                try:
                    self.assertIsInstance(instance, commands.Bot)
                    self.assertTrue(instance.intents.members)
                    self.assertTrue(instance.intents.message_content)
                    self.assertFalse(instance.intents.typing)
                    self.assertFalse(instance.intents.presences)
                finally:
                    asyncio.run(instance.close())
        finally:
            sys.modules.pop('bot', None)
            if old_bot_module is not None:
                sys.modules['bot'] = old_bot_module

    def test_fastapi_routes_and_pydantic_model_construct(self):
        model_stubs = ModuleType('modules.models')
        model_stubs.ApiApplication = type('ApiApplication', (), {})
        model_stubs.DiscordMember = type('DiscordMember', (), {})
        model_stubs.Game = type('Game', (), {})

        runtime_profile = SimpleNamespace(
            api_enabled=True,
            discord_token='offline-test-token',
            environment='development',
        )
        settings_stub = ModuleType('settings')
        settings_stub.runtime_profile = runtime_profile

        old_api_module = sys.modules.pop('modules.api', None)
        try:
            with mock.patch.dict(
                    sys.modules,
                    {'modules.models': model_stubs, 'settings': settings_stub}):
                api = importlib.import_module('modules.api')

            game = api.NewGame(
                game_name='Dependency smoke test',
                guild_id=123,
                sides_discord_ids=[[1], [2]],
            )
            self.assertEqual(game.game_name, 'Dependency smoke test')
            self.assertFalse(game.is_ranked)
            self.assertEqual(
                game.model_dump(),
                {
                    'game_name': 'Dependency smoke test',
                    'guild_id': 123,
                    'is_ranked': False,
                    'is_mobile': True,
                    'notes': '',
                    'sides_discord_ids': [[1], [2]],
                },
            )

            routes = {
                (route.path, method)
                for route in api.server.routes
                for method in getattr(route, 'methods', ())
            }
            self.assertIn(('/users/{discord_id}', 'GET'), routes)
            self.assertIn(('/games/{game_id}', 'GET'), routes)
            self.assertIn(('/game/new', 'POST'), routes)
            self.assertIsInstance(api.server, fastapi.FastAPI)
            self.assertTrue(issubclass(api.NewGame, pydantic.BaseModel))
            api_client = api.create_discord_client()
            self.assertIsInstance(api_client, discord.Client)
            asyncio.run(api_client.close())

            user_record = SimpleNamespace(
                as_json=lambda include_games: {
                    'discord_id': 1,
                    'include_games': include_games,
                }
            )
            game_record = SimpleNamespace(
                as_json=lambda: {'game_id': 2}
            )
            created_game = SimpleNamespace(
                id=3,
                notes='',
                save=mock.Mock(),
            )
            api.DiscordMember.discord_id = mock.sentinel.discord_id_field
            api.DiscordMember.get_or_none = mock.Mock(
                return_value=user_record
            )
            api.Game.id = mock.sentinel.game_id_field
            api.Game.get_or_none = mock.Mock(return_value=game_record)
            api.Game.create_game = mock.Mock(
                return_value=(created_game, [])
            )

            async def offline_scopes():
                return ['users:read', 'games:read', 'games:new']

            api.server.dependency_overrides[api.get_scopes] = offline_scopes

            async def fake_get_discord_member(guild_id, user_id):
                return SimpleNamespace(guild_id=guild_id, id=user_id)

            async def asgi_request(method, path, payload=None):
                body = (
                    json.dumps(payload).encode('utf-8')
                    if payload is not None else b''
                )
                request_sent = False
                messages = []

                async def receive():
                    nonlocal request_sent
                    if request_sent:
                        return {'type': 'http.disconnect'}
                    request_sent = True
                    return {
                        'type': 'http.request',
                        'body': body,
                        'more_body': False,
                    }

                async def send(message):
                    messages.append(message)

                headers = [(b'host', b'offline.test')]
                if payload is not None:
                    headers.append((b'content-type', b'application/json'))
                await api.server(
                    {
                        'type': 'http',
                        'asgi': {'version': '3.0'},
                        'http_version': '1.1',
                        'method': method,
                        'scheme': 'http',
                        'path': path,
                        'raw_path': path.encode('ascii'),
                        'query_string': b'',
                        'headers': headers,
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
                response_body = b''.join(
                    message.get('body', b'') for message in messages
                    if message['type'] == 'http.response.body'
                )
                return status, json.loads(response_body)

            try:
                with mock.patch.object(
                        api, 'get_discord_member',
                        side_effect=fake_get_discord_member):
                    async def exercise_routes():
                        return (
                            await asgi_request('GET', '/users/1'),
                            await asgi_request('GET', '/games/2'),
                            await asgi_request(
                                'POST',
                                '/game/new',
                                {
                                    'game_name': 'Offline route test',
                                    'guild_id': 123,
                                    'notes': 'offline',
                                    'sides_discord_ids': [[1], [2]],
                                },
                            ),
                            await asgi_request(
                                'POST',
                                '/game/new',
                                {
                                    'game_name': 'Missing sides',
                                    'guild_id': 123,
                                },
                            ),
                        )

                    (
                        user_response,
                        game_response,
                        create_response,
                        invalid_response,
                    ) = asyncio.run(exercise_routes())

                self.assertEqual(user_response[0], 200)
                self.assertEqual(
                    user_response[1],
                    {'discord_id': 1, 'include_games': True},
                )
                self.assertEqual(game_response, (200, {'game_id': 2}))
                self.assertEqual(create_response, (200, {'game_id': 3}))
                self.assertEqual(invalid_response[0], 422)
                self.assertEqual(created_game.notes, 'offline')
                created_game.save.assert_called_once_with()
                api.Game.create_game.assert_called_once()
            finally:
                api.server.dependency_overrides.clear()
        finally:
            sys.modules.pop('modules.api', None)
            if old_api_module is not None:
                sys.modules['modules.api'] = old_api_module

    def test_representative_postgresql_query_compiles_without_connection(self):
        smoke_database = PostgresqlDatabase(None)

        class SmokeBase(Model):
            class Meta:
                database = smoke_database

        class SmokeTeam(SmokeBase):
            guild_id = IntegerField()

        class SmokeGame(SmokeBase):
            is_completed = BooleanField(default=False)
            winner = ForeignKeyField(SmokeTeam, null=True)

        query = (
            SmokeGame
            .select(SmokeGame, SmokeTeam)
            .join(SmokeTeam)
            .where(
                (SmokeGame.is_completed == True)
                & (SmokeTeam.guild_id == 123)
            )
            .order_by(SmokeGame.id.desc())
        )
        sql, parameters = query.sql()

        self.assertIn('INNER JOIN', sql)
        self.assertIn('ORDER BY', sql)
        self.assertEqual(parameters, [True, 123])
        self.assertTrue(smoke_database.is_closed())


class CardRenderingCompatibilityTests(unittest.TestCase):
    def test_antiscam_average_hash_uses_current_pillow_api(self):
        settings_stub = ModuleType('settings')
        settings_stub.config = {}
        settings_stub.is_staff = lambda _member: False
        utilities_stub = ModuleType('modules.utilities')

        image_bytes = BytesIO()
        image = Image.new('RGB', (16, 16), '#000000')
        for x in range(8, 16):
            for y in range(16):
                image.putpixel((x, y), (255, 255, 255))
        image.save(image_bytes, format='PNG')

        old_antiscam_module = sys.modules.pop('modules.antiscam', None)
        try:
            with mock.patch.dict(
                    sys.modules,
                    {
                        'settings': settings_stub,
                        'modules.utilities': utilities_stub,
                    }):
                antiscam = importlib.import_module('modules.antiscam')

            image_hash = antiscam._average_hash(image_bytes.getvalue())
            self.assertEqual(len(image_hash), 64)
            self.assertEqual(set(image_hash), {'0', '1'})
        finally:
            sys.modules.pop('modules.antiscam', None)
            if old_antiscam_module is not None:
                sys.modules['modules.antiscam'] = old_antiscam_module

    def test_draft_card_renders_with_current_discord_and_pillow_apis(self):
        team = SimpleNamespace(id=1, name='Test Team', image_url='https://example.com/team.png')

        class Team:
            @staticmethod
            def get_or_except(**_kwargs):
                return team

        model_stubs = ModuleType('modules.models')
        model_stubs.Team = Team
        member = SimpleNamespace(
            id=1,
            name='Test Player',
            guild=SimpleNamespace(id=2),
            display_avatar=SimpleNamespace(
                replace=lambda **_kwargs: 'https://example.com/avatar.png'
            ),
        )
        role = SimpleNamespace(
            name='Test Team',
            colour=discord.Colour.blue(),
            color=discord.Colour.blue(),
        )
        images = [
            Image.new('RGBA', (100, 100), '#00ff00'),
            Image.new('RGBA', (100, 100), '#0000ff'),
        ]

        with mock.patch.dict(sys.modules, {'modules.models': model_stubs}):
            with mock.patch.object(
                    imgen.image_storage, 'resolve_image',
                    return_value=team.image_url):
                with mock.patch.object(
                        imgen, 'fetch_image',
                        side_effect=images) as fetch_image:
                    with mock.patch.object(
                            imgen, 'get_player_summary',
                            return_value=(
                                'LOCAL\n  1000 ELO\n'
                                'GLOBAL\n  1000 ELO'
                            )):
                        rendered = imgen.player_draft_card(member, role)

        try:
            self.assertEqual(
                rendered.filename, 'Test Team_selects_Test Player.png'
            )
            self.assertGreater(rendered.fp.getbuffer().nbytes, 0)
            self.assertEqual(fetch_image.call_count, 2)
            self.assertIsInstance(fetch_image.call_args_list[0].args[0], str)
        finally:
            rendered.close()


if __name__ == '__main__':
    unittest.main()
