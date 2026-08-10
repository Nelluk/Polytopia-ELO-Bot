"""Focused offline coverage for P9.3 operator Tribe emoji access."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


operator_workers = import_offline_runtime('modules.operator_tribe_workers')
operator_tribe = import_offline_runtime('modules.operator_tribe')
administration = import_offline_runtime('modules.administration')


class TribeRecord:
    def __init__(self, database, tribe_id, name, emoji):
        self.database = database
        self.id = tribe_id
        self.name = name
        self.emoji = emoji

    def save(self):
        self.database.events.append('save')
        if self.database.fail_save:
            raise peewee.OperationalError('save failed')


class TribeQuery:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def order_by(self, *_fields):
        return self

    def limit(self, limit):
        return self.rows[:limit]


class FakeTribeModel:
    name = SimpleNamespace()
    id = SimpleNamespace()
    rows = ()

    @classmethod
    def select(cls):
        return TribeQuery(cls.rows)


class FakeDatabase:
    def __init__(self):
        self.events = []
        self.logs = []
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_save = False
        self.fail_audit = False

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                database.events.append('connection-open')
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1
                database.events.append('connection-close')
                return False

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                self.emojis = {
                    row.id: row.emoji for row in FakeTribeModel.rows
                }
                self.logs = list(database.logs)
                database.events.append('atomic-open')
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    database.events.append('commit')
                    return False
                database.rollbacks += 1
                for row in FakeTribeModel.rows:
                    row.emoji = self.emojis[row.id]
                database.logs = self.logs
                database.events.append('rollback')
                return False

        return Atomic()


class FakeGameLog:
    database = None

    @classmethod
    def write(cls, **kwargs):
        cls.database.events.append('audit')
        if cls.database.fail_audit:
            raise peewee.OperationalError('audit failed')
        cls.database.logs.append(kwargs)


def read_request(**overrides):
    values = dict(guild_id=300, requester_id=100, tribe_lookup='Xin-xi')
    values.update(overrides)
    return operator_workers.OperatorTribeReadRequest(**values)


def mutation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_description='**Owner** (`100`)',
        tribe_lookup='Xin-xi',
        emoji='❤️',
    )
    values.update(overrides)
    return operator_workers.OperatorTribeMutationRequest(**values)


class OperatorTribeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.xin = TribeRecord(self.database, 1, 'Xin-xi', '😀')
        self.xeno = TribeRecord(self.database, 2, 'Xeno', '🦌')
        FakeTribeModel.rows = (self.xin, self.xeno)
        FakeGameLog.database = self.database
        self.patches = ExitStack()
        self.patches.enter_context(
            mock.patch.object(operator_workers.models, 'db', self.database)
        )
        self.patches.enter_context(
            mock.patch.object(operator_workers.models, 'Tribe', FakeTribeModel)
        )
        self.patches.enter_context(
            mock.patch.object(operator_workers.models, 'GameLog', FakeGameLog)
        )
        self.patches.enter_context(
            mock.patch.object(operator_workers.settings, 'owner_id', 100)
        )
        self.addCleanup(self.patches.close)

    def test_requests_are_frozen_primitive_snapshots(self):
        request = mutation_request()
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 999
        self.assertIsInstance(request.tribe_lookup, str)
        self.assertIsInstance(request.requester_description, str)
        self.assertNotIn('Member', repr(request))

    def test_read_and_atomic_edit_use_worker_local_connections_and_audit(self):
        read = operator_workers.read_tribe_emoji(read_request())
        self.assertEqual(read.emoji, '😀')
        self.assertFalse(read.changed)
        self.assertEqual(self.database.commits, 0)

        result = operator_workers.set_tribe_emoji(mutation_request())
        self.assertEqual((result.old_emoji, result.emoji), ('😀', '❤️'))
        self.assertTrue(result.changed)
        self.assertEqual(self.xin.emoji, '❤️')
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.logs[0]['guild_id'], 300)
        self.assertIn('/operator tribe emoji', self.database.logs[0]['message'])
        self.assertEqual(self.database.connection_opened, 2)
        self.assertEqual(self.database.connection_closed, 2)

    def test_owner_is_revalidated_before_input_validation_or_mutation(self):
        with self.assertRaises(operator_workers.OperatorTribePermissionError):
            operator_workers.set_tribe_emoji(
                mutation_request(requester_id=200, emoji='not emoji')
            )
        self.assertEqual(self.xin.emoji, '😀')
        self.assertEqual(self.database.logs, [])
        self.assertEqual(self.database.rollbacks, 1)

    def test_exact_prefix_missing_and_ambiguous_resolution(self):
        self.assertEqual(
            operator_workers.read_tribe_emoji(
                read_request(tribe_lookup='xin-XI')
            ).tribe_id,
            1,
        )
        self.assertEqual(
            operator_workers.read_tribe_emoji(
                read_request(tribe_lookup='xen')
            ).tribe_id,
            2,
        )
        with self.assertRaises(operator_workers.OperatorTribeLookupError):
            operator_workers.read_tribe_emoji(read_request(tribe_lookup='x'))
        with self.assertRaises(operator_workers.OperatorTribeLookupError):
            operator_workers.read_tribe_emoji(read_request(tribe_lookup='ely'))

    def test_unicode_static_and_animated_custom_emoji_are_supported(self):
        for value in (
            '😀',
            '❤️',
            '👩‍💻',
            '<:xinxi:123456789012345678>',
            '<a:xinxi_wave:123456789012345678>',
        ):
            with self.subTest(value=value):
                self.xin.emoji = 'old'
                result = operator_workers.set_tribe_emoji(
                    mutation_request(emoji=value)
                )
                self.assertEqual(result.emoji, value)

    def test_invalid_emoji_and_audit_failure_roll_back(self):
        with self.assertRaises(operator_workers.OperatorTribeValidationError):
            operator_workers.set_tribe_emoji(
                mutation_request(emoji='plain text')
            )
        self.assertEqual(self.xin.emoji, '😀')

        self.database.fail_audit = True
        with self.assertRaises(peewee.PeeweeException):
            operator_workers.set_tribe_emoji(mutation_request())
        self.assertEqual(self.xin.emoji, '😀')
        self.assertEqual(self.database.logs, [])
        self.assertEqual(self.database.rollbacks, 2)

    def test_autocomplete_is_owner_only_filtered_and_bounded(self):
        results = operator_workers.list_tribes(
            operator_workers.OperatorTribeAutocompleteRequest(
                requester_id=100,
                current='xin',
            )
        )
        self.assertEqual([result.tribe_name for result in results], ['Xin-xi'])
        with self.assertRaises(operator_workers.OperatorTribePermissionError):
            operator_workers.list_tribes(
                operator_workers.OperatorTribeAutocompleteRequest(
                    requester_id=200,
                    current='',
                )
            )


class OperatorTribeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_result_is_actor_attributed_after_private_cleanup(self):
        events = []

        async def delete_original():
            events.append('delete-private')

        async def send(content, **kwargs):
            events.append(('public', content, kwargs))

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=100, display_name='Owner', name='Owner'),
            delete_original_response=delete_original,
            channel=SimpleNamespace(send=send),
        )
        result = operator_workers.OperatorTribeResult(
            guild_id=300,
            tribe_id=1,
            tribe_name='Xin-xi',
            old_emoji='😀',
            emoji='❤️',
            changed=True,
        )
        await operator_tribe.publish_result(interaction, result)
        self.assertEqual(events[0], 'delete-private')
        self.assertEqual(events[1][0], 'public')
        self.assertIn('Owner', events[1][1])
        self.assertIn('Xin-xi', events[1][1])

    async def test_autocomplete_hides_catalog_from_non_owner(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=200))
        with mock.patch.object(operator_tribe.settings, 'owner_id', 100), \
                mock.patch.object(
                    operator_tribe.operator_tribe_workers,
                    'run_autocomplete',
                    new=mock.AsyncMock(),
                ) as worker:
            self.assertEqual(
                await operator_tribe.autocomplete_tribes(interaction, ''),
                [],
            )
        worker.assert_not_awaited()


class OperatorTribeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(
            administration.administration
        )
        self.operator_group = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.command = self.operator_group.get_command('tribe').get_command(
            'emoji'
        )

    def test_exact_registration_shape_and_prefix_retirement(self):
        self.assertTrue(self.operator_group.guild_only)
        self.assertEqual(
            self.operator_group.default_permissions,
            discord.Permissions(administrator=True),
        )
        self.assertEqual(
            [(parameter.name, parameter.required) for parameter in self.command.parameters],
            [('tribe', True), ('emoji', False)],
        )
        prefix_names = {
            command.name
            for command in administration.administration.__cog_commands__
        }
        self.assertNotIn('tribe_emoji', prefix_names)

    async def test_non_owner_denial_is_private_and_does_not_defer(self):
        response = SimpleNamespace(
            send_message=mock.AsyncMock(),
            defer=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=200),
            response=response,
        )
        with mock.patch.object(administration.settings, 'owner_id', 100), \
                mock.patch.object(
                    administration.operator_tribe_workers,
                    'run_read',
                    new=mock.AsyncMock(),
                ) as worker:
            await self.command.callback(self.cog, interaction, 'Xin-xi', None)
        response.send_message.assert_awaited_once_with(
            'Only the configured bot owner can manage Tribe emojis.',
            ephemeral=True,
        )
        response.defer.assert_not_awaited()
        worker.assert_not_awaited()

    async def test_owner_defers_before_worker_and_publishes_success(self):
        events = []

        async def defer(**kwargs):
            events.append(('defer', kwargs))

        result = operator_workers.OperatorTribeResult(
            guild_id=300,
            tribe_id=1,
            tribe_name='Xin-xi',
            old_emoji='😀',
            emoji='😀',
            changed=False,
        )

        async def run_read(_request):
            events.append('worker')
            return result

        async def publish(_interaction, _result):
            events.append('public')

        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=100, display_name='Owner', name='Owner'),
            response=SimpleNamespace(defer=defer),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(administration.settings, 'owner_id', 100), \
                mock.patch.object(
                    administration.operator_tribe_workers,
                    'run_read',
                    new=run_read,
                ), mock.patch.object(
                    administration.operator_tribe_service,
                    'publish_result',
                    new=publish,
                ):
            returned = await self.command.callback(
                self.cog,
                interaction,
                'Xin-xi',
                None,
            )
        self.assertIs(returned, result)
        self.assertEqual(events, [('defer', {'ephemeral': True}), 'worker', 'public'])

    async def test_safe_worker_failures_remain_private(self):
        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=100, display_name='Owner', name='Owner'),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(administration.settings, 'owner_id', 100), \
                mock.patch.object(
                    administration.operator_tribe_workers,
                    'run_read',
                    new=mock.AsyncMock(
                        side_effect=operator_workers.OperatorTribeLookupError(
                            'No Tribe matched "bad".'
                        )
                    ),
                ):
            await self.command.callback(self.cog, interaction, 'bad', None)
        interaction.followup.send.assert_awaited_once_with(
            'No Tribe matched "bad".',
            ephemeral=True,
        )


if __name__ == '__main__':
    unittest.main()
