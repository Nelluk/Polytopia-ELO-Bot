"""Focused coverage for P8.7 House list/show reads."""

import asyncio
from contextlib import asynccontextmanager
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import time
import threading
import unittest
from unittest import mock

import discord
from discord.ext import commands
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.house_show_workers')
service = import_offline_runtime('modules.house_show')
league = import_offline_runtime('modules.league')


def member(member_id, name, *role_names):
    return workers.HouseMemberSnapshot(
        discord_id=member_id,
        display_name=name,
        role_names=tuple(role_names),
    )


def guild_snapshot():
    return workers.HouseGuildSnapshot(
        guild_id=300,
        members=(
            member(10, 'Leader', 'Ninjas', 'House Leader', 'Ronin'),
            member(11, 'Player', 'Ninjas', 'Ronin'),
            member(12, 'Jet', 'Jets', 'The Jets'),
        ),
        role_names=(
            'Ninjas', 'Jets', 'House Leader', 'Ronin', 'The Jets',
        ),
    )


def request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        house_lookup='Ninjas',
        require_selection=True,
        league_scope=True,
        channel_allowed=True,
        inactive_role_name='Inactive',
        guild_snapshot=guild_snapshot(),
    )
    values.update(overrides)
    return workers.HouseShowRequest(**values)


def team(team_id, name, *, archived=False, roster=()):
    return workers.HouseTeamRow(
        team_id=team_id,
        name=name,
        emoji='⚔️',
        elo=1200 + team_id,
        league_tier=2,
        tier_name='Pro',
        archived=archived,
        role_found=True,
        roster=tuple(roster),
        roster_truncated=False,
    )


def house(house_id, name, *, teams=()):
    return workers.HouseRow(
        house_id=house_id,
        name=name,
        emoji='🥷',
        image_url='https://example.test/house.png',
        league_tokens=house_id,
        role_found=True,
        leaders=('Leader',),
        coleaders=(),
        recruiters=(),
        teams=tuple(teams),
    )


def result(*, selected=1, houses=None):
    if houses is None:
        houses = (
            house(
                1,
                'Ninjas',
                teams=(
                    team(
                        1,
                        'Ronin',
                        roster=(workers.HouseRosterRow(10, 'Leader', 1350),),
                    ),
                ),
            ),
            house(2, 'Jets'),
        )
    return workers.HouseShowResult(
        guild_id=300,
        requester_id=10,
        houses=tuple(houses),
        selected_house_id=selected,
        houses_truncated=False,
        teams_truncated=False,
    )


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1
                return False

        return Context()


class FakeQuery:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def where(self, *args):
        return self

    def order_by(self, *args):
        return self

    def limit(self, limit):
        return self

    def __iter__(self):
        return iter(self.rows)


class RegistrationTests(unittest.TestCase):
    def test_exact_native_shape_and_retained_prefixes(self):
        root = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'house'
        )
        self.assertTrue(root.guild_only)
        self.assertEqual(
            {command.name for command in root.commands},
            {'show', 'list', 'name', 'image'},
        )
        show = root.get_command('show')
        self.assertEqual(
            [(parameter.name, parameter.required, parameter.type)
             for parameter in show.parameters],
            [('house', False, discord.AppCommandOptionType.string)],
        )
        self.assertEqual(root.get_command('list').parameters, [])

        prefix = {command.name: command for command in league.league.__cog_commands__}
        self.assertIsInstance(prefix['house'], commands.Command)
        self.assertIsInstance(prefix['houses'], commands.Command)
        self.assertEqual(prefix['houses'].aliases, ['balance'])


class RequestAndWorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitive_snapshots(self):
        with self.assertRaises(FrozenInstanceError):
            request().guild_id = 1
        snapshot = guild_snapshot()
        self.assertEqual(snapshot.members[0].role_names[-1], 'Ronin')
        self.assertFalse(any(hasattr(value, 'guild') for value in snapshot.members))

    def test_selected_house_exact_partial_inference_and_ambiguity(self):
        houses = (SimpleNamespace(id=1, name='Ninjas'), SimpleNamespace(id=2, name='Jets'))
        self.assertEqual(workers._resolve_selected_house(request(), houses), 1)
        self.assertEqual(
            workers._resolve_selected_house(request(house_lookup='jet'), houses),
            2,
        )
        self.assertEqual(
            workers._resolve_selected_house(request(house_lookup=None), houses),
            1,
        )
        with self.assertRaises(workers.HouseShowLookupError):
            workers._resolve_selected_house(
                request(
                    house_lookup=None,
                    guild_snapshot=workers.HouseGuildSnapshot(
                        guild_id=300,
                        members=(member(10, 'Leader', 'Ninjas', 'Jets'),),
                        role_names=('Ninjas', 'Jets'),
                    ),
                ),
                houses,
            )

    def test_worker_owns_connection_scopes_teams_and_returns_dense_snapshot(self):
        database = FakeDatabase()
        ninjas = SimpleNamespace(
            id=1, name='Ninjas', emoji='🥷', image_url=None, league_tokens=3,
        )
        jets = SimpleNamespace(
            id=2, name='Jets', emoji='✈️', image_url=None, league_tokens=1,
        )
        ronin = SimpleNamespace(
            id=21,
            name='Ronin',
            emoji='⚔️',
            elo=1300,
            league_tier=2,
            is_archived=False,
            house_id=1,
        )
        house_model = SimpleNamespace(
            select=mock.Mock(return_value=FakeQuery((ninjas, jets))),
            name=mock.MagicMock(),
        )
        team_model = SimpleNamespace(
            select=mock.Mock(return_value=FakeQuery((ronin,))),
            guild_id=mock.MagicMock(),
            house=mock.MagicMock(),
            is_hidden=mock.MagicMock(),
            is_archived=mock.MagicMock(),
            league_tier=mock.MagicMock(),
            elo=mock.MagicMock(),
            id=mock.MagicMock(),
        )
        team_model.house.in_.return_value = True
        fake_player = SimpleNamespace(
            discord_member=SimpleNamespace(discord_id=10),
            elo_moonrise=1400,
        )
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models, 'House', house_model
        ), mock.patch.object(workers.models, 'Team', team_model), mock.patch.object(
            workers, '_load_players', return_value=(fake_player,)
        ):
            loaded = workers.load_house_show(request())

        self.assertEqual(database.opened, 1)
        self.assertEqual(database.closed, 1)
        self.assertEqual(loaded.selected_house_id, 1)
        self.assertEqual(loaded.houses[0].teams[0].roster[0].elo, 1400)
        self.assertEqual(loaded.houses[0].leaders, ('Leader',))
        self.assertEqual(loaded.houses[1].teams, ())


class AsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_worker_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow(_request):
            started.set()
            while not release.is_set():
                time.sleep(0.001)
            return result()

        heartbeat = False
        with mock.patch.object(workers, 'load_house_show', side_effect=slow):
            task = asyncio.create_task(workers.run_house_show(request()))
            for _ in range(1000):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            heartbeat = True
            release.set()
            loaded = await asyncio.wait_for(task, timeout=1)

        self.assertTrue(heartbeat)
        self.assertEqual(loaded.selected_house_id, 1)


class PresentationTests(unittest.IsolatedAsyncioTestCase):
    def test_dense_show_and_paginated_list_preserve_house_information(self):
        loaded = result()
        detail = service.render_house_embed(loaded, 1)
        self.assertIn('House Ninjas', detail.title)
        self.assertIn('Leader', {field.value for field in detail.fields})
        self.assertTrue(any('1201 ELO' in field.value for field in detail.fields))
        listing = service.render_list_embed(loaded, 0)
        self.assertEqual(len(listing.fields), 2)
        self.assertIn('Page 1/1', listing.footer.text)

    async def test_workspace_is_requester_bound_and_refines_without_requery(self):
        loaded = result()
        view = service.HouseWorkspace(loaded, requester_id=10)
        rows = {}
        for child in view.children:
            rows.setdefault(child.row, []).append(child)
        self.assertTrue(any(
            len(children) == 1 and isinstance(children[0], discord.ui.Select)
            for children in rows.values()
        ))
        self.assertTrue(all(
            len(children) == 1 or all(
                isinstance(child, discord.ui.Button) for child in children
            )
            for children in rows.values()
        ))
        outsider = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(outsider))
        outsider.response.send_message.assert_awaited_once()

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(
                is_done=lambda: False,
                edit_message=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._next(interaction)
        interaction.response.edit_message.assert_awaited_once()
        self.assertIs(view.result, loaded)

    async def test_native_success_is_public_and_failure_stays_private(self):
        loaded = result()
        sent = SimpleNamespace(edit=mock.AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            channel=SimpleNamespace(send=mock.AsyncMock(return_value=sent)),
            delete_original_response=mock.AsyncMock(),
        )
        await service.publish_native(interaction, loaded, detail_house_id=1)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_house_uses_shared_worker_and_legacy_text_renderer(self):
        cog = league.league.__new__(league.league)
        guild = SimpleNamespace(id=300, roles=(), members=())

        @asynccontextmanager
        async def typing():
            yield

        ctx = SimpleNamespace(
            author=SimpleNamespace(id=10),
            guild=guild,
            channel=SimpleNamespace(id=400),
            prefix='!',
            invoked_with='house',
            typing=typing,
            send=mock.AsyncMock(),
        )
        command = next(
            command for command in league.league.__cog_commands__
            if command.name == 'house'
        )
        with mock.patch.object(
            service, 'build_request', return_value=request()
        ), mock.patch.object(
            workers,
            'run_house_show',
            new=mock.AsyncMock(return_value=result()),
        ) as run_worker, mock.patch.object(
            league.utilities, 'buffered_send', new=mock.AsyncMock()
        ) as send:
            await command.callback(cog, ctx, arg='Ninjas')

        run_worker.assert_awaited_once()
        self.assertIn('House Ninjas', send.await_args.kwargs['content'])

    async def test_slash_defers_before_worker_and_publishes_after_load(self):
        cog = league.league.__new__(league.league)
        guild = SimpleNamespace(id=300, roles=(), members=())
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=300,
            channel_id=400,
            channel=SimpleNamespace(),
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        events = []
        with mock.patch.object(
            service, 'native_access_error', return_value=None
        ), mock.patch.object(
            service, 'build_request', return_value=request()
        ), mock.patch.object(
            workers,
            'run_house_show',
            new=mock.AsyncMock(
                side_effect=lambda _request: events.append('worker') or result()
            ),
        ), mock.patch.object(
            service,
            'publish_native',
            new=mock.AsyncMock(
                side_effect=lambda *args, **kwargs: events.append('publish')
            ),
        ):
            original_defer = interaction.response.defer

            async def defer(**kwargs):
                events.append('defer')
                return await original_defer(**kwargs)

            interaction.response.defer = mock.AsyncMock(side_effect=defer)
            command = next(
                command for command in league.league.__cog_app_commands__
                if command.name == 'house'
            ).get_command('show')
            await command.callback(cog, interaction, 'Ninjas')

        self.assertEqual(events, ['defer', 'worker', 'publish'])

    async def test_database_failure_is_private_and_has_no_public_effect(self):
        cog = league.league.__new__(league.league)
        guild = SimpleNamespace(id=300, roles=(), members=())
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=300,
            channel_id=400,
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(
                defer=mock.AsyncMock(), send_message=mock.AsyncMock()
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(
            service, 'native_access_error', return_value=None
        ), mock.patch.object(
            service, 'build_request', return_value=request()
        ), mock.patch.object(
            workers,
            'run_house_show',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
        ), mock.patch.object(
            service, 'publish_native', new=mock.AsyncMock()
        ) as publish:
            command = next(
                command for command in league.league.__cog_app_commands__
                if command.name == 'house'
            ).get_command('list')
            await command.callback(cog, interaction)

        publish.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            'The House directory could not be loaded.', ephemeral=True
        )
