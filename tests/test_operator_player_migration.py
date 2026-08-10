"""Focused offline coverage for P9.4 configured-superuser migration."""

from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.operator_player_migration_workers')
service = import_offline_runtime('modules.operator_player_migration')
views = import_offline_runtime('modules.operator_player_migration_views')
administration = import_offline_runtime('modules.administration')


def preview(**overrides):
    values = dict(
        guild_id=300,
        source_id=100,
        source_name='Old Name',
        destination_id=200,
        destination_name='New Name',
        destination_exists=True,
        destination_completed_games=0,
        destination_metadata=('canonical timezone',),
        guilds=(workers.PlayerMigrationGuildPreview(
            guild_id=300,
            source_player_id=1,
            destination_player_id=2,
            disposition='merge destination player into source player',
            source_team_id=10,
            destination_team_id=10,
            lineups=2,
            hosted_games=1,
            squad_memberships=1,
            house_preferences=1,
            bids=2,
        ),),
        blockers=(),
        fingerprint='abc',
    )
    values.update(overrides)
    return workers.PlayerMigrationPreview(**values)


class FakeConnectionDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        outer = self

        class Context(AbstractContextManager):
            def __enter__(self):
                outer.opened += 1

            def __exit__(self, *_args):
                outer.closed += 1

        return Context()


class PlayerMigrationWorkerBoundaryTests(unittest.TestCase):
    def test_requests_and_results_are_frozen_primitive_values(self):
        request = workers.PlayerMigrationCommitRequest(
            guild_id=300,
            requester_id=10,
            requester_description='**Operator** (`10`)',
            source_id=100,
            destination_id=200,
            destination_name='New Name',
            expected_fingerprint='abc',
        )
        with self.assertRaises(FrozenInstanceError):
            request.source_id = 999
        self.assertNotIn('Member', repr(request))
        self.assertNotIn('Model', repr(request))

    def test_fingerprint_is_deterministic_and_state_sensitive(self):
        left = workers._fingerprint({'players': [(1, 2)], 'member': (3, 4)})
        reordered = workers._fingerprint({'member': (3, 4), 'players': [(1, 2)]})
        changed = workers._fingerprint({'players': [(1, 9)], 'member': (3, 4)})
        self.assertEqual(left, reordered)
        self.assertNotEqual(left, changed)

    def test_worker_revalidates_superuser_before_graph_load(self):
        database = FakeConnectionDatabase()
        request = workers.PlayerMigrationPreviewRequest(
            guild_id=300,
            requester_id=99,
            source_id=100,
            destination_id=200,
            destination_name='New Name',
        )
        with mock.patch.object(workers.models, 'db', database), \
                mock.patch.object(workers.settings, 'superuser_ids', (10,)), \
                mock.patch.object(workers, '_build_graph') as build:
            with self.assertRaises(workers.PlayerMigrationPermissionError):
                workers.load_preview(request)
        build.assert_not_called()
        self.assertEqual((database.opened, database.closed), (1, 1))

    def test_preview_returns_the_single_authoritative_graph_snapshot(self):
        database = FakeConnectionDatabase()
        expected = preview()
        request = workers.PlayerMigrationPreviewRequest(
            guild_id=300,
            requester_id=10,
            source_id=100,
            destination_id=200,
            destination_name='New Name',
        )
        graph = SimpleNamespace(preview=expected)
        with mock.patch.object(workers.models, 'db', database), \
                mock.patch.object(workers.settings, 'superuser_ids', (10,)), \
                mock.patch.object(workers, '_build_graph', return_value=graph):
            self.assertIs(workers.load_preview(request), expected)
        self.assertEqual((database.opened, database.closed), (1, 1))


class PlayerMigrationViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_blockers_disable_confirm_and_cancel_is_requester_bound(self):
        view = views.PlayerMigrationPreviewView(
            requester_id=10,
            preview=preview(blockers=('Same game overlap.',)),
            confirmer=mock.AsyncMock(),
        )
        buttons = [
            item for item in view.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        self.assertTrue(next(item for item in buttons if item.label == 'Confirm migration').disabled)

        denial = mock.AsyncMock()
        allowed = await view.interaction_check(SimpleNamespace(
            user=SimpleNamespace(id=11),
            response=SimpleNamespace(send_message=denial),
        ))
        self.assertFalse(allowed)
        denial.assert_awaited_once_with(
            'Only the requesting superuser can control this preview.',
            ephemeral=True,
        )

    async def test_precommit_failure_restores_retryable_preview(self):
        confirmer = mock.AsyncMock(
            side_effect=workers.PlayerMigrationStaleError('Reload preview.')
        )
        view = views.PlayerMigrationPreviewView(
            requester_id=10,
            preview=preview(),
            confirmer=confirmer,
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        await view._confirm(interaction)
        self.assertFalse(view.finished)
        self.assertFalse(view.busy)
        self.assertEqual(view.status, 'Reload preview.')
        interaction.edit_original_response.assert_awaited_once_with(view=view)


class PlayerMigrationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_success_is_actor_attributed_after_private_cleanup(self):
        events = []

        async def clear():
            events.append('clear')

        async def send(content, **kwargs):
            events.append(('public', content, kwargs))

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10, display_name='Operator', name='Operator'),
            delete_original_response=clear,
            channel=SimpleNamespace(send=send),
        )
        result = workers.PlayerMigrationResult(
            guild_id=300,
            source_id=100,
            source_name='Old',
            destination_id=200,
            destination_name='New',
            destination_identity_removed=True,
            players_reparented=1,
            players_merged=1,
            lineups_reassigned=2,
            hosts_reassigned=1,
            squad_memberships_reassigned=1,
            squad_memberships_deduplicated=0,
            house_preferences_reassigned=1,
            house_preferences_deduplicated=0,
            bids_reassigned=2,
            player_names_refreshed=2,
        )
        await service.publish_result(interaction, result)
        self.assertEqual(events[0], 'clear')
        self.assertIn('Operator', events[1][1])
        self.assertIn('Old', events[1][1])
        self.assertIn('New', events[1][1])


class PlayerMigrationAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(administration.administration)
        self.operator = next(
            command for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.command = self.operator.get_command('player').get_command('migrate')

    def test_exact_nested_shape_and_prefix_retirement(self):
        self.assertEqual(
            [(parameter.name, parameter.required) for parameter in self.command.parameters],
            [('source_id', True), ('destination', True)],
        )
        prefix_names = {
            name
            for command in administration.administration.__cog_commands__
            for name in (command.name, *command.aliases)
        }
        self.assertNotIn('migrate_player', prefix_names)
        self.assertNotIn('migrate', prefix_names)

    async def test_non_superuser_and_self_migration_fail_privately_before_defer(self):
        destination = SimpleNamespace(id=200, bot=False, name='New')
        response = SimpleNamespace(
            send_message=mock.AsyncMock(),
            defer=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=99),
            response=response,
        )
        with mock.patch.object(administration.settings, 'superuser_ids', (10,)):
            await self.command.callback(self.cog, interaction, '100', destination)
        response.send_message.assert_awaited_once_with(
            'Only a configured bot superuser can migrate players.',
            ephemeral=True,
        )
        response.defer.assert_not_awaited()

    async def test_valid_request_defers_before_preview_worker(self):
        events = []

        async def defer(**kwargs):
            events.append(('defer', kwargs))

        async def run_preview(_request):
            events.append('worker')
            return preview()

        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=10, display_name='Operator', name='Operator'),
            response=SimpleNamespace(defer=defer, send_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            original_response=mock.AsyncMock(return_value=SimpleNamespace()),
        )
        destination = SimpleNamespace(
            id=223456789012345678,
            bot=False,
            name='New Name',
        )
        with mock.patch.object(administration.settings, 'superuser_ids', (10,)), \
                mock.patch.object(
                    administration.operator_player_migration_workers,
                    'run_preview',
                    new=run_preview,
                ):
            await self.command.callback(
                self.cog,
                interaction,
                '123456789012345678',
                destination,
            )
        self.assertEqual(events[:2], [('defer', {'ephemeral': True}), 'worker'])
        interaction.edit_original_response.assert_awaited_once()
