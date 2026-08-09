"""Focused P5.16 external-broadcast creation lifecycle tests."""

from contextlib import nullcontext
from types import SimpleNamespace
import asyncio
import unittest
from unittest import mock

from modules import game_broadcast_creation as creation
from modules import game_broadcast_creation_workers as workers
from modules import game_open
from modules import game_open_workers


def role(role_id, role_name):
    return workers.BroadcastRoleSnapshot(role_id, role_name)


def plan(server_id=700, label='Team Jets'):
    return workers.BroadcastDestinationPlan(
        external_server_id=server_id,
        scopes=(label,),
        content_with_join=f'join {server_id}',
        content_without_join=f'no reaction {server_id}',
    )


def request(*, game_id=42):
    return creation.ExternalBroadcastCreationRequest(
        game_id=game_id,
        guild_id=300,
        jump_url='https://discord.test/channels/300/301/302',
        role_locks=(role(501, 'The Jets'),),
        channel_name='beta-bot-tests',
    )


class BroadcastCreationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.game = SimpleNamespace(
            id=42,
            guild_id=300,
            is_pending=True,
            notes='A note',
            host=SimpleNamespace(name='Host'),
            size_string=lambda: '1v1',
            get_headline=lambda: 'Ranked game',
            is_uncaught_season_game=lambda: False,
            reaction_join_string=lambda: 'Join game 42 by reacting with ⚔️',
        )

    def test_plan_deduplicates_destinations_and_combines_stable_scopes(self):
        resolved = {
            'The Jets': ((700, 'Team Jets'), None),
            'The Ronin': ((700, 'Team Ronin'), None),
            'Nova': ((701, 'House Nova'), None),
        }
        request_value = workers.BroadcastPlanRequest(
            game_id=42,
            guild_id=300,
            jump_url='https://discord.test/jump',
            role_locks=(
                role(1, 'The Jets'),
                role(2, 'The Ronin'),
                role(1, 'The Jets'),
                role(3, 'Nova'),
            ),
        )
        with mock.patch.dict(
            workers.settings.server_ids,
            {'polychampions': 300, 'test': 301},
        ), mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_load_game',
            return_value=(self.game, workers.READY),
        ), mock.patch.object(
            workers,
            '_resolve_role_scope',
            side_effect=lambda **kwargs: resolved[kwargs['role'].role_name],
        ):
            result = workers.build_broadcast_plan(request_value)

        self.assertEqual(
            [destination.external_server_id for destination in result.destinations],
            [700, 701],
        )
        self.assertEqual(
            result.destinations[0].scopes,
            ('Team Jets', 'Team Ronin'),
        )
        self.assertIn('Team Jets / Team Ronin', result.destinations[0].content_with_join)
        self.assertIn('Join game 42', result.destinations[0].content_with_join)
        self.assertIn('Missing add reactions', result.destinations[0].content_without_join)

    def test_ambiguous_house_route_is_skipped_with_exact_warning(self):
        house = SimpleNamespace(id=9)
        with mock.patch.object(
            workers.models.Team,
            'get_or_none',
            return_value=None,
        ), mock.patch.object(
            workers.models.House,
            'get_or_none',
            return_value=house,
        ), mock.patch.object(
            workers,
            '_house_external_server_ids',
            return_value=(700, 701),
        ):
            resolved, warning = workers._resolve_role_scope(
                guild_id=300,
                role=role(3, 'Nova'),
            )

        self.assertIsNone(resolved)
        self.assertIn('2 distinct', warning)
        self.assertIn('700, 701', warning)

    def test_persistence_revalidates_and_creates_exact_tracking_row(self):
        request_value = workers.BroadcastTargetRequest(
            game_id=42,
            guild_id=300,
            channel_id=800,
            message_id=900,
        )
        row = SimpleNamespace(id=10)
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_load_game',
            return_value=(self.game, workers.READY),
        ), mock.patch.object(
            workers,
            '_existing_row',
            return_value=None,
        ), mock.patch.object(
            workers.models.TeamServerBroadcastMessage,
            'create',
            return_value=row,
        ) as create_row:
            result = workers.persist_broadcast_target(request_value)

        self.assertEqual(result.status, workers.TRACKED)
        create_row.assert_called_once_with(
            game=42,
            channel_id=800,
            message_id=900,
        )

    def test_stale_post_send_state_never_creates_tracking_row(self):
        request_value = workers.BroadcastTargetRequest(
            game_id=42,
            guild_id=300,
            channel_id=800,
            message_id=900,
        )
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_load_game',
            return_value=(None, workers.STALE),
        ), mock.patch.object(
            workers.models.TeamServerBroadcastMessage,
            'create',
        ) as create_row:
            result = workers.persist_broadcast_target(request_value)

        self.assertEqual(result.status, workers.STALE)
        create_row.assert_not_called()


class BroadcastCreationDiscordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        creation._active_games.clear()

    def make_channel(self, *, channel_id, message=None, send_error=None):
        if message is None:
            message = SimpleNamespace(
                id=channel_id + 1000,
                delete=mock.AsyncMock(),
            )
        send = mock.AsyncMock(
            side_effect=send_error,
            return_value=message,
        )
        channel = SimpleNamespace(
            id=channel_id,
            name='beta-bot-tests',
            send=send,
            permissions_for=lambda member: SimpleNamespace(
                add_reactions=True
            ),
        )
        return channel, message

    def make_bot(self, channels):
        guilds = {
            server_id: SimpleNamespace(
                id=server_id,
                me=SimpleNamespace(id=999),
                text_channels=(channel,),
                get_member=lambda member_id: SimpleNamespace(id=member_id),
            )
            for server_id, channel in channels.items()
        }
        return SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: guilds.get(guild_id),
        )

    async def run_case(self, *, destinations, bot, persist=None):
        plan_result = workers.BroadcastPlanResult(
            game_id=42,
            guild_id=300,
            status=workers.READY,
            destinations=tuple(destinations),
            warnings=(),
        )
        preflight = workers.BroadcastTargetResult(
            status=workers.READY,
            game_id=42,
            guild_id=300,
            channel_id=800,
            message_id=None,
        )
        if persist is None:
            persist = workers.BroadcastTargetResult(
                status=workers.TRACKED,
                game_id=42,
                guild_id=300,
                channel_id=800,
                message_id=1800,
                row_id=10,
            )
        with mock.patch.object(
            workers,
            'run_build_broadcast_plan',
            new=mock.AsyncMock(return_value=plan_result),
        ), mock.patch.object(
            workers,
            'run_preflight_broadcast_target',
            new=mock.AsyncMock(return_value=preflight),
        ), mock.patch.object(
            workers,
            'run_persist_broadcast_target',
            new=mock.AsyncMock(
                side_effect=persist if isinstance(persist, Exception) else None,
                return_value=None if isinstance(persist, Exception) else persist,
            ),
        ):
            return await creation.create_external_broadcasts(
                bot=bot,
                request=request(),
            )

    async def test_successful_send_is_tracked_without_compensation(self):
        channel, message = self.make_channel(channel_id=800)
        result = await self.run_case(
            destinations=(plan(),),
            bot=self.make_bot({700: channel}),
        )

        self.assertEqual(result.outcomes[0].status, creation.TRACKED)
        channel.send.assert_awaited_once_with('join 700')
        message.delete.assert_not_awaited()
        self.assertEqual(result.warnings, ())

    async def test_persistence_failure_deletes_concrete_message(self):
        channel, message = self.make_channel(channel_id=800)
        result = await self.run_case(
            destinations=(plan(),),
            bot=self.make_bot({700: channel}),
            persist=RuntimeError('database unavailable'),
        )

        self.assertEqual(result.outcomes[0].status, creation.COMPENSATED)
        message.delete.assert_awaited_once()
        self.assertIn('was deleted', result.warnings[0])

    async def test_failed_compensation_reports_exact_orphan_target(self):
        message = SimpleNamespace(
            id=1800,
            delete=mock.AsyncMock(side_effect=RuntimeError('forbidden')),
        )
        channel, _ = self.make_channel(channel_id=800, message=message)
        result = await self.run_case(
            destinations=(plan(),),
            bot=self.make_bot({700: channel}),
            persist=RuntimeError('database unavailable'),
        )

        self.assertEqual(result.outcomes[0].status, creation.ORPHANED)
        warning = result.warnings[0]
        self.assertIn('channel 800', warning)
        self.assertIn('message 1800', warning)

    async def test_ambiguous_send_is_not_retried_and_later_target_continues(self):
        first, _ = self.make_channel(
            channel_id=800,
            send_error=RuntimeError('timeout'),
        )
        second, second_message = self.make_channel(channel_id=801)
        bot = self.make_bot({700: first, 701: second})
        persisted = workers.BroadcastTargetResult(
            status=workers.TRACKED,
            game_id=42,
            guild_id=300,
            channel_id=801,
            message_id=1801,
            row_id=11,
        )
        result = await self.run_case(
            destinations=(plan(700), plan(701)),
            bot=bot,
            persist=persisted,
        )

        self.assertEqual(first.send.await_count, 1)
        second.send.assert_awaited_once()
        second_message.delete.assert_not_awaited()
        self.assertEqual(
            [outcome.status for outcome in result.outcomes],
            [creation.UNCERTAIN, creation.TRACKED],
        )

    async def test_stale_post_send_result_compensates_message(self):
        channel, message = self.make_channel(channel_id=800)
        stale = workers.BroadcastTargetResult(
            status=workers.STALE,
            game_id=42,
            guild_id=300,
            channel_id=800,
            message_id=1800,
        )
        result = await self.run_case(
            destinations=(plan(),),
            bot=self.make_bot({700: channel}),
            persist=stale,
        )

        self.assertEqual(result.outcomes[0].status, creation.COMPENSATED)
        message.delete.assert_awaited_once()

    async def test_cancellation_drains_tracking_after_concrete_send(self):
        channel, message = self.make_channel(channel_id=800)
        bot = self.make_bot({700: channel})
        plan_result = workers.BroadcastPlanResult(
            game_id=42,
            guild_id=300,
            status=workers.READY,
            destinations=(plan(),),
            warnings=(),
        )
        preflight = workers.BroadcastTargetResult(
            status=workers.READY,
            game_id=42,
            guild_id=300,
            channel_id=800,
            message_id=None,
        )
        persist_started = asyncio.Event()
        release_persist = asyncio.Event()

        async def persist_target(target):
            persist_started.set()
            await release_persist.wait()
            return workers.BroadcastTargetResult(
                status=workers.TRACKED,
                game_id=42,
                guild_id=300,
                channel_id=800,
                message_id=message.id,
                row_id=10,
            )

        with mock.patch.object(
            workers,
            'run_build_broadcast_plan',
            new=mock.AsyncMock(return_value=plan_result),
        ), mock.patch.object(
            workers,
            'run_preflight_broadcast_target',
            new=mock.AsyncMock(return_value=preflight),
        ), mock.patch.object(
            workers,
            'run_persist_broadcast_target',
            new=mock.AsyncMock(side_effect=persist_target),
        ):
            operation = asyncio.create_task(
                creation.create_external_broadcasts(
                    bot=bot,
                    request=request(),
                )
            )
            await persist_started.wait()
            operation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            release_persist.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation

        message.delete.assert_not_awaited()
        self.assertNotIn(42, creation._active_games)


class BroadcastCreationPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_service_warnings_are_published_after_completion(self):
        result = game_open_workers.OpenGameResult(
            game_id=42,
            guild_id=300,
            requester_id=100,
            host_name='Host',
            size=(1, 1),
            expiration_hours=24,
            is_ranked=True,
            is_mobile=True,
            notes_display='A note',
            warnings=(),
            role_locks=(game_open_workers.OpenGameSide(1, 501, 'Jets'),),
        )
        send = mock.AsyncMock(return_value=SimpleNamespace())
        broadcast = mock.AsyncMock(
            return_value=SimpleNamespace(
                warnings=(':warning: exact external target failed',)
            )
        )

        await game_open.publish_open_game_result(
            result,
            prefix='$',
            send=send,
            broadcast=broadcast,
        )

        self.assertEqual(send.await_count, 2)
        self.assertEqual(
            send.await_args_list[1].args[0],
            ':warning: exact external target failed',
        )
