"""Focused coverage for P8.8/P8.9 House attributes and creation."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.house_attributes_workers')
service = import_offline_runtime('modules.house_attributes')
league = import_offline_runtime('modules.league')


class FakeDatabase:
    def __init__(self):
        self.connections = 0
        self.atomics = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.connections += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()

    def atomic(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.atomics += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()


class FakeQuery:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def order_by(self, *args):
        return self

    def __iter__(self):
        return iter(self.rows)


def read_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_is_mod=False,
        league_scope=True,
        channel_allowed=True,
        house_lookup='Ninjas',
        requester_role_names=('Ninjas',),
        attribute=workers.HOUSE_ATTRIBUTE_NAME,
        requester_description='**Actor** (`10`)',
    )
    values.update(overrides)
    return workers.HouseAttributeReadRequest(**values)


def read_result(**overrides):
    values = dict(
        guild_id=300,
        house_id=7,
        house_name='Ninjas',
        attribute=workers.HOUSE_ATTRIBUTE_NAME,
        image_url=None,
        effective_image_source='none',
        local_image_bytes=None,
        local_image_digest=None,
    )
    values.update(overrides)
    return workers.HouseAttributeReadResult(**values)


def mutation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_is_mod=True,
        league_scope=True,
        channel_allowed=True,
        house_id=7,
        attribute=workers.HOUSE_ATTRIBUTE_NAME,
        value='New Ninjas',
        image_operation=None,
        staged_path=None,
        expected_name='Ninjas',
        expected_image_url=None,
        expected_local_digest=None,
        requester_description='**Actor** (`10`)',
    )
    values.update(overrides)
    return workers.HouseAttributeMutationRequest(**values)


def creation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_is_mod=True,
        league_scope=True,
        channel_allowed=True,
        name='New House',
        requester_description='**Actor** (`10`)',
    )
    values.update(overrides)
    return workers.HouseCreationRequest(**values)


class RegistrationTests(unittest.TestCase):
    def test_native_shapes_and_prefix_retirement(self):
        root = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'house'
        )
        self.assertEqual(
            {command.name for command in root.commands},
            {'show', 'list', 'create', 'name', 'image'},
        )
        create = root.get_command('create')
        self.assertEqual(
            [(parameter.name, parameter.required, parameter.type)
             for parameter in create.parameters],
            [('name', True, discord.AppCommandOptionType.string)],
        )
        name = root.get_command('name')
        self.assertEqual(
            [(parameter.name, parameter.required, parameter.type)
             for parameter in name.parameters],
            [
                ('house', False, discord.AppCommandOptionType.string),
                ('name', False, discord.AppCommandOptionType.string),
            ],
        )
        image = root.get_command('image')
        self.assertEqual(
            [(parameter.name, parameter.required, parameter.type)
             for parameter in image.parameters],
            [
                ('house', False, discord.AppCommandOptionType.string),
                ('image', False, discord.AppCommandOptionType.attachment),
                ('clear', False, discord.AppCommandOptionType.boolean),
            ],
        )
        prefix = {command.name: command for command in league.league.__cog_commands__}
        self.assertNotIn('house_add', prefix)
        self.assertNotIn('house_rename', prefix)
        self.assertNotIn('house_image', prefix)


class WorkerTests(unittest.TestCase):
    def test_requests_are_frozen_primitive_values(self):
        request = read_request()
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 1
        self.assertEqual(request.requester_role_names, ('Ninjas',))

    def test_read_resolves_explicit_and_inferred_house(self):
        rows = (
            SimpleNamespace(id=7, name='Ninjas', image_url=None),
            SimpleNamespace(id=8, name='The Jets', image_url='https://example.test/jets.png'),
        )
        house_model = SimpleNamespace(
            select=mock.Mock(return_value=FakeQuery(rows)),
            name=mock.MagicMock(),
        )
        database = FakeDatabase()
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models, 'House', house_model
        ), mock.patch.object(workers, '_local_state', return_value=(None, None)):
            explicit = workers.read_house_attribute(read_request(house_lookup='jet'))
            inferred = workers.read_house_attribute(read_request(house_lookup=None))
        self.assertEqual(explicit.house_id, 8)
        self.assertEqual(explicit.effective_image_source, 'url')
        self.assertEqual(inferred.house_id, 7)
        self.assertEqual(database.connections, 2)

    def test_name_mutation_is_mod_only_atomic_and_audited(self):
        house = SimpleNamespace(
            id=7,
            name='Ninjas',
            image_url=None,
            save=mock.Mock(),
        )
        house_model = SimpleNamespace(get_by_id=mock.Mock(return_value=house))
        database = FakeDatabase()
        game_log = SimpleNamespace(write=mock.Mock())
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models, 'House', house_model
        ), mock.patch.object(workers.models, 'GameLog', game_log), mock.patch.object(
            workers, '_local_state', return_value=(None, None)
        ):
            result = workers.mutate_house_attribute(mutation_request())
        self.assertEqual(result.old_name, 'Ninjas')
        self.assertEqual(result.house_name, 'New Ninjas')
        self.assertEqual(house.name, 'New Ninjas')
        self.assertEqual(database.atomics, 1)
        game_log.write.assert_called_once()

        with self.assertRaises(workers.HouseAttributePermissionError):
            workers.mutate_house_attribute(
                mutation_request(requester_is_mod=False)
            )

    def test_name_validation_and_conflict_are_bounded(self):
        for value in ('', 'x' * 51, 'bad\nname'):
            with self.assertRaises(workers.HouseAttributeValidationError):
                workers.validate_house_name(value)
        self.assertEqual(workers.validate_house_name('  New House  '), 'New House')

    def test_image_mutation_requires_staged_file_and_clears_url_in_transaction(self):
        house = SimpleNamespace(
            id=7,
            name='Ninjas',
            image_url='https://example.test/old.png',
            save=mock.Mock(),
        )
        database = FakeDatabase()
        with tempfile.NamedTemporaryFile() as staged, mock.patch.object(
            workers.models, 'db', database
        ), mock.patch.object(
            workers.models.House, 'get_by_id', return_value=house
        ), mock.patch.object(
            workers.models.GameLog, 'write'
        ) as audit, mock.patch.object(
            workers, '_local_state', return_value=(b'old', 'digest')
        ):
            result = workers.mutate_house_attribute(
                mutation_request(
                    attribute=workers.HOUSE_ATTRIBUTE_IMAGE,
                    value=None,
                    image_operation=workers.HOUSE_IMAGE_LOCAL,
                    staged_path=staged.name,
                    expected_image_url='https://example.test/old.png',
                    expected_local_digest='digest',
                )
            )
        self.assertIsNone(result.image_url)
        self.assertIsNone(house.image_url)
        self.assertEqual(database.atomics, 1)
        audit.assert_called_once()

    def test_creation_is_mod_only_worker_local_atomic_and_audited(self):
        database = FakeDatabase()
        house = SimpleNamespace(id=12, name='New House')
        house_model = SimpleNamespace(create=mock.Mock(return_value=house))
        game_log = SimpleNamespace(write=mock.Mock())
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models, 'House', house_model
        ), mock.patch.object(workers.models, 'GameLog', game_log):
            result = workers.create_house(creation_request(name='  New House  '))
        self.assertEqual(result.house_id, 12)
        self.assertEqual(result.house_name, 'New House')
        house_model.create.assert_called_once_with(name='New House')
        self.assertEqual(database.connections, 1)
        self.assertEqual(database.atomics, 1)
        game_log.write.assert_called_once_with(
            guild_id=300,
            message=(
                "**Actor** (`10`) created House 'New House' ID 12 "
                '(/house create)'
            ),
        )

        with self.assertRaises(workers.HouseAttributePermissionError):
            workers.create_house(creation_request(requester_is_mod=False))

    def test_creation_duplicate_is_private_validation_conflict(self):
        database = FakeDatabase()
        house_model = SimpleNamespace(
            create=mock.Mock(side_effect=peewee.IntegrityError('duplicate'))
        )
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models, 'House', house_model
        ):
            with self.assertRaisesRegex(
                workers.HouseAttributeValidationError,
                'already exists',
            ):
                workers.create_house(creation_request())


class AsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_read_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow(_request):
            started.set()
            while not release.is_set():
                time.sleep(0.001)
            return read_result()

        with mock.patch.object(workers, 'read_house_attribute', side_effect=slow):
            task = asyncio.create_task(workers.run_house_attribute_read(read_request()))
            for _ in range(1000):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            release.set()
            result = await asyncio.wait_for(task, timeout=1)
        self.assertEqual(result.house_id, 7)

    async def test_filesystem_publication_follows_worker_commit(self):
        staged = SimpleNamespace(path='/tmp/staged-house', data=b'png')
        result = workers.HouseAttributeMutationResult(
            guild_id=300,
            house_id=7,
            attribute=workers.HOUSE_ATTRIBUTE_IMAGE,
            old_name='Ninjas',
            house_name='Ninjas',
            image_operation=workers.HOUSE_IMAGE_LOCAL,
            old_image_url=None,
            image_url=None,
        )
        events = []
        with mock.patch.object(
            workers,
            'run_house_attribute_mutation',
            new=mock.AsyncMock(side_effect=lambda request: events.append('commit') or result),
        ), mock.patch.object(
            service,
            '_publish_filesystem',
            new=mock.AsyncMock(side_effect=lambda result, staged: events.append('publish')),
        ):
            loaded = await service.run_mutation(
                mutation_request(
                    attribute=workers.HOUSE_ATTRIBUTE_IMAGE,
                    value=None,
                    image_operation=workers.HOUSE_IMAGE_LOCAL,
                    staged_path=staged.path,
                ),
                staged=staged,
            )
        self.assertEqual(events, ['commit', 'publish'])
        self.assertEqual(loaded.local_image_bytes, b'png')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, *, user_id=10):
        return SimpleNamespace(
            guild=SimpleNamespace(id=300),
            guild_id=300,
            channel_id=400,
            channel=SimpleNamespace(send=mock.AsyncMock()),
            user=SimpleNamespace(id=user_id, roles=(), display_name='Actor'),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

    async def test_non_mod_mutation_denied_before_defer(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction()
        command = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'house'
        ).get_command('name')
        with mock.patch.object(
            service,
            'native_access_error',
            return_value='You do not have permission to manage House attributes.',
        ):
            await command.callback(cog, interaction, 'Ninjas', 'New Name')
        interaction.response.send_message.assert_awaited_once_with(
            'You do not have permission to manage House attributes.',
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    async def test_name_read_defers_then_publishes_publicly(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction()
        command = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'house'
        ).get_command('name')
        events = []
        with mock.patch.object(
            service, 'native_access_error', return_value=None
        ), mock.patch.object(
            service, 'build_read_request', return_value=read_request()
        ), mock.patch.object(
            service,
            'run_read',
            new=mock.AsyncMock(side_effect=lambda request: events.append('read') or read_result()),
        ), mock.patch.object(
            service,
            'publish_read',
            new=mock.AsyncMock(side_effect=lambda *args, **kwargs: events.append('publish')),
        ):
            original = interaction.response.defer

            async def defer(**kwargs):
                events.append('defer')
                return await original(**kwargs)

            interaction.response.defer = mock.AsyncMock(side_effect=defer)
            await command.callback(cog, interaction, 'Ninjas', None)
        self.assertEqual(events, ['defer', 'read', 'publish'])

    async def test_database_failure_is_private_and_has_no_public_success(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction()
        command = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'house'
        ).get_command('image')
        with mock.patch.object(
            service, 'native_access_error', return_value=None
        ), mock.patch.object(
            service, 'build_read_request', return_value=read_request()
        ), mock.patch.object(
            service,
            'run_read',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
        ), mock.patch.object(
            service, 'publish_read', new=mock.AsyncMock()
        ) as publish:
            await command.callback(cog, interaction, 'Ninjas', None, False)
        publish.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            'House image operation failed and rolled back.', ephemeral=True
        )

    async def test_create_denies_non_mod_before_defer(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction()
        command = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'house'
        ).get_command('create')
        with mock.patch.object(
            service,
            'native_access_error',
            return_value='You do not have permission to create Houses.',
        ):
            await command.callback(cog, interaction, 'New House')
        interaction.response.send_message.assert_awaited_once_with(
            'You do not have permission to create Houses.',
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    async def test_create_defers_commits_then_publishes_publicly(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction()
        command = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'house'
        ).get_command('create')
        request = creation_request()
        result = workers.HouseCreationResult(
            guild_id=300,
            house_id=12,
            house_name='New House',
        )
        events = []
        original = interaction.response.defer

        async def defer(**kwargs):
            events.append('defer')
            return await original(**kwargs)

        interaction.response.defer = mock.AsyncMock(side_effect=defer)
        with mock.patch.object(
            service, 'native_access_error', return_value=None
        ), mock.patch.object(
            service, 'build_creation_request', return_value=request
        ), mock.patch.object(
            service,
            'run_creation',
            new=mock.AsyncMock(
                side_effect=lambda _request: events.append('commit') or result
            ),
        ), mock.patch.object(
            service,
            'publish_creation',
            new=mock.AsyncMock(
                side_effect=lambda *args, **kwargs: events.append('publish')
            ),
        ):
            loaded = await command.callback(cog, interaction, 'New House')
        self.assertEqual(loaded, result)
        self.assertEqual(events, ['defer', 'commit', 'publish'])
