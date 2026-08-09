"""Focused member-join worker and Discord reconciliation coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.member_join_workers')
games = import_offline_runtime('modules.games')


def request():
    return workers.MemberJoinRequest(
        guild_id=300,
        member_id=20,
        discord_name='Target',
        discord_nick='Target Nick',
    )


def missing_side(*, notes=None):
    return workers.MissingSideChannel(
        game=workers.ChannelGame(
            id=101,
            guild_id=300,
            name='Test Game',
            notes=notes,
            host=workers.ChannelHost(name='Host'),
            league_season=None,
            league_tier=None,
            league_playoff=False,
        ),
        gameside_id=11,
        side_name='Alpha',
        team_name='The Team',
        players=(
            workers.ChannelPlayer(
                name='One',
                discord_member=workers.ChannelDiscordMember(20),
            ),
            workers.ChannelPlayer(
                name='Two',
                discord_member=workers.ChannelDiscordMember(21),
            ),
        ),
        preferred_guild_id=None,
        force_pcplus_guild=False,
    )


def result(*, registered=True, missing=()):
    return workers.MemberJoinResult(
        guild_id=300,
        member_id=20,
        registered=registered,
        local_player_created=False,
        player_id=7 if registered else None,
        side_channels=(workers.ChannelTarget(101, 801),) if registered else (),
        game_channels=(workers.ChannelTarget(101, 802),) if registered else (),
        missing_side_channels=tuple(missing),
    )


class WorkerTests(unittest.TestCase):
    def test_dtos_are_frozen_and_channel_snapshot_is_model_free(self):
        item = missing_side()
        with self.assertRaises(FrozenInstanceError):
            item.gameside_id = 2
        self.assertEqual(item.roster_names, 'Side **Alpha**: One, Two')
        self.assertEqual(item.game.is_season_game(), ())
        self.assertFalse(hasattr(item.game, '_meta'))

    def test_load_owns_connection_and_one_transaction(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        atomic = mock.MagicMock()
        atomic.__enter__.return_value = None
        atomic.__exit__.return_value = False
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=connection,
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=atomic,
        ), mock.patch.object(
            workers.models.Player,
            'get_by_discord_id',
            return_value=(SimpleNamespace(id=7), True),
        ) as lookup, mock.patch.object(
            workers,
            '_channel_target_rows',
            side_effect=(
                (workers.ChannelTarget(101, 801),),
                (workers.ChannelTarget(101, 802),),
            ),
        ), mock.patch.object(
            workers,
            '_missing_side_channels',
            return_value=(missing_side(),),
        ):
            loaded = workers.load_member_join(request())
        self.assertTrue(loaded.registered)
        self.assertTrue(loaded.local_player_created)
        self.assertEqual(loaded.player_id, 7)
        self.assertEqual(loaded.missing_side_channels[0].gameside_id, 11)
        lookup.assert_called_once_with(
            discord_id=20,
            discord_name='Target',
            discord_nick='Target Nick',
            guild_id=300,
        )
        connection.__enter__.assert_called_once()
        atomic.__enter__.assert_called_once()

    def test_unregistered_join_is_transactional_noop(self):
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.Player,
            'get_by_discord_id',
            return_value=(None, False),
        ), mock.patch.object(
            workers,
            '_channel_target_rows',
        ) as channel_rows, mock.patch.object(
            workers,
            '_missing_side_channels',
        ) as missing:
            loaded = workers.load_member_join(request())
        self.assertFalse(loaded.registered)
        channel_rows.assert_not_called()
        missing.assert_not_called()

    def test_persist_claims_null_side_and_records_external_guild(self):
        game = SimpleNamespace(id=101, guild_id=300, is_completed=False)
        side = SimpleNamespace(id=11, game_id=101)
        update = mock.MagicMock()
        update.where.return_value = update
        update.execute.return_value = 1
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.Game,
            'get_by_id',
            return_value=game,
        ), mock.patch.object(
            workers.models.GameSide,
            'get_by_id',
            return_value=side,
        ), mock.patch.object(
            workers.models.GameSide,
            'update',
            return_value=update,
        ) as update_call:
            workers.persist_side_channel(
                workers.PersistSideChannelRequest(101, 11, 900, 301)
            )
        self.assertEqual(
            update_call.call_args.kwargs['team_chan_external_server'],
            301,
        )

    def test_persist_conflict_is_rejected(self):
        game = SimpleNamespace(id=101, guild_id=300, is_completed=False)
        side = SimpleNamespace(id=11, game_id=101)
        update = mock.MagicMock()
        update.where.return_value = update
        update.execute.return_value = 0
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.Game,
            'get_by_id',
            return_value=game,
        ), mock.patch.object(
            workers.models.GameSide,
            'get_by_id',
            return_value=side,
        ), mock.patch.object(
            workers.models.GameSide,
            'update',
            return_value=update,
        ):
            with self.assertRaises(workers.MemberJoinConflictError):
                workers.persist_side_channel(
                    workers.PersistSideChannelRequest(101, 11, 900, 300)
                )

    def test_slow_load_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(workers, 'load_member_join', side_effect=slow):
                task = asyncio.create_task(workers.run_member_join(request()))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = not task.done()
                release.set()
                loaded = await task
            return responsive, loaded

        responsive, loaded = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertTrue(loaded.registered)

    def test_cancelled_load_waits_for_worker_before_propagating(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(workers, 'load_member_join', side_effect=slow):
                task = asyncio.create_task(workers.run_member_join(request()))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                await asyncio.sleep(0.005)
                still_draining = not task.done()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return still_draining

        self.assertTrue(asyncio.run(scenario()))

    def test_cancelled_channel_claim_surfaces_completed_worker_result(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()
            completed = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                completed.set()

            persist_request = workers.PersistSideChannelRequest(
                101, 11, 900, 300
            )
            with mock.patch.object(
                workers,
                'persist_side_channel',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_persist_side_channel(persist_request)
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                await asyncio.sleep(0.005)
                still_draining = not task.done()
                release.set()
                loaded = await task
            return still_draining, completed.is_set(), loaded

        still_draining, completed, loaded = asyncio.run(scenario())
        self.assertTrue(still_draining)
        self.assertTrue(completed)
        self.assertIsNone(loaded)


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def member(self):
        guild = SimpleNamespace(
            id=300,
            name='Guild',
            text_channels=[],
        )
        return SimpleNamespace(
            id=20,
            name='Target',
            nick='Target Nick',
            display_name='Target Nick',
            mention='<@20>',
            guild=guild,
        )

    async def test_existing_channel_effects_follow_worker_success(self):
        member = self.member()
        events = []
        side_channel = SimpleNamespace(
            id=801,
            name='side',
            guild=member.guild,
            send=mock.AsyncMock(side_effect=lambda *_: events.append('send-side')),
        )
        game_channel = SimpleNamespace(
            id=802,
            name='game',
            guild=member.guild,
            send=mock.AsyncMock(side_effect=lambda *_: events.append('send-game')),
        )
        bot = SimpleNamespace(
            guilds=[member.guild],
            get_channel=lambda channel_id: {
                801: side_channel,
                802: game_channel,
            }.get(channel_id),
        )
        cog = games.polygames.__new__(games.polygames)
        cog.bot = bot

        async def run(_request):
            events.append('worker')
            return result()

        async def add(channel, _member):
            events.append(f'add-{channel.id}')

        with mock.patch.object(
            games.member_join_workers,
            'run_member_join',
            side_effect=run,
        ), mock.patch.object(
            games.channels,
            'add_member_to_channel',
            side_effect=add,
        ):
            await cog.on_member_join(member)
        self.assertEqual(
            events,
            ['worker', 'add-801', 'send-side', 'add-802', 'send-game'],
        )

    async def test_database_failure_has_no_discord_effect(self):
        member = self.member()
        bot = SimpleNamespace(guilds=[member.guild], get_channel=mock.Mock())
        cog = games.polygames.__new__(games.polygames)
        cog.bot = bot
        with mock.patch.object(
            games.member_join_workers,
            'run_member_join',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
        ), mock.patch.object(
            games.channels,
            'add_member_to_channel',
            new=mock.AsyncMock(),
        ) as add, mock.patch.object(
            games.channels,
            'create_game_channel',
            new=mock.AsyncMock(),
        ) as create:
            await cog.on_member_join(member)
        bot.get_channel.assert_not_called()
        add.assert_not_awaited()
        create.assert_not_awaited()

    async def test_missing_side_creates_persists_then_greets(self):
        member = self.member()
        created = SimpleNamespace(id=900, delete=mock.AsyncMock())
        bot = SimpleNamespace(guilds=[member.guild], get_channel=lambda _id: None)
        cog = games.polygames.__new__(games.polygames)
        cog.bot = bot
        events = []

        async def create(*_args, **_kwargs):
            events.append('create')
            return created

        async def persist(_request):
            events.append('persist')

        async def greet(*_args, **_kwargs):
            events.append('greet')

        with mock.patch.object(
            games.member_join_workers,
            'run_member_join',
            new=mock.AsyncMock(return_value=result(missing=(missing_side(),))),
        ), mock.patch.object(
            games.channels,
            'add_member_to_channel',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.channels,
            'create_game_channel',
            side_effect=create,
        ) as create_mock, mock.patch.object(
            games.member_join_workers,
            'run_persist_side_channel',
            side_effect=persist,
        ) as persist_mock, mock.patch.object(
            games.channels,
            'greet_game_channel',
            side_effect=greet,
        ):
            await cog.on_member_join(member)
        self.assertEqual(events, ['create', 'persist', 'greet'])
        self.assertFalse(hasattr(create_mock.call_args.kwargs['game'], '_meta'))
        self.assertEqual(
            persist_mock.call_args.args[0].channel_guild_id,
            300,
        )
        created.delete.assert_not_awaited()

    async def test_persist_conflict_deletes_duplicate_without_greeting(self):
        member = self.member()
        created = SimpleNamespace(id=900, delete=mock.AsyncMock())
        bot = SimpleNamespace(guilds=[member.guild], get_channel=lambda _id: None)
        cog = games.polygames.__new__(games.polygames)
        cog.bot = bot
        with mock.patch.object(
            games.member_join_workers,
            'run_member_join',
            new=mock.AsyncMock(return_value=result(missing=(missing_side(),))),
        ), mock.patch.object(
            games.channels,
            'add_member_to_channel',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.channels,
            'create_game_channel',
            new=mock.AsyncMock(return_value=created),
        ), mock.patch.object(
            games.member_join_workers,
            'run_persist_side_channel',
            new=mock.AsyncMock(
                side_effect=workers.MemberJoinConflictError('race')
            ),
        ), mock.patch.object(
            games.channels,
            'greet_game_channel',
            new=mock.AsyncMock(),
        ) as greet:
            await cog.on_member_join(member)
        created.delete.assert_awaited_once()
        greet.assert_not_awaited()

    async def test_live_game_does_not_recreate_channel(self):
        member = self.member()
        bot = SimpleNamespace(guilds=[member.guild], get_channel=lambda _id: None)
        cog = games.polygames.__new__(games.polygames)
        cog.bot = bot
        with mock.patch.object(
            games.member_join_workers,
            'run_member_join',
            new=mock.AsyncMock(
                return_value=result(missing=(missing_side(notes='Live game'),))
            ),
        ), mock.patch.object(
            games.channels,
            'add_member_to_channel',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.channels,
            'create_game_channel',
            new=mock.AsyncMock(),
        ) as create:
            await cog.on_member_join(member)
        create.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
