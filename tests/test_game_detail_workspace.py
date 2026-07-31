"""Focused offline coverage for the unified game-detail workspace."""

import asyncio
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_detail_workers')
views = import_offline_runtime('modules.game_detail_views')
games = import_offline_runtime('modules.games')


def fake_game(*, game_id=7, guild_id=10, pending=False, completed=True):
    member_one = SimpleNamespace(
        discord_id=101,
        name='Alpha Discord',
    )
    member_two = SimpleNamespace(
        discord_id=202,
        name='Beta Discord',
    )
    player_one = SimpleNamespace(
        id=11,
        name='Alpha',
        elo=1110,
        elo_moonrise=1120,
        discord_member=member_one,
    )
    player_two = SimpleNamespace(
        id=22,
        name='Beta',
        elo=1090,
        elo_moonrise=1080,
        discord_member=member_two,
    )
    tribe_one = SimpleNamespace(name='Bardur', emoji='🐻')
    tribe_two = SimpleNamespace(name='Luxidoor', emoji='💎')
    team_one = SimpleNamespace(
        id=31,
        name='Home',
        emoji='🏠',
        is_hidden=False,
        image_url='https://example.test/home.png',
        elo_alltime=1210,
    )
    team_two = SimpleNamespace(
        id=32,
        name='Away',
        emoji='✈️',
        is_hidden=False,
        image_url=None,
        elo_alltime=1190,
    )
    side_one = SimpleNamespace(
        id=41,
        position=1,
        size=1,
        team=team_one,
        squad=None,
        elo_change_team_alltime=12,
        elo_change_squad=0,
        required_role_id=501,
        team_chan=701,
        team_chan_external_server=None,
        win_confirmed=True,
        lineup=[SimpleNamespace(
            id=51,
            player=player_one,
            tribe=tribe_one,
            elo_after_game_moonrise=1120,
            elo_change_player_moonrise=20,
            elo_after_game=1110,
            elo_change_player=10,
        )],
    )
    side_two = SimpleNamespace(
        id=42,
        position=2,
        size=1,
        team=team_two,
        squad=None,
        elo_change_team_alltime=-12,
        elo_change_squad=0,
        required_role_id=None,
        team_chan=None,
        team_chan_external_server=None,
        win_confirmed=False,
        lineup=[SimpleNamespace(
            id=52,
            player=player_two,
            tribe=tribe_two,
            elo_after_game_moonrise=1080,
            elo_change_player_moonrise=-20,
            elo_after_game=1090,
            elo_change_player=-10,
        )],
    )
    host = SimpleNamespace(
        name='Alpha',
        discord_member=member_one,
    )
    game = SimpleNamespace(
        id=game_id,
        guild_id=guild_id,
        name='A Proper Game',
        date='2026-07-30',
        completed_ts='2026-07-31 01:02:03' if completed else None,
        win_claimed_ts='2026-07-30 23:00:00' if completed else None,
        expiration='2026-08-01 00:00:00',
        is_pending=pending,
        is_completed=completed,
        is_confirmed=completed,
        is_ranked=True,
        is_mobile=True,
        map_type='Dryland',
        notes='Season notes',
        league_season=8,
        league_tier=2,
        league_playoff=True,
        size=[1, 1],
        game_chan=702,
        host=host,
        winner=side_one if completed else None,
        gamesides=[side_one, side_two],
    )

    def uses_channel_id(channel_id):
        return channel_id in (701, 702)

    def series_record():
        return ((side_one, 1), (side_two, 0))

    game.uses_channel_id = uses_channel_id
    game.series_record = series_record
    return game


def snapshot(*, cross_guild=False, pending=False, completed=True):
    game = fake_game(pending=pending, completed=completed)
    request = workers.GameDetailRequest(
        guild_id=999 if cross_guild else 10,
        channel_id=702,
        requester_discord_id=900,
        game_id=game.id,
    )
    return workers._snapshot_from_game(
        game,
        request=request,
        inferred_from_channel=False,
    )


class GameDetailRegistrationTests(unittest.TestCase):
    def test_exact_slash_shape_and_prefix_alias(self):
        game_group = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        command = game_group.get_command('show')
        self.assertIsNotNone(command)
        self.assertEqual(
            [
                (parameter.name, parameter.required, parameter.type)
                for parameter in command.parameters
            ],
            [('game_id', False, discord.AppCommandOptionType.integer)],
        )
        prefix = {
            command.name: command
            for command in games.polygames.__cog_commands__
        }
        self.assertEqual(prefix['game'].aliases, ['match'])


class GameDetailWorkerTests(unittest.TestCase):
    def test_request_and_snapshot_are_immutable(self):
        request = workers.GameDetailRequest(10, 702, 900, 7)
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 8
        result = snapshot()
        with self.assertRaises(FrozenInstanceError):
            result.name = 'changed'
        with self.assertRaises(FrozenInstanceError):
            result.sides[0].lineups[0].player_name = 'changed'

    def test_worker_owns_connection_and_channel_inference(self):
        events = []

        @contextmanager
        def connection_context():
            events.append('open')
            yield
            events.append('close')

        game = fake_game()
        with (
            mock.patch.object(
                workers.models,
                'db',
                SimpleNamespace(connection_context=connection_context),
            ),
            mock.patch.object(
                workers.models.Game,
                'by_channel_id',
                return_value=game,
            ) as by_channel,
            mock.patch.object(
                workers.models.Game,
                'load_full_game',
                return_value=game,
            ) as load_full,
        ):
            result = workers.load_game_detail(
                workers.GameDetailRequest(10, 702, 900)
            )

        self.assertEqual(events, ['open', 'close'])
        by_channel.assert_called_once_with(chan_id=702)
        load_full.assert_called_once_with(game_id=7)
        self.assertTrue(result.inferred_from_channel)
        self.assertEqual(result.sides[0].lineups[0].discord_id, 101)

    def test_channel_absence_and_ambiguity_are_distinct(self):
        database = SimpleNamespace(
            connection_context=lambda: mock.MagicMock(),
        )
        request = workers.GameDetailRequest(10, 702, 900)
        with mock.patch.object(workers.models, 'db', database):
            with mock.patch.object(
                workers.models.Game,
                'by_channel_id',
                side_effect=workers.exceptions.NoMatches('none'),
            ):
                with self.assertRaisesRegex(
                    workers.GameDetailError,
                    'could not identify one game.*provide a game ID',
                ) as missing:
                    workers.load_game_detail(request)
            self.assertEqual(missing.exception.code, 'missing_channel_game')

            with mock.patch.object(
                workers.models.Game,
                'by_channel_id',
                side_effect=workers.exceptions.TooManyMatches('many'),
            ):
                with self.assertRaisesRegex(
                    workers.GameDetailError,
                    'multiple games.*provide a game ID',
                ) as ambiguous:
                    workers.load_game_detail(request)
            self.assertEqual(ambiguous.exception.code, 'ambiguous_channel')

    def test_invalid_and_missing_ids_preserve_meaningful_errors(self):
        with self.assertRaisesRegex(workers.GameDetailError, 'Invalid game ID'):
            workers.load_game_detail(workers.GameDetailRequest(10, 702, 900, 0))

        database = SimpleNamespace(
            connection_context=lambda: mock.MagicMock(),
        )
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(
                workers.models.Game,
                'load_full_game',
                side_effect=workers.models.DoesNotExist,
            ),
        ):
            with self.assertRaisesRegex(
                workers.GameDetailError,
                'Game with ID 999 cannot be found',
            ):
                workers.load_game_detail(
                    workers.GameDetailRequest(10, 702, 900, 999)
                )

    def test_cross_guild_explicit_lookup_is_marked_for_compatibility(self):
        game = fake_game(guild_id=10)
        database = SimpleNamespace(
            connection_context=lambda: mock.MagicMock(),
        )
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(
                workers.models.Game,
                'load_full_game',
                return_value=game,
            ),
        ):
            result = workers.load_game_detail(
                workers.GameDetailRequest(999, 702, 900, 7)
            )
        self.assertTrue(result.cross_guild)
        self.assertEqual(result.game_channel_id, 702)

    def test_snapshot_covers_pending_incomplete_and_completed_states(self):
        pending = snapshot(pending=True, completed=False)
        incomplete = snapshot(pending=False, completed=False)
        completed = snapshot(pending=False, completed=True)
        self.assertIn(pending.status_label, ('Open', 'Full — waiting to start', 'Expired open game'))
        self.assertEqual(incomplete.status_label, 'Incomplete')
        self.assertEqual(completed.status_label, 'Completed')
        self.assertIn('Winner:', completed.result_label)
        self.assertEqual(completed.map_type, 'Dryland')
        self.assertEqual(completed.sides[0].lineups[0].tribe_name, 'Bardur')
        self.assertEqual(completed.league_season, 8)
        self.assertIn('series', completed.series_record_label.lower())


class GameDetailExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_read_keeps_event_loop_responsive(self):
        original = workers.load_game_detail

        def slow(_request):
            time.sleep(0.08)
            return snapshot()

        workers.load_game_detail = slow
        try:
            task = asyncio.create_task(workers.run_game_detail(
                workers.GameDetailRequest(10, 702, 900, 7)
            ))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            heartbeat = asyncio.Event()

            async def pulse():
                await asyncio.sleep(0)
                heartbeat.set()

            await pulse()
            self.assertTrue(heartbeat.is_set())
            await asyncio.sleep(0.10)
            self.assertEqual((await task).game_id, 7)
        finally:
            workers.load_game_detail = original


class GameDetailViewTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self):
        guild = SimpleNamespace(
            id=10,
            name='Test Guild',
            get_member=lambda member_id: None,
            get_role=lambda role_id: None,
        )
        bot = SimpleNamespace(
            guilds=[guild],
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
            get_user=lambda member_id: None,
            get_channel=lambda channel_id: None,
        )
        display = views.resolve_display(snapshot(), guild=guild, bot=bot)
        return views.GameDetailWorkspace(requester_id=900, display=display)

    async def test_public_result_has_requester_only_controls(self):
        view = self.make_view()
        denied = SimpleNamespace(
            user=SimpleNamespace(id=901),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(denied))
        denied.response.send_message.assert_awaited_once_with(
            view.unauthorized_message,
            ephemeral=True,
        )
        allowed = SimpleNamespace(user=SimpleNamespace(id=900))
        self.assertTrue(await view.interaction_check(allowed))

    async def test_section_navigation_is_snapshot_only(self):
        view = self.make_view()
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=mock.AsyncMock(),
            edit_message=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=900),
            response=response,
        )
        view.section_select._values = ['players']
        await view._select_section(interaction)
        self.assertEqual(view.section, 'players')
        response.edit_message.assert_awaited_once()

    async def test_expired_request_is_ephemeral(self):
        view = self.make_view()
        view.stop()
        response = SimpleNamespace(send_message=mock.AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=900),
            response=response,
        )
        self.assertFalse(await view.interaction_check(interaction))
        response.send_message.assert_awaited_once_with(
            view.expired_message,
            ephemeral=True,
        )

    def test_v2_serialization_and_component_count(self):
        view = self.make_view()
        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        self.assertLessEqual(view.total_children_count, 40)
        self.assertTrue(view.to_components())
        text = '\n'.join(
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn('Game 7', text)
        self.assertIn('Dryland', text)

    def test_team_image_preserves_local_attachment_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / 'team-logo-31.png'
            image_path.write_bytes(b'not decoded by the view')
            attachment = SimpleNamespace(
                embed_url='attachment://team-logo-31.png',
                path=image_path,
                filename='team-logo-31.png',
            )
            guild = SimpleNamespace(
                id=10,
                name='Test Guild',
                get_member=lambda member_id: None,
                get_role=lambda role_id: None,
            )
            bot = SimpleNamespace(
                guilds=[guild],
                get_guild=lambda guild_id: guild,
                get_user=lambda member_id: None,
                get_channel=lambda channel_id: None,
            )
            with mock.patch.object(
                views.image_storage,
                'local_attachment',
                return_value=attachment,
            ):
                display = views.resolve_display(
                    snapshot(),
                    guild=guild,
                    bot=bot,
                )
                view = views.GameDetailWorkspace(
                    requester_id=900,
                    display=display,
                )

            self.assertEqual(display.asset.source, 'attachment://team-logo-31.png')
            uploaded_file = view.new_file()
            try:
                self.assertEqual(uploaded_file.filename, 'team-logo-31.png')
            finally:
                uploaded_file.close()
            self.assertTrue(view.to_components())


class GameDetailAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_prefix_uses_shared_workspace_and_alias(self):
        cog = games.polygames.__new__(games.polygames)
        cog._send_game_detail_workspace = mock.AsyncMock(return_value=True)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=SimpleNamespace(id=900),
            channel=SimpleNamespace(id=702),
        )
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'game'
        )
        await command.callback(cog, ctx, game_search='7')
        cog._send_game_detail_workspace.assert_awaited_once_with(
            ctx,
            guild=ctx.guild,
            requester_id=900,
            channel_id=702,
            game_id=7,
        )

    async def test_nonnumeric_prefix_keeps_game_search_delegation(self):
        cog = games.polygames.__new__(games.polygames)
        search_command = object()
        cog.bot = SimpleNamespace(get_command=lambda name: search_command)
        ctx = SimpleNamespace(
            invoke=mock.AsyncMock(),
        )
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'game'
        )
        await command.callback(cog, ctx, game_search='Oceans')
        ctx.invoke.assert_awaited_once_with(search_command, args='Oceans')

    async def test_numeric_prefix_failures_remain_public(self):
        cog = games.polygames.__new__(games.polygames)

        async def load(_request):
            raise workers.GameDetailError(
                'Game with ID 999 cannot be found.',
                code='not_found',
            )

        cog._load_game_detail = load
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=SimpleNamespace(id=900),
            channel=SimpleNamespace(id=702),
            send=mock.AsyncMock(),
        )
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'game'
        )
        result = await command.callback(cog, ctx, game_search='999')
        self.assertFalse(result)
        ctx.send.assert_awaited_once_with(
            'Game with ID 999 cannot be found.',
        )

    async def test_slash_timeout_failure_is_ephemeral(self):
        cog = games.polygames.__new__(games.polygames)
        cog._load_game_detail = mock.AsyncMock(
            side_effect=asyncio.TimeoutError(),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        result = await cog._send_game_detail_workspace(
            interaction,
            guild=interaction.guild,
            requester_id=900,
            channel_id=702,
            game_id=7,
            slash=True,
        )
        self.assertFalse(result)
        interaction.followup.send.assert_awaited_once_with(
            'Game detail lookup timed out. Please try again.',
            ephemeral=True,
        )

    async def test_show_keeps_guild_only_group_without_bot_channel_check(self):
        game_group = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        show = game_group.get_command('show')
        self.assertTrue(game_group.guild_only)
        self.assertFalse(any(
            'bot' in getattr(check, '__name__', '').lower()
            for check in getattr(show, 'checks', ())
        ))

    async def test_slash_defers_before_slow_read_and_errors_ephemerally(self):
        cog = games.polygames.__new__(games.polygames)
        events = []

        async def load(_request):
            events.append('load')
            raise workers.GameDetailError('Please provide a game ID.', code='missing_channel_game')

        cog._load_game_detail = load
        cog.bot = SimpleNamespace()
        response = SimpleNamespace()

        async def defer():
            events.append('defer')

        response.defer = defer
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            user=SimpleNamespace(id=900),
            channel_id=702,
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        command = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('show')
        await command.callback(cog, interaction, None)
        self.assertEqual(events, ['defer', 'load'])
        interaction.followup.send.assert_awaited_once_with(
            'Please provide a game ID.',
            ephemeral=True,
        )


if __name__ == '__main__':
    unittest.main()
