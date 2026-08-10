"""Offline coverage for recurring ELO Champion reconciliation."""

import asyncio
from contextlib import AbstractContextManager
import datetime
import importlib
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee
from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase


def import_offline_runtime(module_name):
    with mock.patch.object(
        PostgresqlExtDatabase, 'connect', return_value=True
    ), mock.patch.object(
        PostgresqlExtDatabase, 'close', return_value=True
    ), mock.patch.object(
        PostgresqlExtDatabase, 'create_tables'
    ), mock.patch.object(
        SchemaManager, 'create_foreign_key'
    ):
        return importlib.import_module(module_name)


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.thread_ids = []

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                database.thread_ids.append(threading.get_ident())

            def __exit__(self, *_args):
                database.closed += 1

        return Context()

    def atomic(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, *_args):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1

        return Context()


class FakeQuery:
    def __init__(self, row):
        self.row = row

    def limit(self, _value):
        return self

    def first(self):
        return self.row


class FakeMember:
    def __init__(self, member_id, name, *, remove_error=None, add_error=None):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.remove_error = remove_error
        self.add_error = add_error
        self.removals = []
        self.additions = []

    async def remove_roles(self, role, *, reason):
        self.removals.append((role, reason))
        if self.remove_error is not None:
            raise self.remove_error

    async def add_roles(self, role, *, reason):
        self.additions.append((role, reason))
        if self.add_error is not None:
            raise self.add_error


class FakeGuild:
    def __init__(self, guild_id, role, members):
        self.id = guild_id
        self.name = f'Guild {guild_id}'
        self.roles = [role]
        self._members = {member.id: member for member in members}

    def get_member(self, member_id):
        return self._members.get(member_id)


class FakeBot:
    def __init__(self, guilds):
        self.guilds = tuple(guilds)
        self._guilds = {guild.id: guild for guild in guilds}

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)


class ChampionRoleWorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.workers = import_offline_runtime('modules.champion_role_workers')

    async def test_plan_is_immutable_worker_owned_and_handles_default_elo(self):
        database = FakeDatabase()
        global_row = SimpleNamespace(discord_id=90, elo_field=1000)
        local_rows = {
            100: SimpleNamespace(
                discord_member=SimpleNamespace(discord_id=91),
                elo_field=1000,
            ),
            200: SimpleNamespace(
                discord_member=SimpleNamespace(discord_id=92),
                elo_field=1200,
            ),
        }

        class DiscordMember:
            @staticmethod
            def leaderboard(**_kwargs):
                return FakeQuery(global_row)

        class Player:
            @staticmethod
            def leaderboard(*, guild_id, **_kwargs):
                return FakeQuery(local_rows[guild_id])

        request = self.workers.ChampionRoleRequest(
            guild_ids=(100, 200),
            date_cutoff=datetime.datetime(2025, 8, 10),
        )
        main_thread = threading.get_ident()
        with mock.patch.object(
            self.workers,
            'models',
            SimpleNamespace(
                db=database,
                DiscordMember=DiscordMember,
                Player=Player,
            ),
        ):
            plan = await self.workers.run_load_champion_role_plan(request)

        self.assertIsNone(plan.global_champion_discord_id)
        self.assertEqual(
            plan.guilds,
            (
                self.workers.ChampionGuildTarget(100, None),
                self.workers.ChampionGuildTarget(200, 92),
            ),
        )
        self.assertEqual(database.opened, 1)
        self.assertEqual(database.closed, 1)
        self.assertNotEqual(database.thread_ids, [main_thread])

    async def test_slow_plan_load_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()
        expected = self.workers.ChampionRolePlan(None, ())

        def slow_load(_request):
            started.set()
            release.wait(timeout=2)
            return expected

        request = self.workers.ChampionRoleRequest(
            guild_ids=(100,),
            date_cutoff=datetime.datetime.now(),
        )
        with mock.patch.object(
            self.workers,
            'load_champion_role_plan',
            side_effect=slow_load,
        ):
            task = asyncio.create_task(
                self.workers.run_load_champion_role_plan(request)
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_later(0.01, heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
            release.set()
            self.assertEqual(await task, expected)

    async def test_cancellation_drains_worker_before_returning(self):
        started = threading.Event()
        release = threading.Event()

        def slow_load(_request):
            started.set()
            release.wait(timeout=2)
            return self.workers.ChampionRolePlan(None, ())

        request = self.workers.ChampionRoleRequest(
            guild_ids=(100,),
            date_cutoff=datetime.datetime.now(),
        )
        with mock.patch.object(
            self.workers,
            'load_champion_role_plan',
            side_effect=slow_load,
        ):
            task = asyncio.create_task(
                self.workers.run_load_champion_role_plan(request)
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_audit_is_transactional_and_worker_owned(self):
        database = FakeDatabase()
        logs = []

        class GameLog:
            @staticmethod
            def write(**kwargs):
                logs.append(kwargs)

        request = self.workers.ChampionAuditRequest(
            guild_id=100,
            messages=('one', 'two'),
        )
        main_thread = threading.get_ident()
        with mock.patch.object(
            self.workers,
            'models',
            SimpleNamespace(db=database, GameLog=GameLog),
        ):
            result = await self.workers.run_record_champion_role_audit(request)

        self.assertEqual(result.message, 'one\ntwo')
        self.assertEqual(logs, [{'guild_id': 100, 'message': 'one\ntwo'}])
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertNotEqual(database.thread_ids, [main_thread])

    async def test_audit_failure_rolls_back_and_closes_connection(self):
        database = FakeDatabase()

        class GameLog:
            @staticmethod
            def write(**_kwargs):
                raise peewee.OperationalError('audit failed')

        with mock.patch.object(
            self.workers,
            'models',
            SimpleNamespace(db=database, GameLog=GameLog),
        ):
            with self.assertRaisesRegex(
                peewee.OperationalError,
                'audit failed',
            ):
                await self.workers.run_record_champion_role_audit(
                    self.workers.ChampionAuditRequest(
                        guild_id=100,
                        messages=('completed effect',),
                    )
                )

        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.opened, 1)
        self.assertEqual(database.closed, 1)


class ChampionRoleServiceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.achievements = import_offline_runtime('modules.achievements')
        cls.games = import_offline_runtime('modules.games')

    def _plan(self, *, local=30, global_champion=None):
        workers = self.achievements.champion_role_workers
        return workers.ChampionRolePlan(
            global_champion_discord_id=global_champion,
            guilds=(workers.ChampionGuildTarget(100, local),),
        )

    async def test_concurrent_reconciliations_are_serialized(self):
        active = 0
        maximum_active = 0
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_reconciliation(*, bot=None):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            first_started.set()
            await release.wait()
            active -= 1
            return bot

        with mock.patch.object(
            self.achievements,
            '_set_champion_role',
            side_effect=slow_reconciliation,
        ):
            first = asyncio.create_task(
                self.achievements.set_champion_role(bot='first')
            )
            await first_started.wait()
            second = asyncio.create_task(
                self.achievements.set_champion_role(bot='second')
            )
            await asyncio.sleep(0.01)
            self.assertFalse(second.done())
            release.set()
            self.assertEqual(await first, 'first')
            self.assertEqual(await second, 'second')

        self.assertEqual(maximum_active, 1)

    async def test_partial_role_failure_continues_and_audits_successes(self):
        role = SimpleNamespace(name='ELO Champion')
        failed_old = FakeMember(10, 'Failed Old', remove_error=RuntimeError())
        removed_old = FakeMember(20, 'Removed Old')
        local = FakeMember(30, 'Local Winner')
        role.members = [failed_old, removed_old]
        guild = FakeGuild(100, role, (failed_old, removed_old, local))
        bot = FakeBot((guild,))
        audit = mock.AsyncMock(return_value=object())
        staff_log = mock.AsyncMock()

        with mock.patch.object(
            self.achievements.champion_role_workers,
            'run_load_champion_role_plan',
            new=mock.AsyncMock(return_value=self._plan()),
        ), mock.patch.object(
            self.achievements.champion_role_workers,
            'run_record_champion_role_audit',
            new=audit,
        ), mock.patch.object(
            self.achievements.utilities,
            'send_to_log_channel',
            new=staff_log,
        ), mock.patch.object(self.achievements.logger, 'exception'):
            result = await self.achievements.set_champion_role(bot=bot)

        outcome = result.guilds[0]
        self.assertEqual(outcome.succeeded_count, 2)
        self.assertEqual(outcome.failed_count, 1)
        self.assertFalse(outcome.converged)
        self.assertTrue(outcome.audit_recorded)
        self.assertEqual(len(local.additions), 1)
        request = audit.await_args.args[0]
        self.assertTrue(any('Removed Old' in m for m in request.messages))
        self.assertTrue(any('Local Winner' in m for m in request.messages))
        self.assertFalse(any('Failed Old' in m for m in request.messages))
        staff_log.assert_awaited_once_with(guild, '\n'.join(request.messages))
        self.assertTrue(result.plan_current)

    async def test_successful_role_with_failed_audit_is_reconciliation(self):
        role = SimpleNamespace(name='ELO Champion', members=[])
        local = FakeMember(30, 'Local Winner')
        guild = FakeGuild(100, role, (local,))
        bot = FakeBot((guild,))
        staff_log = mock.AsyncMock()

        with mock.patch.object(
            self.achievements.champion_role_workers,
            'run_load_champion_role_plan',
            new=mock.AsyncMock(return_value=self._plan()),
        ), mock.patch.object(
            self.achievements.champion_role_workers,
            'run_record_champion_role_audit',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError('audit unavailable')
            ),
        ), mock.patch.object(
            self.achievements.utilities,
            'send_to_log_channel',
            new=staff_log,
        ), mock.patch.object(self.achievements.logger, 'exception'):
            result = await self.achievements.set_champion_role(bot=bot)

        outcome = result.guilds[0]
        self.assertEqual(outcome.succeeded_count, 1)
        self.assertTrue(outcome.converged)
        self.assertFalse(outcome.audit_recorded)
        self.assertTrue(outcome.staff_log_sent)
        staff_log.assert_awaited_once()

    async def test_no_meaningful_champion_removes_stale_role(self):
        role = SimpleNamespace(name='ELO Champion')
        old = FakeMember(10, 'Old Champion')
        role.members = [old]
        guild = FakeGuild(100, role, (old,))
        bot = FakeBot((guild,))

        with mock.patch.object(
            self.achievements.champion_role_workers,
            'run_load_champion_role_plan',
            new=mock.AsyncMock(return_value=self._plan(local=None)),
        ), mock.patch.object(
            self.achievements.champion_role_workers,
            'run_record_champion_role_audit',
            new=mock.AsyncMock(return_value=object()),
        ), mock.patch.object(
            self.achievements.utilities,
            'send_to_log_channel',
            new=mock.AsyncMock(),
        ):
            result = await self.achievements.set_champion_role(bot=bot)

        self.assertEqual(result.guilds[0].succeeded_count, 1)
        self.assertTrue(result.guilds[0].converged)
        self.assertEqual(len(old.removals), 1)

    async def test_global_champion_is_applied_when_local_is_default(self):
        role = SimpleNamespace(name='ELO Champion', members=[])
        global_member = FakeMember(40, 'Global Winner')
        guild = FakeGuild(100, role, (global_member,))
        bot = FakeBot((guild,))

        class NoOrm:
            def __getattr__(self, name):
                raise AssertionError(f'publisher accessed ORM attribute {name}')

        with mock.patch.object(
            self.achievements.champion_role_workers,
            'run_load_champion_role_plan',
            new=mock.AsyncMock(return_value=self._plan(
                local=None,
                global_champion=40,
            )),
        ), mock.patch.object(
            self.achievements.champion_role_workers,
            'run_record_champion_role_audit',
            new=mock.AsyncMock(return_value=object()),
        ), mock.patch.object(
            self.achievements.utilities,
            'send_to_log_channel',
            new=mock.AsyncMock(),
        ), mock.patch.object(self.achievements, 'models', NoOrm()):
            result = await self.achievements.set_champion_role(bot=bot)

        self.assertEqual(result.guilds[0].succeeded_count, 1)
        self.assertTrue(result.guilds[0].converged)
        self.assertEqual(global_member.additions[0][1], 'Global champion')

    async def test_missing_planned_member_remains_reconciling(self):
        role = SimpleNamespace(name='ELO Champion', members=[])
        guild = FakeGuild(100, role, ())
        bot = FakeBot((guild,))

        with mock.patch.object(
            self.achievements.champion_role_workers,
            'run_load_champion_role_plan',
            new=mock.AsyncMock(return_value=self._plan(local=30)),
        ), mock.patch.object(
            self.achievements.champion_role_workers,
            'run_record_champion_role_audit',
            new=mock.AsyncMock(return_value=object()),
        ), mock.patch.object(
            self.achievements.utilities,
            'send_to_log_channel',
            new=mock.AsyncMock(),
        ), mock.patch.object(self.achievements.logger, 'warning'):
            result = await self.achievements.set_champion_role(bot=bot)

        outcome = result.guilds[0]
        self.assertEqual(outcome.succeeded_count, 0)
        self.assertEqual(outcome.failed_count, 1)
        self.assertFalse(outcome.converged)

    async def test_changed_post_effect_plan_is_explicitly_reconciling(self):
        role = SimpleNamespace(name='ELO Champion', members=[])
        local = FakeMember(30, 'Local Winner')
        guild = FakeGuild(100, role, (local,))
        bot = FakeBot((guild,))
        initial = self._plan(local=30)
        changed = self._plan(local=40)

        with mock.patch.object(
            self.achievements.champion_role_workers,
            'run_load_champion_role_plan',
            new=mock.AsyncMock(side_effect=[initial, changed]),
        ), mock.patch.object(
            self.achievements.champion_role_workers,
            'run_record_champion_role_audit',
            new=mock.AsyncMock(return_value=object()),
        ), mock.patch.object(
            self.achievements.utilities,
            'send_to_log_channel',
            new=mock.AsyncMock(),
        ), mock.patch.object(self.achievements.logger, 'warning'):
            result = await self.achievements.set_champion_role(bot=bot)

        self.assertFalse(result.plan_current)
        self.assertEqual(result.post_effect_plan, changed)

    async def test_later_scheduled_cycle_runs_after_failure(self):
        cog = SimpleNamespace()
        champion = mock.AsyncMock(side_effect=[
            peewee.OperationalError('first cycle failed'),
            'second cycle',
        ])
        with mock.patch.object(
            self.games.achievements,
            'set_champion_role',
            new=champion,
        ), mock.patch.object(self.games.logger, 'exception'):
            first = await self.games.polygames.run_champion_role_cycle(cog)
            second = await self.games.polygames.run_champion_role_cycle(cog)

        self.assertIsNone(first)
        self.assertEqual(second, 'second cycle')
        self.assertEqual(champion.await_count, 2)
