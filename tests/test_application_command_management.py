"""Offline safety tests for the explicit application-command manager."""

import asyncio
from contextlib import redirect_stdout
import io
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
from discord import app_commands
from discord.ext import commands

from modules.application_command_policy import (
    CapabilityFamily,
    build_capability_policy,
    plan_application_commands,
)
from scripts import manage_application_commands as manager


class FakeCommand:
    def __init__(self, name, version='v1'):
        self.name = name
        self._payload = {
            'name': name,
            'description': version,
            'options': [],
        }

    def to_dict(self, _tree=None):
        return dict(self._payload)


class ApplicationCommandManagementTests(unittest.TestCase):
    def test_target_guilds_default_to_allowlist_and_reject_unknowns(self):
        self.assertEqual(
            manager.validate_target_guilds(None, (30, 10)),
            (10, 30),
        )
        with self.assertRaisesRegex(manager.CommandManagementError, 'outside'):
            manager.validate_target_guilds((11,), (10,))
        with self.assertRaisesRegex(manager.CommandManagementError, 'explicit'):
            manager.validate_target_guilds(None, (10,), require_explicit=True)

    def test_apply_confirmation_requires_exact_environment_guilds_scope(self):
        kwargs = dict(
            selected_environment='development',
            expected_environment='development',
            selected_guild_ids=(10, 20),
            confirmed_guild_ids=(10, 20),
            confirmed_environment='development',
            scope='guild',
            confirmed_scope='guild',
            confirmed_no_global_sync=True,
        )
        manager.validate_apply_confirmation(**kwargs)

        for field, value, pattern in (
            ('confirmed_environment', 'production', 'environment'),
            ('confirmed_guild_ids', (10,), 'exact'),
            ('confirmed_scope', 'global', 'global'),
            ('confirmed_environment', None, 'environment'),
        ):
            changed = dict(kwargs)
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                    manager.CommandManagementError, pattern):
                manager.validate_apply_confirmation(**changed)

        changed = dict(kwargs, confirmed_no_global_sync=False)
        with self.assertRaisesRegex(manager.CommandManagementError, 'global'):
            manager.validate_apply_confirmation(**changed)

    def test_apply_only_syncs_changed_guilds_with_explicit_scope(self):
        policy = build_capability_policy({10: ('core_user',)}, [10, 20])
        source = tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        ))
        plans = plan_application_commands(
            policy,
            source,
            {20: ()},
            guild_ids=(10, 20),
        )

        class FakeTree:
            def __init__(self):
                self.added = []

            def clear_commands(self, *, guild):
                self.cleared_guild = guild

            def add_command(self, command, *, guild):
                self.added.append((command.name, guild.id))

            async def sync(self, *, guild):
                self.sync_guild = guild
                return [SimpleNamespace(name=name) for name, _ in self.added]

        tree = FakeTree()
        synced = asyncio.run(manager.apply_guild_plans(
            SimpleNamespace(tree=tree), plans
        ))

        # Guild 10 creates its root; guild 20 is default-deny but has no
        # current root, so no remote mutation is necessary for either plan.
        self.assertEqual(
            [command.name for command in synced[10]],
            ['game', 'leaderboard', 'player'],
        )
        self.assertEqual(tree.sync_guild.id, 10)
        self.assertEqual(tree.cleared_guild.id, 10)
        self.assertEqual(
            tree.added,
            [('game', 10), ('leaderboard', 10), ('player', 10)],
        )

    def test_repeat_plan_is_unchanged_and_skips_remote_apply(self):
        policy = build_capability_policy({10: ('core_user',)}, [10])
        source = tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        ))
        first = plan_application_commands(policy, source, guild_ids=(10,))[0]
        repeat = plan_application_commands(
            policy,
            source,
            {10: first.desired},
            guild_ids=(10,),
        )[0]

        self.assertFalse(repeat.diff.has_changes)
        tree = mock.Mock()
        synced = asyncio.run(
            manager.apply_guild_plans(SimpleNamespace(tree=tree), (repeat,))
        )
        self.assertEqual(synced, {})
        tree.clear_commands.assert_not_called()

    def test_remote_fetch_reads_global_then_each_explicit_guild(self):
        class FakeTree:
            def __init__(self):
                self.scopes = []

            async def fetch_commands(self, *, guild=None):
                self.scopes.append(None if guild is None else guild.id)
                if guild is None:
                    return [FakeCommand('stale-global')]
                return [FakeCommand(f'guild-{guild.id}')]

        tree = FakeTree()
        snapshot = asyncio.run(manager.fetch_remote_commands(
            SimpleNamespace(tree=tree),
            (20, 10),
        ))

        self.assertEqual(tree.scopes, [None, 20, 10])
        self.assertEqual(
            [command.name for command in snapshot.global_commands],
            ['stale-global'],
        )
        self.assertEqual(
            {
                guild_id: [command.name for command in commands]
                for guild_id, commands in snapshot.guild_commands.items()
            },
            {20: ['guild-20'], 10: ['guild-10']},
        )

    def test_remote_inspect_reports_nonempty_global_tree_without_mutation(self):
        client = SimpleNamespace(
            tree=SimpleNamespace(),
            login=mock.AsyncMock(),
            close=mock.AsyncMock(),
        )
        snapshot = manager.RemoteCommandSnapshot(
            global_commands=(FakeCommand('stale-global'),),
            guild_commands={10: ()},
        )
        policy = build_capability_policy({}, [10])

        output = io.StringIO()
        with mock.patch.object(
                manager,
                'fetch_remote_commands',
                new=mock.AsyncMock(return_value=snapshot),
        ), mock.patch.object(
                manager,
                'apply_guild_plans',
                new=mock.AsyncMock(),
        ) as apply, redirect_stdout(output):
            result = asyncio.run(manager._remote_mode(
                mode='inspect',
                profile=SimpleNamespace(discord_token='token'),
                source_client=client,
                source_commands=(),
                policy=policy,
                guild_ids=(10,),
            ))

        self.assertEqual(result, 0)
        self.assertIn('"current_roots": [\n      "stale-global"', output.getvalue())
        self.assertIn('"guild_apply_safe": false', output.getvalue())
        apply.assert_not_awaited()
        client.close.assert_awaited_once()

    def test_remote_apply_refuses_nonempty_global_tree_before_guild_sync(self):
        client = SimpleNamespace(
            tree=SimpleNamespace(),
            login=mock.AsyncMock(),
            close=mock.AsyncMock(),
        )
        snapshot = manager.RemoteCommandSnapshot(
            global_commands=(FakeCommand('stale-global'),),
            guild_commands={10: ()},
        )
        policy = build_capability_policy({}, [10])

        output = io.StringIO()
        with mock.patch.object(
                manager,
                'fetch_remote_commands',
                new=mock.AsyncMock(return_value=snapshot),
        ), mock.patch.object(
                manager,
                'apply_guild_plans',
                new=mock.AsyncMock(),
        ) as apply, redirect_stdout(output), self.assertRaisesRegex(
                manager.CommandManagementError, 'stale-global'):
            asyncio.run(manager._remote_mode(
                mode='apply',
                profile=SimpleNamespace(discord_token='token'),
                source_client=client,
                source_commands=(),
                policy=policy,
                guild_ids=(10,),
            ))

        self.assertIn('"guild_apply_safe": false', output.getvalue())
        apply.assert_not_awaited()
        client.close.assert_awaited_once()

    def test_remote_apply_with_empty_global_tree_preserves_guild_apply(self):
        client = SimpleNamespace(
            tree=SimpleNamespace(),
            login=mock.AsyncMock(),
            close=mock.AsyncMock(),
        )
        snapshot = manager.RemoteCommandSnapshot(
            global_commands=(),
            guild_commands={10: ()},
        )
        policy = build_capability_policy({}, [10])

        with mock.patch.object(
                manager,
                'fetch_remote_commands',
                new=mock.AsyncMock(return_value=snapshot),
        ), mock.patch.object(
                manager,
                'apply_guild_plans',
                new=mock.AsyncMock(return_value={}),
        ) as apply, redirect_stdout(io.StringIO()):
            result = asyncio.run(manager._remote_mode(
                mode='apply',
                profile=SimpleNamespace(discord_token='token'),
                source_client=client,
                source_commands=(),
                policy=policy,
                guild_ids=(10,),
            ))

        self.assertEqual(result, 0)
        apply.assert_awaited_once()
        client.close.assert_awaited_once()

    def test_apply_uses_existing_tree_prunes_and_preserves_other_scopes(self):
        async def callback(_interaction):
            return None

        client = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        global_command = app_commands.Command(
            name='global-only', description='global', callback=callback,
        )
        other_guild_command = app_commands.Command(
            name='other-only', description='other', callback=callback,
        )
        stale_target_command = app_commands.Command(
            name='stale-target', description='stale', callback=callback,
        )
        stale_empty_command = app_commands.Command(
            name='stale-empty', description='stale', callback=callback,
        )
        desired_command = app_commands.Command(
            name='game', description='game', callback=callback,
        )
        target_guild = discord.Object(id=10)
        empty_guild = discord.Object(id=20)
        other_guild = discord.Object(id=30)
        client.tree.add_command(global_command)
        client.tree.add_command(stale_target_command, guild=target_guild)
        client.tree.add_command(stale_empty_command, guild=empty_guild)
        client.tree.add_command(other_guild_command, guild=other_guild)
        policy = build_capability_policy(
            {10: ('test',)},
            [10, 20],
            families=(
                CapabilityFamily('test', ('game',)),
            ),
            available_roots=('game',),
        )
        plans = plan_application_commands(
            policy,
            (desired_command,),
            {
                10: client.tree.get_commands(guild=target_guild),
                20: client.tree.get_commands(guild=empty_guild),
            },
            guild_ids=(10, 20),
            tree=client.tree,
        )

        async def run_apply():
            sync_guild_ids = []

            async def network_sync(*, guild=None):
                if guild is None:
                    raise AssertionError('global command synchronization is forbidden')
                sync_guild_ids.append(guild.id)
                return list(client.tree.get_commands(guild=guild))

            with mock.patch.object(
                    client.tree,
                    'sync',
                    new=mock.AsyncMock(side_effect=network_sync),
            ) as sync:
                try:
                    synced = await manager.apply_guild_plans(client, plans)
                finally:
                    await client.close()
            return synced, sync, sync_guild_ids

        synced, sync, sync_guild_ids = asyncio.run(run_apply())

        # A real commands.Bot already owns this CommandTree. Applying plans
        # sequentially must replace only each selected guild's local state.
        self.assertEqual(sync_guild_ids, [10, 20])
        self.assertEqual(
            [call.kwargs['guild'].id for call in sync.await_args_list],
            [10, 20],
        )
        self.assertEqual(
            [command.name for command in synced[10]],
            ['game'],
        )
        self.assertEqual(synced[20], [])
        self.assertEqual(
            [command.name for command in client.tree.get_commands()],
            ['global-only'],
        )
        self.assertEqual(
            [command.name for command in client.tree.get_commands(guild=target_guild)],
            ['game'],
        )
        self.assertEqual(client.tree.get_commands(guild=empty_guild), [])
        self.assertEqual(
            [command.name for command in client.tree.get_commands(guild=other_guild)],
            ['other-only'],
        )
        self.assertIsNot(
            client.tree.get_commands(guild=target_guild)[0],
            desired_command,
        )

    def test_manager_source_is_explicitly_not_the_bot_module(self):
        self.assertNotIn('import bot', manager.load_command_source.__doc__ or '')
        self.assertIn(
            '_model_free_command_imports',
            manager.load_command_source.__code__.co_names,
        )


if __name__ == '__main__':
    unittest.main()
