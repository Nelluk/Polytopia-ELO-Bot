"""Focused offline coverage for the P7.11 squad-show workspace."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.squad_show_workers')
service = import_offline_runtime('modules.squad_show')
views = import_offline_runtime('modules.squad_show_views')
games = import_offline_runtime('modules.games')


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1
                return False

        return ConnectionContext()


class FakeQuery:
    def __init__(self, rows, count=None):
        self.rows = tuple(rows)
        self._count = len(self.rows) if count is None else count
        self.limit_calls = []

    def count(self):
        return self._count

    def limit(self, value):
        self.limit_calls.append(value)
        return FakeQuery(self.rows[:value], count=min(self._count, value))

    def __getitem__(self, item):
        return self.rows[item]


def card(index, *, guild_id=300):
    return workers.SquadShowCard(
        guild_id=guild_id,
        squad_id=1000 + index,
        squad_name=f'Squad {index:02d}',
        members=(
            workers.SquadShowMember(
                player_id=2000 + index,
                discord_id=3000 + index,
                name=f'Player {index:02d}',
                team_emoji='⚔️' if index % 2 else '🛡️',
            ),
            workers.SquadShowMember(
                player_id=4000 + index,
                discord_id=5000 + index,
                name=f'Partner {index:02d}',
                team_emoji='',
            ),
        ),
        elo=1500 - index,
        wins=index,
        losses=index // 2,
        leaderboard_rank=index,
        leaderboard_length=60,
        recent_games=(
            workers.SquadShowRecentGame(
                headline=f'Game {index}',
                summary='2026-08-04 - 2v2 - WINNER: Player',
            ),
        ),
    )


def result(count=3, *, selected=None):
    cards = tuple(card(index) for index in range(1, count + 1))
    return workers.SquadShowResult(
        guild_id=300,
        requester_id=999,
        member_ids=(999,),
        cards=cards,
        selected_squad_id=selected,
        total_matches=count,
        truncated=False,
    )


class FakeResponse:
    def __init__(self):
        self.done = False
        self.defer = mock.AsyncMock(side_effect=self._done)
        self.send_message = mock.AsyncMock(side_effect=self._done)
        self.edit_message = mock.AsyncMock(side_effect=self._done)
        self.send_modal = mock.AsyncMock(side_effect=self._done)

    async def _done(self, *args, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


class FakeComponentResponse(FakeResponse):
    """Model discord.py's component defer/original-response semantics."""

    def __init__(self):
        super().__init__()
        self.defer_type = None
        self.deferred_ephemeral = None
        self.defer = mock.AsyncMock(side_effect=self._component_defer)

    async def _component_defer(
        self,
        *,
        ephemeral=False,
        thinking=False,
    ):
        self.done = True
        self.defer_type = (
            'deferred_channel_message'
            if thinking
            else 'deferred_message_update'
        )
        self.deferred_ephemeral = bool(ephemeral and thinking)


class FakePublicMessage:
    def __init__(self, view):
        self.view = view
        self.edit = mock.AsyncMock()


class FakeComponentInteraction:
    """A component interaction whose original response is the public message."""

    def __init__(self, message, *, edit_error=None):
        self.user = SimpleNamespace(id=999)
        self.message = message
        self.response = FakeComponentResponse()
        self.followup = SimpleNamespace(send=mock.AsyncMock())
        self._edit_error = edit_error
        self.edit_original_response = mock.AsyncMock(
            side_effect=self._edit_original_response,
        )
        self.delete_original_response = mock.AsyncMock(
            side_effect=AssertionError(
                'component success must not delete the public original',
            ),
        )

    async def _edit_original_response(self, *, view):
        if self._edit_error is not None:
            raise self._edit_error
        self.message.view = view
        return self.message


def interaction(user_id=999):
    response = FakeResponse()
    message = SimpleNamespace(edit=mock.AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=response,
        followup=SimpleNamespace(send=mock.AsyncMock()),
        edit_original_response=mock.AsyncMock(),
        delete_original_response=mock.AsyncMock(),
        message=message,
    )


class RegistrationAndCompatibilityTests(unittest.TestCase):
    def test_exact_native_shape_and_prefix_retirement(self):
        root = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'squad'
        )
        self.assertEqual(
            [command.name for command in root.commands],
            ['show', 'name'],
        )
        show = root.get_command('show')
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter in show.parameters
            ],
            [('squad_id', discord.AppCommandOptionType.integer, False)],
        )
        name = root.get_command('name')
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter in name.parameters
            ],
            [
                ('squad_id', discord.AppCommandOptionType.integer, True),
                ('name', discord.AppCommandOptionType.string, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        prefix_names = {command.name for command in games.polygames.__cog_commands__}
        self.assertNotIn('squad', prefix_names)
        self.assertNotIn(
            'squads',
            {alias for command in games.polygames.__cog_commands__ for alias in command.aliases},
        )
        self.assertNotIn(
            'squadname',
            {command.name for command in games.polygames.__cog_commands__},
        )
        lbsquad = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'lbsquad'
        )
        self.assertEqual(set(lbsquad.aliases), {'squadlb'})


class WorkerBoundaryTests(unittest.TestCase):
    def request(self, **kwargs):
        values = dict(
            guild_id=300,
            requester_id=999,
            member_ids=(999,),
            team_enabled=True,
            channel_allowed=True,
        )
        values.update(kwargs)
        return workers.SquadShowRequest(**values)

    def test_result_is_frozen_and_worker_connection_is_closed(self):
        database = FakeDatabase()
        fake_squad = SimpleNamespace(id=1, guild_id=300)
        query = FakeQuery([SimpleNamespace(squad=fake_squad)])
        loaded = card(1)
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers, '_load_players', return_value=(object(),)),
            mock.patch.object(workers, '_guild_scoped_matching_query', return_value=query),
            mock.patch.object(workers, '_load_card', return_value=loaded),
        ):
            result_value = workers.load_squad_show(self.request())

        self.assertEqual((database.opened, database.closed), (1, 1))
        self.assertIsInstance(result_value.cards, tuple)
        with self.assertRaises(FrozenInstanceError):
            result_value.cards = ()

    def test_search_preserves_order_and_caps_loaded_cards_at_fifty(self):
        squads = [
            SimpleNamespace(id=index, guild_id=300)
            for index in range(1, 52)
        ]
        query = FakeQuery(
            [SimpleNamespace(squad=squad) for squad in squads],
            count=51,
        )
        database = FakeDatabase()
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers, '_load_players', return_value=(object(),)),
            mock.patch.object(workers, '_guild_scoped_matching_query', return_value=query),
            mock.patch.object(
                workers,
                '_load_card',
                side_effect=lambda squad: card(squad.id),
            ),
        ):
            result_value = workers.load_squad_show(self.request())

        self.assertEqual(len(result_value.cards), 50)
        self.assertEqual(
            [loaded.squad_id for loaded in result_value.cards],
            list(range(1001, 1051)),
        )
        self.assertEqual(result_value.total_matches, 51)
        self.assertTrue(result_value.truncated)
        self.assertEqual(query.limit_calls, [workers.MAX_SQUAD_MATCHES + 1])

    def test_bounded_rows_use_database_limit_instead_of_python_slice(self):
        query = FakeQuery(range(100))

        loaded = workers._bounded_query_rows(query, 7)

        self.assertEqual(loaded, tuple(range(7)))
        self.assertEqual(query.limit_calls, [7])

    def test_recent_games_query_orders_date_then_id_descending(self):
        class OrderedField:
            def __init__(self, name):
                self.name = name

            def __eq__(self, other):
                return True

            def __neg__(self):
                return ('descending', self.name)

        class Query:
            def __init__(self):
                self.ordering = None

            def join(self, _model):
                return self

            def where(self, _predicate):
                return self

            def order_by(self, *ordering):
                self.ordering = ordering
                return self

            def __getitem__(self, _item):
                return self

        query = Query()
        game_side = SimpleNamespace(
            squad=OrderedField('squad'),
            select=lambda _game: query,
        )
        game_model = SimpleNamespace(
            date=OrderedField('date'),
            id=OrderedField('id'),
        )
        with (
            mock.patch.object(workers.models, 'GameSide', game_side),
            mock.patch.object(workers.models, 'Game', game_model),
            mock.patch.object(
                workers.utilities,
                'summarize_game_list',
                return_value=(),
            ),
        ):
            self.assertEqual(workers._recent_games(SimpleNamespace(id=1)), ())

        self.assertEqual(
            query.ordering,
            (('descending', 'date'), ('descending', 'id')),
        )

    def test_permission_and_member_validation_happen_at_worker_boundary(self):
        with self.assertRaises(workers.SquadShowPermissionError):
            workers.load_squad_show(self.request(team_enabled=False))
        with self.assertRaises(workers.SquadShowValidationError):
            workers.load_squad_show(self.request(member_ids=(1, 1)))
        with self.assertRaises(workers.SquadShowValidationError):
            workers.load_squad_show(self.request(squad_id=0))

    def test_exact_id_is_guild_scoped_and_does_not_resolve_members(self):
        database = FakeDatabase()
        fake_squad = SimpleNamespace(id=42, guild_id=300)
        loaded = card(42)
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=fake_squad),
            mock.patch.object(workers, '_load_players') as load_players,
            mock.patch.object(workers, '_load_card', return_value=loaded),
        ):
            result_value = workers.load_squad_show(
                self.request(squad_id=42, member_ids=())
            )
        self.assertEqual(result_value.selected_squad_id, 1000 + 42)
        load_players.assert_not_called()

        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(
                workers.models.Squad,
                'get',
                return_value=SimpleNamespace(id=42, guild_id=301),
            ),
        ):
            with self.assertRaises(workers.SquadShowWrongGuild):
                workers.load_squad_show(self.request(squad_id=42))

    def test_unregistered_member_and_zero_match_search_are_private_errors_at_adapter(self):
        with (
            mock.patch.object(workers.models, 'db', FakeDatabase()),
            mock.patch.object(
                workers,
                '_load_players',
                side_effect=workers.SquadShowPlayerNotFound(
                    '<@777> is not a registered player on this server.'
                ),
            ),
        ):
            with self.assertRaises(workers.SquadShowPlayerNotFound):
                workers.load_squad_show(self.request(member_ids=(777,)))

        database = FakeDatabase()
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers, '_load_players', return_value=(object(),)),
            mock.patch.object(
                workers,
                '_guild_scoped_matching_query',
                return_value=FakeQuery((), count=0),
            ),
        ):
            result_value = workers.load_squad_show(self.request())
        self.assertEqual(result_value.cards, ())
        self.assertFalse(result_value.truncated)

    def test_cancellation_drains_non_cancellable_read(self):
        started = threading.Event()
        finished = threading.Event()

        def slow_read(_request):
            started.set()
            time.sleep(0.06)
            finished.set()
            return result(1)

        async def run_case():
            with mock.patch.object(workers, 'load_squad_show', side_effect=slow_read):
                task = asyncio.create_task(
                    workers.run_squad_show(self.request())
                )
                deadline = asyncio.get_running_loop().time() + 1
                while not started.is_set():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail('the worker did not start')
                    await asyncio.sleep(0.001)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    self.fail('the cancelled read did not propagate cancellation')
            self.assertTrue(finished.is_set())

        asyncio.run(run_case())

    def test_slow_worker_keeps_event_loop_responsive(self):
        async def run_case():
            async def ticker():
                ticks = 0
                while ticks < 3:
                    await asyncio.sleep(0.01)
                    ticks += 1
                return ticks

            with mock.patch.object(
                workers,
                'load_squad_show',
                side_effect=lambda _request: (time.sleep(0.05), result(1))[1],
            ):
                loaded, ticks = await asyncio.gather(
                    workers.run_squad_show(self.request()),
                    ticker(),
                )
            return loaded, ticks

        loaded, ticks = asyncio.run(run_case())
        self.assertEqual(len(loaded.cards), 1)
        self.assertEqual(ticks, 3)
        self.assertLessEqual(workers._squad_show_read_executor._max_workers, 2)


class ServiceAndViewTests(unittest.IsolatedAsyncioTestCase):
    def test_non_strict_channel_policy_and_member_capture(self):
        member = SimpleNamespace(id=999)
        with mock.patch.object(
            service,
            '_setting',
            side_effect=lambda _guild, name, default=None: {
                'allow_teams': True,
                'bot_channels': (123,),
                'bot_channels_private': (456,),
            }.get(name, default),
        ):
            self.assertIsNone(service.native_access_error(member, 300, 123))
            self.assertIsNone(service.native_access_error(member, 300, 456))
            self.assertIsNotNone(service.native_access_error(member, 300, 789))
            self.assertEqual(
                service.capture_member_ids([SimpleNamespace(id=1), 2]),
                (1, 2),
            )
        with self.assertRaises(workers.SquadShowValidationError):
            service.capture_member_ids([SimpleNamespace(id=1)] * 2)

    def make_view(self, count=26, *, selected=None, loader=None):
        return views.SquadShowWorkspace(
            requester_id=999,
            result=result(count, selected=selected),
            member_loader=loader,
        )

    def test_component_limits_and_dense_card_fields(self):
        view = self.make_view()
        user_select = view.member_select
        result_select = view.result_select
        self.assertEqual(user_select.min_values, 1)
        self.assertEqual(user_select.max_values, 3)
        self.assertLessEqual(len(result_select.options), 25)
        self.assertEqual(view.page_count, 3)
        text = '\n'.join(
            child.content
            for child in view.walk_children()
            if isinstance(child, discord.ui.TextDisplay)
        )
        self.assertIn('ELO', text)

        view.selected_squad_id = view.result.cards[0].squad_id
        view.rebuild()
        text = '\n'.join(
            child.content
            for child in view.walk_children()
            if isinstance(child, discord.ui.TextDisplay)
        )
        self.assertIn('Current squad ELO', text)
        self.assertIn('Confirmed ranked record', text)
        self.assertIn('Current leaderboard', text)
        self.assertIn('Most recent games', text)

    def test_rendered_recent_games_keep_newest_first_with_id_tiebreak(self):
        ordered_card = replace(
            card(1),
            recent_games=(
                workers.SquadShowRecentGame(
                    headline='Newest date',
                    summary='2026-08-04 - game 9',
                ),
                workers.SquadShowRecentGame(
                    headline='Same day newer ID',
                    summary='2026-08-03 - game 8',
                ),
                workers.SquadShowRecentGame(
                    headline='Same day older ID',
                    summary='2026-08-03 - game 7',
                ),
                workers.SquadShowRecentGame(
                    headline='Older date',
                    summary='2026-08-02 - game 6',
                ),
            ),
        )
        loaded = replace(
            result(1, selected=1001),
            cards=(ordered_card,),
        )
        view = views.SquadShowWorkspace(requester_id=999, result=loaded)
        body = view._card_body(ordered_card)

        self.assertLess(body.index('Newest date'), body.index('Same day newer ID'))
        self.assertLess(
            body.index('Same day newer ID'),
            body.index('Same day older ID'),
        )
        self.assertLess(body.index('Same day older ID'), body.index('Older date'))

    async def test_paging_and_result_selection_use_loaded_snapshot_only(self):
        loader = mock.AsyncMock()
        view = self.make_view(loader=loader)
        next_page = interaction()
        await view._next_page(next_page)
        self.assertEqual(view.page_index, 1)
        loader.assert_not_awaited()
        next_page.response.edit_message.assert_awaited_once_with(view=view)

        select = interaction()
        view.result_select._values = [str(view.result.cards[10].squad_id)]
        await view._select_result(select)
        self.assertEqual(view.selected_squad_id, view.result.cards[10].squad_id)
        loader.assert_not_awaited()
        select.response.edit_message.assert_awaited_once_with(view=view)

    async def test_member_selection_edits_public_original_after_later_page(self):
        replacement = result(2, selected=None)
        loader = mock.AsyncMock(return_value=replacement)
        view = self.make_view(loader=loader)
        public_message = FakePublicMessage(view)
        view.message = public_message
        await view._next_page(interaction())
        self.assertEqual(view.page_index, 1)
        previous_result_select = view.result_select
        select = FakeComponentInteraction(public_message)
        view.member_select._values = [SimpleNamespace(id=10), SimpleNamespace(id=20)]

        await view._select_members(select)

        loader.assert_awaited_once_with((10, 20))
        select.response.defer.assert_awaited_once_with()
        self.assertEqual(select.response.defer_type, 'deferred_message_update')
        self.assertFalse(select.response.deferred_ephemeral)
        select.delete_original_response.assert_not_awaited()
        select.edit_original_response.assert_awaited_once_with(view=view)
        public_message.edit.assert_not_awaited()
        self.assertIsNot(view.result_select, previous_result_select)
        self.assertEqual(view.page_index, 0)
        page_button = next(
            child
            for child in view.walk_children()
            if isinstance(child, discord.ui.Button)
            and child.label.startswith('Page ')
        )
        self.assertEqual(page_button.label, 'Page 1/1')
        self.assertIs(view.result, replacement)

    async def test_failed_public_refresh_rolls_back_state_and_controls(self):
        previous = result(1, selected=1001)
        replacement = result(2, selected=None)
        publication_error = RuntimeError('public edit failed')
        loader = mock.AsyncMock(return_value=replacement)
        view = views.SquadShowWorkspace(
            requester_id=999,
            result=previous,
            member_loader=loader,
        )
        public_message = FakePublicMessage(view)
        view.message = public_message
        previous_member_select = view.member_select
        select = FakeComponentInteraction(
            public_message,
            edit_error=publication_error,
        )
        view.member_select._values = [SimpleNamespace(id=10)]

        with mock.patch.object(views.logger, 'exception') as log:
            await view._select_members(select)

        loader.assert_awaited_once_with((10,))
        select.edit_original_response.assert_awaited_once_with(view=view)
        select.delete_original_response.assert_not_awaited()
        select.followup.send.assert_awaited_once_with(
            'The squad workspace could not be refreshed. Please run '
            '`/squad show` again.',
            ephemeral=True,
        )
        self.assertIs(view.result, previous)
        self.assertEqual(view.selected_squad_id, 1001)
        self.assertEqual(view.page_index, 0)
        self.assertIsNot(view.member_select, previous_member_select)
        self.assertIsNone(getattr(view, 'result_select', None))
        self.assertTrue(log.called)
        self.assertIs(log.call_args.args[1], publication_error)

    async def test_zero_match_member_search_stays_private_and_keeps_public_snapshot(self):
        empty = workers.SquadShowResult(
            guild_id=300,
            requester_id=999,
            member_ids=(10,),
            cards=(),
            selected_squad_id=None,
            total_matches=0,
            truncated=False,
        )
        loader = mock.AsyncMock(return_value=empty)
        view = self.make_view(loader=loader)
        view.message = SimpleNamespace(edit=mock.AsyncMock())
        select = interaction()
        view.member_select._values = [SimpleNamespace(id=10)]

        await view._select_members(select)

        loader.assert_awaited_once_with((10,))
        select.followup.send.assert_awaited_once_with(
            'No eligible squads matched those members.',
            ephemeral=True,
        )
        view.message.edit.assert_not_awaited()

    async def test_member_search_load_failure_stays_private_and_keeps_public_snapshot(self):
        previous = result(3)
        loader = mock.AsyncMock(side_effect=RuntimeError('database unavailable'))
        view = views.SquadShowWorkspace(
            requester_id=999,
            result=previous,
            member_loader=loader,
        )
        view.message = SimpleNamespace(edit=mock.AsyncMock())
        select = interaction()
        view.member_select._values = [SimpleNamespace(id=10)]

        await view._select_members(select)

        loader.assert_awaited_once_with((10,))
        select.response.defer.assert_awaited_once_with()
        select.followup.send.assert_awaited_once_with(
            'Could not search squads for those members. Please run '
            '`/squad show` again.',
            ephemeral=True,
        )
        select.delete_original_response.assert_not_awaited()
        view.message.edit.assert_not_awaited()
        self.assertIs(view.result, previous)

    async def test_invalid_member_selection_stays_private_without_loading(self):
        loader = mock.AsyncMock()
        view = self.make_view(loader=loader)
        select = interaction()
        view.member_select._values = []

        await view._select_members(select)

        select.response.send_message.assert_awaited_once_with(
            'The member selection is invalid. Choose one to three guild '
            'members and try again.',
            ephemeral=True,
        )
        select.response.defer.assert_not_awaited()
        loader.assert_not_awaited()

    async def test_invalid_unauthorized_and_expired_controls_are_private(self):
        view = self.make_view(loader=mock.AsyncMock())
        denied = interaction(user_id=888)
        await view.interaction_check(denied)
        denied.response.send_message.assert_awaited_once_with(
            view.unauthorized_message,
            ephemeral=True,
        )

        invalid = interaction()
        view.result_select._values = ['not-a-squad']
        await view._select_result(invalid)
        invalid.response.send_message.assert_awaited_once()
        invalid.response.send_message.assert_awaited_once_with(
            'Choose one of the displayed squads.',
            ephemeral=True,
        )

        expired = interaction()
        view.stop()
        await view._next_page(expired)
        expired.response.send_message.assert_awaited_once_with(
            view.expired_message,
            ephemeral=True,
        )


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self):
        response = FakeResponse()
        return SimpleNamespace(
            user=SimpleNamespace(id=999),
            guild=SimpleNamespace(id=300),
            channel_id=123,
            channel=SimpleNamespace(send=mock.AsyncMock(return_value=SimpleNamespace(id=1))),
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )

    def command(self):
        return next(
            child
            for root in games.polygames.__cog_app_commands__
            if root.name == 'squad'
            for child in root.commands
            if child.name == 'show'
        )

    async def test_native_command_private_defer_then_public_success(self):
        interaction_value = self._interaction()
        loaded = result(1, selected=1001)
        request_value = workers.SquadShowRequest(
            guild_id=300,
            requester_id=999,
            member_ids=(999,),
        )
        cog = object.__new__(games.polygames)
        with (
            mock.patch.object(service, 'native_access_error', return_value=None),
            mock.patch.object(service, 'build_request', return_value=request_value),
            mock.patch.object(
                workers,
                'run_squad_show',
                new=mock.AsyncMock(return_value=loaded),
            ) as run,
        ):
            await self.command().callback(cog, interaction_value, 1001)

        interaction_value.response.defer.assert_awaited_once_with(ephemeral=True)
        run.assert_awaited_once_with(request_value)
        interaction_value.delete_original_response.assert_awaited_once()
        interaction_value.channel.send.assert_awaited_once()
        self.assertFalse(
            interaction_value.channel.send.await_args.kwargs.get('ephemeral', False)
        )

    async def test_public_success_does_not_stall_when_private_delete_hangs(self):
        interaction_value = self._interaction()
        blocker = asyncio.Event()

        async def never_finishes():
            await blocker.wait()

        interaction_value.delete_original_response.side_effect = never_finishes
        loaded = result(1, selected=1001)
        cog = object.__new__(games.polygames)
        with (
            mock.patch.object(service, 'native_access_error', return_value=None),
            mock.patch.object(
                service,
                'build_request',
                return_value=workers.SquadShowRequest(
                    guild_id=300,
                    requester_id=999,
                    member_ids=(999,),
                ),
            ),
            mock.patch.object(
                workers,
                'run_squad_show',
                new=mock.AsyncMock(return_value=loaded),
            ),
            mock.patch.object(service, 'PRIVATE_RESPONSE_DELETE_TIMEOUT', 0.001),
        ):
            await self.command().callback(cog, interaction_value, 1001)

        interaction_value.channel.send.assert_awaited_once()

    async def test_native_permission_failure_stays_private_and_does_not_read(self):
        interaction_value = self._interaction()
        cog = object.__new__(games.polygames)
        with mock.patch.object(
            service,
            'native_access_error',
            return_value='Teams are not enabled on this server.',
        ), mock.patch.object(
            workers,
            'run_squad_show',
            new=mock.AsyncMock(),
        ) as run:
            await self.command().callback(cog, interaction_value)

        interaction_value.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction_value.followup.send.assert_awaited_once_with(
            'Teams are not enabled on this server.',
            ephemeral=True,
        )
        run.assert_not_awaited()

    async def test_native_zero_match_result_stays_private(self):
        interaction_value = self._interaction()
        empty = workers.SquadShowResult(
            guild_id=300,
            requester_id=999,
            member_ids=(999,),
            cards=(),
            selected_squad_id=None,
            total_matches=0,
            truncated=False,
        )
        cog = object.__new__(games.polygames)
        with (
            mock.patch.object(service, 'native_access_error', return_value=None),
            mock.patch.object(
                service,
                'build_request',
                return_value=workers.SquadShowRequest(
                    guild_id=300,
                    requester_id=999,
                    member_ids=(999,),
                ),
            ),
            mock.patch.object(
                workers,
                'run_squad_show',
                new=mock.AsyncMock(return_value=empty),
            ) as run,
        ):
            await self.command().callback(cog, interaction_value)

        run.assert_awaited_once()
        interaction_value.followup.send.assert_awaited_once_with(
            'No eligible squads matched those members.',
            ephemeral=True,
        )
        interaction_value.channel.send.assert_not_awaited()

    async def test_squadname_prefix_is_completely_retired(self):
        self.assertNotIn(
            'squadname',
            {command.name for command in games.polygames.__cog_commands__},
        )
