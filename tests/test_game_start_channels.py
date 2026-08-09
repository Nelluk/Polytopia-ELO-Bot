"""Focused P5.19a started-game channel boundary coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_start_channel_workers')
creation = import_offline_runtime('modules.game_start_channels')
start_adapter = import_offline_runtime('modules.game_start')
start_workers = import_offline_runtime('modules.game_start_workers')


def player(player_id, name, *, external_server=None):
    team = (
        SimpleNamespace(name='The Team', external_server=external_server)
        if external_server is not None else None
    )
    return SimpleNamespace(
        id=player_id,
        name=name,
        team=team,
        discord_member=SimpleNamespace(discord_id=player_id + 1000),
    )


def side(side_id, position, players, *, name='Alpha', channel=None):
    value = SimpleNamespace(
        id=side_id,
        position=position,
        sidename=name,
        team=SimpleNamespace(name='The Team'),
        team_chan=channel,
    )
    value.ordered_player_list = lambda: tuple(
        SimpleNamespace(id=100 + index, player=item)
        for index, item in enumerate(players)
    )
    value.name = lambda: name
    return value


def game_graph(*, notes='Notes', game_channel=None):
    first = side(
        11,
        1,
        (player(1, 'One', external_server=700),
         player(2, 'Two', external_server=700)),
        name='Alpha',
    )
    second = side(
        12,
        2,
        (player(3, 'Three'), player(4, 'Four')),
        name='Bravo',
    )
    game = SimpleNamespace(
        id=42,
        guild_id=300,
        name='Fields of Fire',
        notes=notes,
        host=SimpleNamespace(name='Host'),
        league_season=None,
        league_tier=None,
        league_playoff=False,
        game_chan=game_channel,
    )
    game.ordered_side_list = lambda: (first, second)
    return game


def frozen_plan(*, preferred_guild_id=None):
    members = (
        workers.ChannelPlayer('One', workers.ChannelDiscordMember(1001)),
        workers.ChannelPlayer('Two', workers.ChannelDiscordMember(1002)),
    )
    return workers.StartedGameChannelPlan(
        game=workers.ChannelGame(
            id=42,
            guild_id=300,
            name='Fields of Fire',
            notes='Notes',
            host=workers.ChannelHost('Host'),
            league_season=None,
            league_tier=None,
            league_playoff=False,
        ),
        roster_names='Side **Alpha**: One, Two',
        side_targets=(
            workers.StartedChannelTarget(
                kind='side',
                side_id=11,
                side_name='Alpha',
                team_name='The Team',
                players=members,
                preferred_guild_id=preferred_guild_id,
            ),
        ),
        central_target=None,
    )


class StartedChannelPlanTests(unittest.TestCase):
    def test_plan_is_frozen_primitive_and_preserves_external_route(self):
        plan = workers.freeze_started_channel_plan(game_graph())

        self.assertEqual(plan.game.id, 42)
        self.assertEqual(len(plan.side_targets), 2)
        self.assertEqual(plan.side_targets[0].preferred_guild_id, 700)
        self.assertIsNone(plan.side_targets[1].preferred_guild_id)
        self.assertIn('Side **Alpha**: One, Two', plan.roster_names)
        self.assertFalse(hasattr(plan.game, '_meta'))
        self.assertFalse(hasattr(plan.side_targets[0].players[0], '_meta'))
        with self.assertRaises(FrozenInstanceError):
            plan.game.name = 'Changed'

    def test_plan_skips_live_and_existing_channel_targets(self):
        self.assertIsNone(
            workers.freeze_started_channel_plan(game_graph(notes='LIVE event'))
        )
        game = game_graph(game_channel=999)
        game.ordered_side_list()[0].team_chan = 801
        plan = workers.freeze_started_channel_plan(game)
        self.assertEqual(
            tuple(target.side_id for target in plan.side_targets),
            (12,),
        )


class StartedChannelPersistenceTests(unittest.TestCase):
    def test_side_claim_owns_connection_transaction_and_external_reference(self):
        game = SimpleNamespace(
            id=42,
            guild_id=300,
            is_pending=False,
            is_completed=False,
        )
        side = SimpleNamespace(
            id=11,
            game_id=42,
            team_chan=None,
            team_chan_external_server=None,
        )
        update = mock.MagicMock()
        update.where.return_value = update
        update.execute.return_value = 1
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
            result = workers.persist_started_channel(
                workers.PersistStartedChannelRequest(
                    game_id=42,
                    guild_id=300,
                    channel_id=900,
                    channel_guild_id=700,
                    kind='side',
                    side_id=11,
                )
            )

        self.assertFalse(result.already_persisted)
        self.assertEqual(
            update_call.call_args.kwargs['team_chan_external_server'],
            700,
        )
        connection.__enter__.assert_called_once()
        atomic.__enter__.assert_called_once()

    def test_stale_claim_is_rejected_inside_transaction(self):
        game = SimpleNamespace(
            id=42,
            guild_id=300,
            is_pending=False,
            is_completed=False,
        )
        side = SimpleNamespace(
            id=11,
            game_id=42,
            team_chan=None,
            team_chan_external_server=None,
        )
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
            with self.assertRaises(workers.StartedChannelConflictError):
                workers.persist_started_channel(
                    workers.PersistStartedChannelRequest(
                        42, 300, 900, 300, 'side', 11
                    )
                )


class StartedChannelDiscordTests(unittest.IsolatedAsyncioTestCase):
    def guild(self, guild_id, *, count=0):
        return SimpleNamespace(
            id=guild_id,
            name=f'Guild {guild_id}',
            text_channels=tuple(SimpleNamespace() for _ in range(count)),
        )

    async def test_create_persist_greet_order_and_external_route(self):
        source = self.guild(300)
        external = self.guild(700)
        channel = SimpleNamespace(
            id=900,
            guild=external,
            delete=mock.AsyncMock(),
        )
        events = []

        async def create(guild, **kwargs):
            events.append(('create', guild.id, kwargs['using_team_server_flag']))
            return channel

        async def persist(request):
            events.append(('persist', request.channel_guild_id))
            return SimpleNamespace(already_persisted=False)

        async def greet(*args, **kwargs):
            events.append(('greet', args[0].id, kwargs['full_game']))

        with mock.patch.object(
            creation.channels,
            'create_game_channel',
            side_effect=create,
        ), mock.patch.object(
            creation.workers,
            'run_persist_started_channel',
            side_effect=persist,
        ), mock.patch.object(
            creation.channels,
            'greet_game_channel',
            side_effect=greet,
        ):
            result = await creation.create_started_game_channels(
                plan=frozen_plan(preferred_guild_id=700),
                source_guild=source,
                bot_guilds=(source, external),
            )

        self.assertEqual(
            events,
            [('create', 700, True), ('persist', 700), ('greet', 700, False)],
        )
        self.assertEqual(result.warnings, ())
        channel.delete.assert_not_awaited()

    async def test_unclaimed_channel_is_deleted_and_never_greeted(self):
        source = self.guild(300)
        channel = SimpleNamespace(
            id=900,
            guild=source,
            delete=mock.AsyncMock(),
        )
        with mock.patch.object(
            creation.channels,
            'create_game_channel',
            new=mock.AsyncMock(return_value=channel),
        ), mock.patch.object(
            creation.workers,
            'run_persist_started_channel',
            new=mock.AsyncMock(
                side_effect=workers.StartedChannelConflictError('race')
            ),
        ), mock.patch.object(
            creation.channels,
            'greet_game_channel',
            new=mock.AsyncMock(),
        ) as greet:
            result = await creation.create_started_game_channels(
                plan=frozen_plan(),
                source_guild=source,
                bot_guilds=(source,),
            )

        channel.delete.assert_awaited_once()
        greet.assert_not_awaited()
        self.assertIn('was not claimed', result.warnings[0])
        self.assertIn('was removed', result.warnings[0])

    async def test_cancellation_drains_claim_and_greeting(self):
        source = self.guild(300)
        channel = SimpleNamespace(
            id=900,
            guild=source,
            delete=mock.AsyncMock(),
        )
        started = asyncio.Event()
        release = asyncio.Event()
        greeted = asyncio.Event()

        async def persist(_request):
            started.set()
            await release.wait()

        async def greet(*_args, **_kwargs):
            greeted.set()

        with mock.patch.object(
            creation.channels,
            'create_game_channel',
            new=mock.AsyncMock(return_value=channel),
        ), mock.patch.object(
            creation.workers,
            'run_persist_started_channel',
            side_effect=persist,
        ), mock.patch.object(
            creation.channels,
            'greet_game_channel',
            side_effect=greet,
        ):
            task = asyncio.create_task(
                creation.create_started_game_channels(
                    plan=frozen_plan(),
                    source_guild=source,
                    bot_guilds=(source,),
                )
            )
            await started.wait()
            task.cancel()
            await asyncio.sleep(0.005)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(greeted.is_set())
        channel.delete.assert_not_awaited()

    async def test_capacity_skip_occurs_without_discord_creation(self):
        source = self.guild(300, count=461)
        with mock.patch.object(
            creation.channels,
            'create_game_channel',
            new=mock.AsyncMock(),
        ) as create:
            result = await creation.create_started_game_channels(
                plan=frozen_plan(),
                source_guild=source,
                bot_guilds=(source,),
            )
        create.assert_not_awaited()
        self.assertIn('461/500', result.warnings[0])


class StartedChannelPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_reload_failure_does_not_suppress_frozen_channels(self):
        sent = []

        async def send(content=None, **_kwargs):
            sent.append(content)

        plan = frozen_plan()
        result = start_workers.StartResult(
            game_id=42,
            guild_id=300,
            name='Fields of Fire',
            requester_id=1001,
            mentions=('<@1001>', '<@1002>'),
            participant_ids=(1001, 1002),
            missing_member_warnings=(),
            name_warning=None,
            league_warning=None,
            creator_id=1001,
            host_id=1001,
            channel_plan=plan,
        )
        channel_runner = mock.AsyncMock(
            return_value=creation.StartedChannelCreationResult(42, ())
        )
        guild = SimpleNamespace(id=300)
        with mock.patch.object(
            start_adapter.models.Game,
            'load_full_game',
            side_effect=RuntimeError('reload failed'),
        ), mock.patch.object(
            start_adapter.settings,
            'guild_setting',
            side_effect=lambda _guild_id, key: (
                True if key == 'game_channel_categories' else None
            ),
        ), mock.patch.object(
            start_adapter.game_start_channels,
            'create_started_game_channels',
            new=channel_runner,
        ):
            await start_adapter.publish_start_result(
                result,
                output_context=SimpleNamespace(send=send),
                guild=guild,
                prefix='$',
                bot_guilds=(guild,),
            )

        channel_runner.assert_awaited_once_with(
            plan=plan,
            source_guild=guild,
            bot_guilds=(guild,),
        )
        self.assertTrue(any('now being tracked' in str(item) for item in sent))


if __name__ == '__main__':
    unittest.main()
