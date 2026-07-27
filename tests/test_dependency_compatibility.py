import asyncio
import importlib
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest import mock
import warnings

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
            'gspread_asyncio',
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
        for package in ('discord.py', 'fastapi', 'Pillow', 'peewee'):
            with self.subTest(package=package):
                self.assertIn(package, details['packages'])

    def test_bot_constructs_without_connecting_to_database_or_discord(self):
        stubs = {}
        for module_name in (
                'modules.initialize_data', 'modules.models',
                'modules.utilities'):
            stubs[module_name] = ModuleType(module_name)
        stubs['modules.initialize_data'].initialize_data = lambda: None

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

        old_api_module = sys.modules.pop('modules.api', None)
        try:
            with mock.patch.dict(sys.modules, {'modules.models': model_stubs}):
                # FastAPI currently warns about the repository's legacy
                # on_event startup hook. The API migration will address that
                # in its own dependency-upgrade step.
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', DeprecationWarning)
                    api = importlib.import_module('modules.api')

            game = api.NewGame(
                game_name='Dependency smoke test',
                guild_id=123,
                sides_discord_ids=[[1], [2]],
            )
            self.assertEqual(game.game_name, 'Dependency smoke test')
            self.assertFalse(game.is_ranked)

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
                    imgen, 'fetch_image', side_effect=images) as fetch_image:
                with mock.patch.object(
                        imgen, 'get_player_summary',
                        return_value='LOCAL\n  1000 ELO\nGLOBAL\n  1000 ELO'):
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
