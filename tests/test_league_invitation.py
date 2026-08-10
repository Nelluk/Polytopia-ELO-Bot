"""Focused offline coverage for P8.27 league invitation separation."""

import asyncio
from contextlib import AbstractContextManager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import datetime
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_invitation_workers')
service = import_offline_runtime('modules.league_invitation')
league = import_offline_runtime('modules.league')


NOW = datetime.datetime(2026, 8, 9, 12, 0, 0)


def eligibility_request(**overrides):
    values = dict(
        as_of=NOW,
        polychampions_guild_id=20,
        global_guild_ids=(10, 11),
        era_start=datetime.date(2020, 12, 1),
        era_end=datetime.date.max,
        after_member_id=None,
        limit=workers.MAX_INVITATION_SCAN,
    )
    values.update(overrides)
    return workers.LeagueInvitationEligibilityRequest(**values)


def candidate(
    member_id,
    *,
    discord_id=None,
    name=None,
    elo=1200,
    polytopia_id='code',
    polytopia_name='name',
):
    return SimpleNamespace(
        id=member_id,
        discord_id=discord_id or 1000 + member_id,
        name=name or f'Player {member_id}',
        elo_max_moonrise=elo,
        polytopia_id=polytopia_id,
        polytopia_name=polytopia_name,
    )


def evaluation(
    member_id,
    *,
    eligible=True,
    reason='eligible_high_elo',
):
    return workers.LeagueInvitationEvaluation(
        member_id=member_id,
        discord_id=1000 + member_id,
        name=f'Player {member_id}',
        wins=5,
        losses=4,
        recent_games=1,
        elo_max_moonrise=1200,
        eligible=eligible,
        reason=reason,
    )


class ConnectionDatabase:
    def __init__(self):
        self.events = []

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.events.append('connection-open')

            def __exit__(self, exc_type, exc_value, traceback):
                database.events.append('connection-close')
                return False

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                database.events.append('atomic-open')

            def __exit__(self, exc_type, exc_value, traceback):
                database.events.append(
                    'commit' if exc_type is None else 'rollback'
                )
                return False

        return Atomic()


class LeagueInvitationWorkerTests(unittest.TestCase):
    def test_requests_results_and_nested_evaluations_are_frozen(self):
        request = eligibility_request()
        with self.assertRaises(FrozenInstanceError):
            request.limit = 3
        row = evaluation(1)
        with self.assertRaises(FrozenInstanceError):
            row.name = 'Changed'
        batch = workers.LeagueInvitationBatch((row,), 1, False, None)
        with self.assertRaises(FrozenInstanceError):
            batch.truncated = True
        self.assertEqual(batch.eligible, (row,))

    def test_legacy_qualification_thresholds_and_identity_order_are_preserved(self):
        cases = (
            (candidate(1), 4, 0, 1, False, 'insufficient_wins'),
            (candidate(2), 5, 9, 0, False, 'no_recent_games'),
            (candidate(3, elo=1151), 5, 99, 1, True, 'eligible_high_elo'),
            (candidate(4, elo=1150), 6, 5, 1, True, 'eligible_positive_record'),
            (candidate(5, elo=1150), 5, 5, 1, False, 'insufficient_elo_or_record'),
            (
                candidate(6, polytopia_id=None, polytopia_name=None),
                6,
                5,
                1,
                False,
                'missing_polytopia_identity',
            ),
        )
        for member, wins, losses, recent, eligible, reason in cases:
            with self.subTest(member=member.id):
                result = workers._evaluate(
                    member,
                    wins=wins,
                    losses=losses,
                    recent_games=recent,
                )
                self.assertEqual(result.eligible, eligible)
                self.assertEqual(result.reason, reason)
        self.assertEqual(workers.MINIMUM_LIFETIME_MAX_ELO, 1075)
        self.assertEqual(workers.MINIMUM_WINS, 5)
        self.assertEqual(workers.RECENT_ACTIVITY_DAYS, 15)
        self.assertEqual(workers.HIGH_ELO_QUALIFICATION, 1150)

    def test_bounded_read_owns_connection_batches_counts_and_returns_cursor(self):
        database = ConnectionDatabase()
        candidates = (candidate(1), candidate(2), candidate(3))
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers,
            '_candidate_members',
            return_value=candidates,
        ) as load, mock.patch.object(
            workers,
            '_record_counts',
            return_value={1: (5, 4), 2: (3, 1)},
        ) as records, mock.patch.object(
            workers,
            '_recent_counts',
            return_value={1: 1, 2: 1},
        ) as recent:
            result = workers.load_invitation_eligibility(
                eligibility_request(limit=2, after_member_id=80)
            )

        self.assertEqual(database.events, ['connection-open', 'connection-close'])
        self.assertEqual(result.scanned_count, 2)
        self.assertTrue(result.truncated)
        self.assertEqual(result.next_after_member_id, 2)
        self.assertEqual([row.member_id for row in result.evaluations], [1, 2])
        self.assertEqual([row.member_id for row in result.eligible], [1])
        load.assert_called_once()
        records.assert_called_once_with((1, 2), mock.ANY)
        recent.assert_called_once_with((1, 2), mock.ANY)

    def test_scan_limit_and_polychampions_identity_fail_closed(self):
        with self.assertRaisesRegex(workers.LeagueInvitationValidationError, 'between 1'):
            workers.load_invitation_eligibility(eligibility_request(limit=0))
        with self.assertRaisesRegex(workers.LeagueInvitationValidationError, 'guild ID'):
            workers.load_invitation_eligibility(
                eligibility_request(polychampions_guild_id=0)
            )

    def test_delivery_write_is_atomic_idempotent_and_identity_bound(self):
        database = ConnectionDatabase()
        request = workers.LeagueInvitationDeliveryRequest(
            member_id=1,
            discord_id=1001,
            sent_on=NOW.date(),
        )
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers,
            '_update_delivery',
            return_value=1,
        ), mock.patch.object(workers, '_delivery_member') as reload_member:
            first = workers.record_invitation_delivery(request)
        self.assertTrue(first.recorded)
        reload_member.assert_not_called()
        self.assertEqual(
            database.events,
            ['connection-open', 'atomic-open', 'commit', 'connection-close'],
        )

        database.events.clear()
        existing = SimpleNamespace(date_polychamps_invite_sent=NOW.date())
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers,
            '_update_delivery',
            return_value=0,
        ), mock.patch.object(
            workers,
            '_delivery_member',
            return_value=existing,
        ):
            repeated = workers.record_invitation_delivery(request)
        self.assertFalse(repeated.recorded)
        self.assertEqual(database.events[-2:], ['commit', 'connection-close'])

    def test_delivery_conflict_rolls_back_and_closes_connection(self):
        database = ConnectionDatabase()
        request = workers.LeagueInvitationDeliveryRequest(1, 1001, NOW.date())
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers,
            '_update_delivery',
            return_value=0,
        ), mock.patch.object(workers, '_delivery_member', return_value=None):
            with self.assertRaises(workers.LeagueInvitationConflictError):
                workers.record_invitation_delivery(request)
        self.assertEqual(database.events[-2:], ['rollback', 'connection-close'])

    def test_executor_keeps_loop_responsive_and_drains_cancellation(self):
        async def check():
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()

            def blocked(_request):
                started.set()
                release.wait(2)
                finished.set()
                return workers.LeagueInvitationBatch((), 0, False, None)

            executor = ThreadPoolExecutor(max_workers=1)
            task = None
            try:
                with mock.patch.object(
                    workers,
                    '_invitation_executor',
                    executor,
                ), mock.patch.object(
                    workers,
                    'load_invitation_eligibility',
                    side_effect=blocked,
                ):
                    task = asyncio.create_task(
                        workers.run_load_invitation_eligibility(
                            eligibility_request()
                        )
                    )
                    for _ in range(500):
                        if started.is_set():
                            break
                        await asyncio.sleep(0.001)
                    self.assertTrue(started.is_set())
                    tick = time.monotonic()
                    await asyncio.sleep(0.02)
                    self.assertLess(time.monotonic() - tick, 0.2)
                    task.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                executor.shutdown(wait=True)
            self.assertTrue(finished.is_set())

        asyncio.run(check())


class LeagueInvitationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild_id = int(service.settings.server_ids['main'])

    async def test_missing_main_guild_stops_before_database_read(self):
        bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        with mock.patch.object(
            service.workers,
            'run_load_invitation_eligibility',
            new=mock.AsyncMock(),
        ) as load:
            result = await service.run_invitation_cycle(bot=bot, as_of=NOW)
        load.assert_not_awaited()
        self.assertEqual(result.scanned_count, 0)

    async def test_cycle_sends_only_eligible_members_and_persists_after_dm(self):
        first = evaluation(1)
        ineligible = evaluation(2, eligible=False, reason='insufficient_wins')
        missing = evaluation(3)
        first_member = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=self.guild_id,
            get_member=lambda discord_id: (
                first_member if discord_id == first.discord_id else None
            ),
        )
        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == self.guild_id else None
        )
        batch = workers.LeagueInvitationBatch(
            (first, ineligible, missing),
            3,
            False,
            None,
        )
        persisted = workers.LeagueInvitationDeliveryResult(
            first.member_id,
            first.discord_id,
            NOW.date(),
            True,
        )
        with mock.patch.object(
            service.workers,
            'run_load_invitation_eligibility',
            new=mock.AsyncMock(return_value=batch),
        ), mock.patch.object(
            service.workers,
            'run_record_invitation_delivery',
            new=mock.AsyncMock(return_value=persisted),
        ) as record:
            result = await service.run_invitation_cycle(bot=bot, as_of=NOW)

        first_member.send.assert_awaited_once_with(service.INVITATION_MESSAGE)
        record.assert_awaited_once()
        self.assertEqual(record.await_args.args[0].discord_id, first.discord_id)
        self.assertEqual(result.eligible_count, 2)
        self.assertEqual(result.delivered_count, 1)
        self.assertEqual(result.missing_member_count, 1)

    async def test_dm_and_persistence_failures_are_isolated_without_false_sent_state(self):
        dm_failure = evaluation(1)
        persistence_failure = evaluation(2)
        rejected = SimpleNamespace(
            send=mock.AsyncMock(side_effect=discord.DiscordException('denied'))
        )
        delivered = SimpleNamespace(send=mock.AsyncMock())
        members = {
            dm_failure.discord_id: rejected,
            persistence_failure.discord_id: delivered,
        }
        guild = SimpleNamespace(id=self.guild_id, get_member=members.get)
        bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
        batch = workers.LeagueInvitationBatch(
            (dm_failure, persistence_failure),
            2,
            False,
            None,
        )
        with mock.patch.object(
            service.workers,
            'run_load_invitation_eligibility',
            new=mock.AsyncMock(return_value=batch),
        ), mock.patch.object(
            service.workers,
            'run_record_invitation_delivery',
            new=mock.AsyncMock(side_effect=RuntimeError('database down')),
        ) as record:
            result = await service.run_invitation_cycle(bot=bot, as_of=NOW)
        self.assertEqual(result.discord_failure_count, 1)
        self.assertEqual(result.persistence_failure_count, 1)
        self.assertEqual(result.delivered_count, 0)
        record.assert_awaited_once()

    async def test_task_delegates_and_advances_cursor_only_after_success(self):
        bot = SimpleNamespace(wait_until_ready=mock.AsyncMock())
        cog = league.league.__new__(league.league)
        cog.bot = bot
        cog._polychamps_invite_cursor = 70
        result = service.LeagueInvitationCycleResult(
            2, 1, 1, 0, 0, 0, 0, True, 90
        )
        with mock.patch.object(
            league.league_invitation,
            'run_invitation_cycle',
            new=mock.AsyncMock(return_value=result),
        ) as run:
            await league.league.task_send_polychamps_invite.coro(cog)
        bot.wait_until_ready.assert_awaited_once()
        run.assert_awaited_once_with(bot=bot, after_member_id=70)
        self.assertEqual(cog._polychamps_invite_cursor, 90)

        with mock.patch.object(
            league.league_invitation,
            'run_invitation_cycle',
            new=mock.AsyncMock(side_effect=RuntimeError('database down')),
        ):
            await league.league.task_send_polychamps_invite.coro(cog)
        self.assertEqual(cog._polychamps_invite_cursor, 90)

    def test_development_profile_keeps_background_task_disabled(self):
        self.assertFalse(service.settings.run_tasks)


if __name__ == '__main__':
    unittest.main()
