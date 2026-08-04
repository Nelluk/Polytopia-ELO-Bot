"""Focused offline coverage for the P6.2 player timezone workflow."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
import copy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


values = import_offline_runtime('modules.player_timezone_values')
workers = import_offline_runtime('modules.player_timezone_workers')
timezone = import_offline_runtime('modules.player_timezone')
games = import_offline_runtime('modules.games')


def member(
    discord_id=100,
    *,
    name='AccountUser',
    display_name='Account User',
    nick=None,
    roles=(),
):
    return SimpleNamespace(
        id=discord_id,
        name=name,
        display_name=display_name,
        nick=nick,
        roles=tuple(SimpleNamespace(name=role) for role in roles),
        guild=SimpleNamespace(id=300),
    )


def request(*, target_id=100, actor_roles=(), offset_minutes=330, clear=False):
    actor = workers.player_registration_workers.MemberSnapshot(
        discord_id=100,
        discord_name='Actor',
        discord_nick='Act',
        display_name='Actor Display',
        role_names=tuple(actor_roles),
    )
    target = workers.player_registration_workers.MemberSnapshot(
        discord_id=target_id,
        discord_name='Target',
        discord_nick=None,
        display_name='Target Display',
        role_names=(),
    )
    return workers.PlayerTimezoneRequest(
        guild_id=300,
        requester_id=100,
        actor=actor,
        target=target,
        offset_minutes=offset_minutes,
        clear=clear,
        requester_is_staff=bool(actor_roles),
        native=True,
        invoked_with='/player timezone',
    )


class TimezoneValueTests(unittest.TestCase):
    def test_parser_normalizes_supported_quarter_hours_and_bounds(self):
        cases = {
            'UTC-12:00': -720,
            'UTC+14:00': 840,
            'UTC-5': -300,
            'GMT+05:30': 330,
            'UTC+05:15': 315,
            'UTC-04:45': -285,
            'UTC': 0,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(timezone.parse_timezone_offset(raw), expected)
        self.assertEqual(
            timezone.normalize_timezone_offset('GMT-5:30'),
            'UTC-05:30',
        )

    def test_parser_rejects_out_of_range_and_non_quarter_values(self):
        for raw in ('UTC-12:15', 'UTC+14:15', 'UTC+05:10', 'UTC+25:00'):
            with self.subTest(raw=raw):
                with self.assertRaises(timezone.TimezoneValidationError):
                    timezone.parse_timezone_offset(raw)
        with self.assertRaises(timezone.TimezoneValidationError):
            timezone.parse_timezone_offset('GMT', allow_gmt=False)

    def test_native_boundary_requires_normalized_utc_form(self):
        self.assertEqual(
            timezone.parse_native_timezone_offset('UTC+05:15'),
            315,
        )
        for raw in ('UTC+5:15', 'UTC+05', 'UTC', 'GMT+05:15'):
            with self.subTest(raw=raw):
                with self.assertRaises(timezone.TimezoneValidationError):
                    timezone.parse_native_timezone_offset(raw)

    def test_autocomplete_is_normalized_and_bounded(self):
        suggestions = values.offset_suggestions('UTC+', limit=100)
        self.assertLessEqual(len(suggestions), 25)
        self.assertTrue(all(value.startswith('UTC+') for value in suggestions))
        self.assertTrue(all(
            values.parse_timezone_offset(value) % 15 == 0
            for value in suggestions
        ))
        self.assertEqual(
            values.offset_suggestions('UTC+14', limit=25),
            ('UTC+14:00',),
        )

    def test_read_precedence_and_explicit_clear_tombstone(self):
        member_value = SimpleNamespace(
            timezone_offset_minutes=330,
            timezone_offset=-4,
            timezone_offset_cleared=False,
        )
        self.assertEqual(
            timezone.effective_timezone_offset_minutes(member_value),
            330,
        )
        member_value.timezone_offset_minutes = None
        self.assertEqual(
            timezone.effective_timezone_offset_minutes(member_value),
            -240,
        )
        member_value.timezone_offset_cleared = True
        self.assertIsNone(
            timezone.effective_timezone_offset_minutes(member_value)
        )


class TransactionDatabase:
    def __init__(self):
        self.events = []
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.member = None
        self.player = None
        self.logs = []
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
                self.member_state = copy.copy(database.member.__dict__)
                self.logs = list(database.logs)
                database.events.append('atomic-open')
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    database.events.append('commit')
                    return False
                database.member.__dict__.clear()
                database.member.__dict__.update(self.member_state)
                database.logs[:] = self.logs
                database.rollbacks += 1
                database.events.append('rollback')
                return False

        return Atomic()


class FakeField:
    def __eq__(self, other):
        del other
        return self

    def __and__(self, other):
        del other
        return self


class FakeMemberModel:
    discord_id = FakeField()
    timezone_offset_minutes = FakeField()
    timezone_offset_cleared = FakeField()
    record = None

    @classmethod
    def get_or_none(cls, **kwargs):
        del kwargs
        return cls.record


class FakeMemberRecord:
    def __init__(self, database):
        self.database = database
        self.discord_id = 200
        self.name = 'Target Discord Name'
        self.timezone_offset = -4
        self.timezone_offset_minutes = None
        self.timezone_offset_cleared = False

    def save(self, only=None):
        self.database.events.append(('save', tuple(only or ())))


class FakePlayerQuery:
    def get_or_none(self):
        return FakePlayerModel.record


class FakePlayerModel:
    guild_id = FakeField()
    record = None

    @classmethod
    def select(cls):
        return cls

    @classmethod
    def join(cls, model):
        del model
        return cls

    @classmethod
    def where(cls, expression):
        del expression
        return FakePlayerQuery()


class FakeGameLog:
    database = None

    @classmethod
    def write(cls, **kwargs):
        cls.database.events.append('audit')
        if cls.database.fail_audit:
            raise peewee.OperationalError('audit failed')
        cls.database.logs.append(kwargs)


class TimezoneWorkerTests(unittest.TestCase):
    def setUp(self):
        self.database = TransactionDatabase()
        self.database.member = FakeMemberRecord(self.database)
        self.database.player = SimpleNamespace(name='Target Player Label')
        FakeMemberModel.record = self.database.member
        FakePlayerModel.record = self.database.player
        FakeGameLog.database = self.database
        self.patches = ExitStack()
        self.patches.enter_context(mock.patch.object(
            workers.models,
            'db',
            self.database,
        ))
        self.patches.enter_context(mock.patch.object(
            workers.models,
            'DiscordMember',
            FakeMemberModel,
        ))
        self.patches.enter_context(mock.patch.object(
            workers.models,
            'Player',
            FakePlayerModel,
        ))
        self.patches.enter_context(mock.patch.object(
            workers.models,
            'GameLog',
            FakeGameLog,
        ))
        self.patches.enter_context(mock.patch.object(
            workers.player_registration_workers,
            'is_staff_snapshot',
            return_value=True,
        ))
        self.addCleanup(self.patches.close)

    def test_request_is_frozen_and_worker_commits_minutes_without_legacy_write(self):
        request_value = request(target_id=200, actor_roles=('Helper',))
        with self.assertRaises(FrozenInstanceError):
            request_value.offset_minutes = 15
        result = workers.write_timezone(request_value)
        self.assertEqual(result.offset_minutes, 330)
        self.assertEqual(result.old_offset_minutes, -240)
        self.assertEqual(self.database.member.timezone_offset_minutes, 330)
        self.assertFalse(self.database.member.timezone_offset_cleared)
        self.assertEqual(self.database.member.timezone_offset, -4)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertEqual(self.database.logs[0]['guild_id'], 300)
        self.assertEqual(self.database.logs[0]['game_id'], 0)
        self.assertIn('Actor Display', self.database.logs[0]['message'])
        self.assertIn('Target Display', self.database.logs[0]['message'])

    def test_clear_sets_tombstone_and_does_not_resurrect_legacy_value(self):
        result = workers.write_timezone(
            request(target_id=200, actor_roles=('Helper',), offset_minutes=None, clear=True)
        )
        self.assertTrue(result.cleared)
        self.assertIsNone(result.offset_minutes)
        self.assertIsNone(
            timezone.effective_timezone_offset_minutes(self.database.member)
        )
        self.assertIsNone(self.database.member.timezone_offset_minutes)
        self.assertTrue(self.database.member.timezone_offset_cleared)
        self.assertEqual(self.database.member.timezone_offset, -4)

    def test_audit_failure_rolls_back_preference_and_has_no_success_result(self):
        self.database.fail_audit = True
        with self.assertRaises(peewee.OperationalError):
            workers.write_timezone(request(offset_minutes=345))
        self.assertIsNone(self.database.member.timezone_offset_minutes)
        self.assertFalse(self.database.member.timezone_offset_cleared)
        self.assertEqual(self.database.member.timezone_offset, -4)
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_worker_revalidates_staff_boundary_before_connection(self):
        self.patches.close()
        self.patches = ExitStack()
        self.patches.enter_context(mock.patch.object(
            workers.models,
            'db',
            self.database,
        ))
        self.patches.enter_context(mock.patch.object(
            workers.player_registration_workers,
            'is_staff_snapshot',
            return_value=False,
        ))
        with self.assertRaises(workers.PlayerTimezonePermissionError):
            workers.write_timezone(request(target_id=200, actor_roles=('Member',)))
        self.assertEqual(self.database.connection_opened, 0)

    def test_read_uses_effective_legacy_fallback_and_connection_without_commit(self):
        result = workers.read_timezone(
            request(target_id=200, offset_minutes=None, clear=False)
        )
        self.assertEqual(result.offset_minutes, -240)
        self.assertFalse(result.mutated)
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_slow_worker_remains_cancellable_without_blocking_event_loop(self):
        original = workers.write_timezone
        started = threading.Event()
        release = threading.Event()

        def slow(request_value):
            del request_value
            started.set()
            release.wait(timeout=2)
            return workers.PlayerTimezoneResult(
                guild_id=300,
                requester_id=100,
                target_id=100,
                target_name='Target',
                actor_description='Actor',
                target_description='Target',
                old_offset_minutes=None,
                offset_minutes=0,
                legacy_offset_hours=None,
                cleared=False,
                mutated=True,
            )

        async def exercise():
            workers.write_timezone = slow
            try:
                heartbeat = asyncio.create_task(asyncio.sleep(0.01))
                task = asyncio.create_task(
                    workers.run_timezone_request(request())
                )
                await asyncio.wait_for(heartbeat, timeout=0.04)
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                workers.write_timezone = original

        asyncio.run(exercise())


class TimezoneAdapterAndCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_build_request_parity_and_conflicting_options(self):
        actor = member(100, roles=('Helper',))
        target = member(200)
        with mock.patch.object(timezone.settings, 'is_staff', return_value=True):
            request_value = timezone.build_request(
                actor=actor,
                target=target,
                guild_id=300,
                offset='UTC+05:15',
            )
        self.assertEqual(request_value.offset_minutes, 315)
        self.assertEqual(request_value.target.discord_id, 200)
        self.assertIsInstance(request_value.target.role_names, tuple)
        with self.assertRaises(timezone.TimezoneValidationError):
            timezone.build_request(
                actor=actor,
                guild_id=300,
                offset='UTC+01:00',
                clear=True,
            )
        with mock.patch.object(timezone.settings, 'is_staff', return_value=False):
            with self.assertRaises(timezone.TimezonePermissionError):
                timezone.build_request(
                    actor=member(100),
                    target=target,
                    guild_id=300,
                    offset='UTC+01:00',
                )

    async def test_prefix_grammar_preserves_self_and_staff_target_forms(self):
        actor = member(100, roles=('Helper',))
        other = member(200)
        ctx = SimpleNamespace(
            author=actor,
            guild=SimpleNamespace(id=300),
            prefix='$',
            message=SimpleNamespace(mentions=()),
        )
        with mock.patch.object(timezone.settings, 'is_staff', return_value=True):
            self_request = await timezone.build_prefix_request(ctx, ('UTC', '+5'))
            self.assertEqual(self_request.target.discord_id, 100)
            self.assertEqual(self_request.offset_minutes, 300)
            with mock.patch.object(
                timezone.utilities,
                'get_guild_member',
                new=mock.AsyncMock(return_value=[other]),
            ):
                target_request = await timezone.build_prefix_request(
                    ctx,
                    ('Other', 'GMT-4:45'),
                )
        self.assertEqual(target_request.target.discord_id, 200)
        self.assertEqual(target_request.offset_minutes, -285)

    def group(self):
        return next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'player'
        )

    async def test_slash_registration_shape_and_public_success_after_defer(self):
        command = self.group().get_command('timezone')
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter in command.parameters
            ],
            [
                ('member', discord.AppCommandOptionType.user, False),
                ('offset', discord.AppCommandOptionType.string, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        request_value = request(offset_minutes=315)
        result = workers.PlayerTimezoneResult(
            guild_id=300,
            requester_id=100,
            target_id=100,
            target_name='Target',
            actor_description='**Actor** (`100`)',
            target_description='**Target** (`100`)',
            old_offset_minutes=None,
            offset_minutes=315,
            legacy_offset_hours=None,
            cleared=False,
            mutated=True,
        )
        interaction = SimpleNamespace(
            user=member(),
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog = games.polygames.__new__(games.polygames)
        with (
            mock.patch.object(games.player_timezone, 'build_request', return_value=request_value),
            mock.patch.object(
                games.player_timezone_workers,
                'run_timezone_request',
                new=mock.AsyncMock(return_value=result),
            ),
        ):
            await command.callback(cog, interaction, None, 'UTC+05:15', False)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()

    async def test_native_database_failure_has_no_public_effect(self):
        command = self.group().get_command('timezone')
        request_value = request(offset_minutes=315)
        interaction = SimpleNamespace(
            user=member(),
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog = games.polygames.__new__(games.polygames)
        with (
            mock.patch.object(games.player_timezone, 'build_request', return_value=request_value),
            mock.patch.object(
                games.player_timezone_workers,
                'run_timezone_request',
                new=mock.AsyncMock(side_effect=peewee.OperationalError('db down')),
            ),
        ):
            await command.callback(cog, interaction, None, 'UTC+05:15', False)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
        interaction.channel.send.assert_not_awaited()
        interaction.delete_original_response.assert_not_awaited()

    async def test_prefix_settime_routes_shared_worker_and_stays_public(self):
        prefix_command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'settime'
        )
        request_value = request(offset_minutes=-285)
        result = workers.PlayerTimezoneResult(
            guild_id=300,
            requester_id=100,
            target_id=100,
            target_name='Target',
            actor_description='Actor',
            target_description='Target',
            old_offset_minutes=None,
            offset_minutes=-285,
            legacy_offset_hours=None,
            cleared=False,
            mutated=True,
        )
        ctx = SimpleNamespace(
            author=member(),
            guild=SimpleNamespace(id=300),
            prefix='$',
            send=mock.AsyncMock(),
        )
        cog = games.polygames.__new__(games.polygames)
        with (
            mock.patch.object(
                games.player_timezone,
                'build_prefix_request',
                new=mock.AsyncMock(return_value=request_value),
            ),
            mock.patch.object(
                games.player_timezone_workers,
                'run_timezone_request',
                new=mock.AsyncMock(return_value=result),
            ) as run,
        ):
            await prefix_command.callback(cog, ctx, 'GMT-4:45')
        run.assert_awaited_once_with(request_value)
        message = ctx.send.await_args.args[0]
        self.assertIn('UTC-04:45', message)
        self.assertIn('account-wide fixed UTC offset', message)
        self.assertIn('Actor', message)
        self.assertIn('Target', message)


if __name__ == '__main__':
    unittest.main()
