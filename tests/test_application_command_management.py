"""Offline safety tests for the explicit application-command manager."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest import mock

from modules.application_command_policy import (
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
        source = (FakeCommand('game'),)
        plans = plan_application_commands(
            policy,
            source,
            {20: ()},
            guild_ids=(10, 20),
        )

        class FakeTree:
            def __init__(self, guild):
                self.guild = guild
                self.added = []

            def add_command(self, command, *, guild):
                self.added.append((command.name, guild.id))

            async def sync(self, *, guild):
                self.sync_guild = guild
                return [SimpleNamespace(name=name) for name, _ in self.added]

        trees = []

        def scoped_tree(_client, desired, guild):
            tree = FakeTree(guild)
            for descriptor in desired:
                tree.add_command(descriptor.command, guild=guild)
            trees.append(tree)
            return tree

        with mock.patch.object(manager, '_scoped_tree', side_effect=scoped_tree):
            synced = asyncio.run(manager.apply_guild_plans(SimpleNamespace(), plans))

        # Guild 10 creates its root; guild 20 is default-deny but has no
        # current root, so no remote mutation is necessary for either plan.
        self.assertEqual(synced, {10: [SimpleNamespace(name='game')]})
        self.assertEqual(len(trees), 1)
        self.assertEqual(trees[0].sync_guild.id, 10)
        self.assertEqual(trees[0].added, [('game', 10)])

    def test_repeat_plan_is_unchanged_and_skips_remote_apply(self):
        policy = build_capability_policy({10: ('core_user',)}, [10])
        source = (FakeCommand('game'),)
        first = plan_application_commands(policy, source, guild_ids=(10,))[0]
        repeat = plan_application_commands(
            policy,
            source,
            {10: first.desired},
            guild_ids=(10,),
        )[0]

        self.assertFalse(repeat.diff.has_changes)
        with mock.patch.object(manager, '_scoped_tree') as scoped_tree:
            synced = asyncio.run(
                manager.apply_guild_plans(SimpleNamespace(), (repeat,))
            )
        self.assertEqual(synced, {})
        scoped_tree.assert_not_called()

    def test_manager_source_is_explicitly_not_the_bot_module(self):
        self.assertNotIn('import bot', manager.load_command_source.__doc__ or '')
        self.assertIn(
            '_model_free_command_imports',
            manager.load_command_source.__code__.co_names,
        )


if __name__ == '__main__':
    unittest.main()
