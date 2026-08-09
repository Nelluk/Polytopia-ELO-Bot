"""Focused coverage for P8.10 native league-token workspace."""

from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_tokens_workers')
service = import_offline_runtime('modules.league_tokens')
views = import_offline_runtime('modules.league_tokens_views')
league = import_offline_runtime('modules.league')


class FakeDatabase:
    def __init__(self):
        self.connections = 0
        self.atomics = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.connections += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()

    def atomic(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.atomics += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()


class FakeQuery:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def order_by(self, *args):
        return self

    def limit(self, count):
        return FakeQuery(self.rows[:count])

    def __iter__(self):
        return iter(self.rows)


def read_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_level=1,
        league_scope=True,
        house_lookup=None,
    )
    values.update(overrides)
    return workers.LeagueTokensReadRequest(**values)


def read_result(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_level=1,
        houses=(
            workers.LeagueTokenHouse(7, 'Ninjas', '🥷', 3),
            workers.LeagueTokenHouse(8, 'Jets', '✈️', 5),
        ),
        logs=(
            workers.LeagueTokenLog(
                90,
                7,
                '2026-08-08 12:00:00',
                'Actor updated league tokens for House ID=7 Ninjas from 2 to 3',
            ),
        ),
        selected_house_id=None,
        houses_truncated=False,
        logs_truncated=False,
    )
    values.update(overrides)
    return workers.LeagueTokensReadResult(**values)


def mutation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_level=5,
        league_scope=True,
        house_id=7,
        expected_house_name='Ninjas',
        expected_balance=3,
        new_balance=4,
        note='Weekly award',
        requester_description='**Actor** (`10`)',
    )
    values.update(overrides)
    return workers.LeagueTokensMutationRequest(**values)


class RegistrationTests(unittest.TestCase):
    def test_native_shape_and_prefix_retirement(self):
        root = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )
        self.assertEqual({command.name for command in root.commands}, {'tokens'})
        command = root.get_command('tokens')
        self.assertEqual(
            [
                (parameter.name, parameter.required, parameter.type)
                for parameter in command.parameters
            ],
            [
                ('house', False, discord.AppCommandOptionType.string),
                ('amount', False, discord.AppCommandOptionType.integer),
                ('note', False, discord.AppCommandOptionType.string),
            ],
        )
        self.assertNotIn(
            'tokens',
            {command.name for command in league.league.__cog_commands__},
        )

    def test_native_access_preserves_broad_read_scope_without_channel_gate(self):
        member = SimpleNamespace(id=10)
        with mock.patch.object(service.house_show, '_league_scope', return_value=True):
            self.assertIsNone(service.native_access_error(member, 300))
        with mock.patch.object(service.house_show, '_league_scope', return_value=False):
            self.assertIn('configured league server', service.native_access_error(member, 300))


class WorkerTests(unittest.TestCase):
    def test_requests_and_results_are_frozen_primitives(self):
        request = read_request()
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 1
        self.assertIsInstance(read_result().houses, tuple)

    def test_read_loads_balances_and_both_historical_log_markers(self):
        houses = (
            SimpleNamespace(id=7, name='Ninjas', emoji='🥷', league_tokens=3),
            SimpleNamespace(id=8, name='Jets', emoji='', league_tokens=5),
        )
        logs = (
            SimpleNamespace(
                id=90,
                message_ts='newer',
                message='Actor updated league tokens for House ID=7 Ninjas',
            ),
            SimpleNamespace(
                id=89,
                message_ts='older',
                message='Historical FATS id=8 update',
            ),
        )
        database = FakeDatabase()
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.House, 'select', return_value=FakeQuery(houses)
        ), mock.patch.object(workers, '_token_log_query', return_value=logs):
            result = workers.load_league_tokens(read_request(house_lookup='nin'))
        self.assertEqual(result.selected_house_id, 7)
        self.assertEqual([row.balance for row in result.houses], [3, 5])
        self.assertEqual([row.house_id for row in result.logs], [7, 8])
        self.assertEqual(database.connections, 1)

    def test_read_is_broad_but_update_requires_level_five(self):
        database = FakeDatabase()
        house = SimpleNamespace(id=7, name='Ninjas', league_tokens=3, save=mock.Mock())
        log = SimpleNamespace(id=91, message_ts='now')
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.House, 'get_by_id', return_value=house
        ), mock.patch.object(workers.models.GameLog, 'write', return_value=log):
            with self.assertRaises(workers.LeagueTokensPermissionError):
                workers.mutate_league_tokens(mutation_request(requester_level=4))
            result = workers.mutate_league_tokens(mutation_request(requester_level=5))
        self.assertEqual(result.new_balance, 4)
        self.assertEqual(house.league_tokens, 4)
        self.assertEqual(database.connections, 1)
        self.assertEqual(database.atomics, 1)

    def test_helper_level_update_is_atomic_and_actor_attributed(self):
        database = FakeDatabase()
        house = SimpleNamespace(id=7, name='Ninjas', league_tokens=3, save=mock.Mock())
        log = SimpleNamespace(id=91, message_ts='now')
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.House, 'get_by_id', return_value=house
        ), mock.patch.object(
            workers.models.GameLog, 'write', return_value=log
        ) as write:
            result = workers.mutate_league_tokens(mutation_request())
        message = write.call_args.kwargs['message']
        self.assertIn('**Actor** (`10`)', message)
        self.assertIn('House ID=7', message)
        self.assertIn('Weekly award', message)
        self.assertEqual(result.old_balance, 3)
        self.assertEqual(database.atomics, 1)

    def test_conflict_prevents_update_and_audit(self):
        database = FakeDatabase()
        house = SimpleNamespace(id=7, name='Ninjas', league_tokens=9, save=mock.Mock())
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.House, 'get_by_id', return_value=house
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            with self.assertRaises(workers.LeagueTokensConflictError):
                workers.mutate_league_tokens(mutation_request())
        house.save.assert_not_called()
        write.assert_not_called()
        self.assertEqual(database.atomics, 0)

    def test_note_and_smallint_bounds_are_validated_before_connection(self):
        with self.assertRaises(workers.LeagueTokensValidationError):
            workers.mutate_league_tokens(
                mutation_request(new_balance=workers.MAX_TOKEN_BALANCE + 1)
            )
        with self.assertRaises(workers.LeagueTokensValidationError):
            workers.validate_note('x' * (workers.MAX_NOTE_LENGTH + 1))


class ServiceAndViewTests(unittest.IsolatedAsyncioTestCase):
    def test_apply_mutation_updates_loaded_snapshot_without_requery(self):
        mutation = workers.LeagueTokensMutationResult(
            guild_id=300,
            house_id=7,
            house_name='Ninjas',
            old_balance=3,
            new_balance=6,
            note='Award',
            log_id=99,
            timestamp='now',
            audit_message='Actor updated league tokens for House ID=7',
        )
        updated = service.apply_mutation(read_result(), mutation)
        self.assertEqual(updated.selected_house_id, 7)
        self.assertEqual(updated.houses[0].balance, 6)
        self.assertEqual(updated.logs[0].log_id, 99)

    def test_workspace_is_requester_bound_paginated_and_serializable(self):
        houses = tuple(
            workers.LeagueTokenHouse(index, f'House {index}', '', index)
            for index in range(1, 13)
        )
        view = views.LeagueTokensWorkspace(
            result=read_result(houses=houses),
            requester_id=10,
        )
        self.assertEqual(view.page_count, 2)
        self.assertEqual(view.requester_id, 10)
        payload = view.to_components()
        self.assertTrue(payload)
        self.assertIn('League tokens', str(payload))

    async def test_publish_clears_private_ack_and_sends_public_workspace(self):
        message = SimpleNamespace(edit=mock.AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock(return_value=message)),
        )
        view = views.LeagueTokensWorkspace(
            result=read_result(), requester_id=10
        )
        published = await views.publish(interaction, view)
        self.assertIs(published, message)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once_with(view=view)
        self.assertIs(view.message, message)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, *, level=1):
        return SimpleNamespace(
            guild=SimpleNamespace(id=300),
            guild_id=300,
            channel_id=400,
            channel=SimpleNamespace(send=mock.AsyncMock()),
            user=SimpleNamespace(
                id=10,
                roles=(),
                display_name='Actor',
                mention='<@10>',
                level=level,
            ),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

    @staticmethod
    def _command():
        return next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        ).get_command('tokens')

    async def test_broad_read_defers_loads_then_publishes_publicly(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction(level=1)
        events = []
        original = interaction.response.defer

        async def defer(**kwargs):
            events.append('defer')
            return await original(**kwargs)

        interaction.response.defer = mock.AsyncMock(side_effect=defer)
        with mock.patch.object(service, 'native_access_error', return_value=None), mock.patch.object(
            service, 'build_read_request', return_value=read_request()
        ), mock.patch.object(
            service,
            'run_read',
            new=mock.AsyncMock(side_effect=lambda _request: events.append('read') or read_result()),
        ), mock.patch.object(
            views,
            'publish',
            new=mock.AsyncMock(side_effect=lambda *_args: events.append('publish')),
        ):
            await self._command().callback(cog, interaction, None, None, None)
        self.assertEqual(events, ['defer', 'read', 'publish'])

    async def test_update_commits_before_public_workspace(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction(level=5)
        current = read_result(selected_house_id=7, requester_level=5)
        mutation = workers.LeagueTokensMutationResult(
            guild_id=300,
            house_id=7,
            house_name='Ninjas',
            old_balance=3,
            new_balance=4,
            note='Award',
            log_id=99,
            timestamp='now',
            audit_message='Actor updated league tokens for House ID=7',
        )
        events = []
        with mock.patch.object(service, 'native_access_error', return_value=None), mock.patch.object(
            service, 'build_read_request', return_value=read_request(house_lookup='Ninjas')
        ), mock.patch.object(
            service, 'run_read', new=mock.AsyncMock(return_value=current)
        ), mock.patch.object(
            service, 'build_mutation_request', return_value=mutation_request()
        ), mock.patch.object(
            service,
            'run_mutation',
            new=mock.AsyncMock(side_effect=lambda _request: events.append('commit') or mutation),
        ), mock.patch.object(
            views,
            'publish',
            new=mock.AsyncMock(side_effect=lambda *_args: events.append('publish')),
        ):
            await self._command().callback(cog, interaction, 'Ninjas', 4, 'Award')
        self.assertEqual(events, ['commit', 'publish'])

    async def test_committed_publication_failure_is_terminal_reconciliation(self):
        cog = league.league.__new__(league.league)
        interaction = self._interaction(level=5)
        current = read_result(selected_house_id=7, requester_level=5)
        mutation = workers.LeagueTokensMutationResult(
            guild_id=300,
            house_id=7,
            house_name='Ninjas',
            old_balance=3,
            new_balance=4,
            note=None,
            log_id=99,
            timestamp='now',
            audit_message='Actor updated league tokens for House ID=7',
        )
        with mock.patch.object(service, 'native_access_error', return_value=None), mock.patch.object(
            service, 'build_read_request', return_value=read_request(house_lookup='Ninjas')
        ), mock.patch.object(service, 'run_read', new=mock.AsyncMock(return_value=current)), mock.patch.object(
            service, 'build_mutation_request', return_value=mutation_request(note=None)
        ), mock.patch.object(service, 'run_mutation', new=mock.AsyncMock(return_value=mutation)), mock.patch.object(
            views,
            'publish',
            new=mock.AsyncMock(side_effect=workers.LeagueTokensPublicationError('failed')),
        ):
            await self._command().callback(cog, interaction, 'Ninjas', 4, None)
        message = interaction.followup.send.await_args.args[0]
        self.assertIn('were committed', message)
        self.assertIn('Do not retry', message)
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])


if __name__ == '__main__':
    unittest.main()
