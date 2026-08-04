"""Offline tests for P8.0 capability policy and guild planning."""

from dataclasses import FrozenInstanceError
import unittest

from modules.application_command_policy import (
    ApplicationCommandPolicyError,
    CapabilityFamily,
    CommandDescriptor,
    build_capability_policy,
    describe_command,
    plan_application_commands,
)


class FakeCommand:
    def __init__(self, name, version='v1', **extra):
        self.name = name
        self._payload = {
            'id': extra.pop('id', None),
            'application_id': extra.pop('application_id', None),
            'name': name,
            'description': version,
            'options': extra,
        }

    def to_dict(self, _tree=None):
        return dict(self._payload)


class ApplicationCommandPolicyTests(unittest.TestCase):
    @staticmethod
    def core_source():
        return tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        ))

    def test_default_deny_and_immutable_policy(self):
        policy = build_capability_policy({}, [20, 10])

        self.assertEqual(policy.allowed_guild_ids, (10, 20))
        self.assertEqual(policy.roots_for_guild(10), ())
        self.assertNotIn('team', policy.roots_for_guild(10))
        self.assertNotIn('staffhelp', policy.roots_for_guild(10))
        self.assertEqual(policy.assigned_guild_ids(), ())
        with self.assertRaises(FrozenInstanceError):
            policy.allowed_guild_ids = (1,)

    def test_assignments_are_deterministic_and_expand_root_membership(self):
        policy = build_capability_policy({
            20: ('elo_maintenance', 'core_user'),
            10: ('core_user',),
        }, [10, 20])

        self.assertEqual(policy.assigned_guild_ids(), (10, 20))
        self.assertEqual(
            policy.capabilities_for_guild(20),
            ('core_user', 'elo_maintenance'),
        )
        self.assertEqual(
            policy.roots_for_guild(20),
            ('elo', 'game', 'leaderboard', 'player'),
        )
        self.assertEqual(policy.roots_for_guild(10), (
            'game', 'leaderboard', 'player',
        ))

        tools_policy = build_capability_policy({
            10: ('tools_support',),
        }, [10])
        self.assertEqual(
            tools_policy.roots_for_guild(10),
            ('staffhelp',),
        )

        beta_policy = build_capability_policy({
            10: ('beta_testing',),
        }, [10])
        self.assertEqual(beta_policy.roots_for_guild(10), ('whattotest',))

    def test_unknown_guild_and_capability_are_rejected(self):
        with self.assertRaisesRegex(ApplicationCommandPolicyError, 'not in'):
            build_capability_policy({30: ('core_user',)}, [10, 20])
        with self.assertRaisesRegex(ApplicationCommandPolicyError, 'unknown'):
            build_capability_policy({10: ('not_a_capability',)}, [10])

    def test_duplicate_and_operator_only_assignments_are_rejected(self):
        with self.assertRaisesRegex(ApplicationCommandPolicyError, 'duplicate'):
            build_capability_policy({10: ('core_user', 'core_user')}, [10])
        with self.assertRaisesRegex(ApplicationCommandPolicyError, 'operator'):
            build_capability_policy({10: ('operator_only',)}, [10])

    def test_unknown_and_conflicting_roots_are_rejected(self):
        with self.assertRaisesRegex(ApplicationCommandPolicyError, 'unknown root'):
            build_capability_policy(
                {},
                [10],
                families=(CapabilityFamily('future', ('new-root',)),),
            )
        with self.assertRaisesRegex(ApplicationCommandPolicyError, 'conflicting'):
            build_capability_policy(
                {},
                [10],
                families=(
                    CapabilityFamily('one', ('game',)),
                    CapabilityFamily('two', ('game',)),
                ),
            )

    def test_command_fingerprint_ignores_remote_runtime_ids(self):
        local = describe_command(FakeCommand('game', 'same'))
        remote = describe_command(
            FakeCommand(
                'game',
                'same',
                id=999,
                application_id=888,
            )
        )
        self.assertEqual(local.fingerprint, remote.fingerprint)

    def test_command_fingerprint_normalizes_discord_response_defaults(self):
        local = FakeCommand('game')
        local._payload.update({
            'dm_permission': False,
            'options': [{
                'name': 'show',
                'description': 'Show a game.',
                'type': 1,
                'options': [{
                    'name': 'game_id',
                    'description': 'Game ID.',
                    'type': 4,
                    'required': False,
                }],
            }],
        })
        remote = FakeCommand('game', id=999, application_id=888)
        remote._payload.update({
            'dm_permission': True,
            'contexts': None,
            'integration_types': None,
            'name_localizations': {},
            'description_localizations': {},
            'options': [{
                'name': 'show',
                'description': 'Show a game.',
                'type': 1,
                'name_localizations': {},
                'description_localizations': {},
                'options': [{
                    'name': 'game_id',
                    'description': 'Game ID.',
                    'type': 4,
                    'required': False,
                    'autocomplete': False,
                    'choices': [],
                    'channel_types': [],
                    'options': [],
                    'min_value': None,
                    'max_value': None,
                }],
            }],
        })

        self.assertEqual(
            describe_command(local).fingerprint,
            describe_command(remote).fingerprint,
        )

    def test_deterministic_create_update_unchanged_and_remove_diff(self):
        policy = build_capability_policy({
            10: ('core_user',),
        }, [10])
        source = (
            FakeCommand('leaderboard', 'same'),
            FakeCommand('game', 'new'),
            FakeCommand('player', 'same'),
        )
        current = (
            FakeCommand('obsolete', 'old'),
            FakeCommand('game', 'old'),
            FakeCommand('player', 'same'),
        )

        plan = plan_application_commands(
            policy,
            source,
            {10: current},
        )[0]

        self.assertEqual(plan.diff.creates, ('leaderboard',))
        self.assertEqual(plan.diff.updates, ('game',))
        self.assertEqual(plan.diff.unchanged, ('player',))
        self.assertEqual(plan.diff.removals, ('obsolete',))
        self.assertTrue(plan.diff.has_changes)

    def test_unassigned_allowed_guilds_are_planned_for_pruning(self):
        policy = build_capability_policy({10: ('core_user',)}, [10, 20])
        source = self.core_source()

        plans = plan_application_commands(
            policy,
            source,
            {20: (FakeCommand('game'),)},
        )

        self.assertEqual([plan.guild_id for plan in plans], [10, 20])
        self.assertEqual(
            plans[0].diff.creates,
            ('game', 'leaderboard', 'player'),
        )
        self.assertEqual(plans[1].diff.removals, ('game',))

    def test_guild_plans_do_not_leak_capabilities_or_mutate_source(self):
        policy = build_capability_policy({
            10: ('core_user',),
            20: ('elo_maintenance',),
        }, [10, 20])
        source = self.core_source() + (FakeCommand('elo'),)

        plans = plan_application_commands(policy, source)

        self.assertEqual(
            tuple(item.name for item in plans[0].desired),
            ('game', 'leaderboard', 'player'),
        )
        self.assertEqual(
            tuple(item.name for item in plans[1].desired), ('elo',)
        )
        self.assertEqual(
            tuple(command.name for command in source),
            ('game', 'leaderboard', 'player', 'elo'),
        )

    def test_selected_guilds_must_stay_within_runtime_allowlist(self):
        policy = build_capability_policy({}, [10])
        with self.assertRaisesRegex(ApplicationCommandPolicyError, 'outside'):
            plan_application_commands(
                policy,
                (),
                guild_ids=(11,),
            )

    def test_many_guild_assignments_need_no_per_guild_policy_code(self):
        guild_ids = tuple(range(100, 120))
        policy = build_capability_policy(
            {guild_id: ('core_user',) for guild_id in guild_ids},
            guild_ids,
        )

        self.assertEqual(len(policy.assignments), 20)
        self.assertTrue(all(
            policy.roots_for_guild(guild_id) == (
                'game', 'leaderboard', 'player',
            )
            for guild_id in guild_ids
        ))

    def test_command_descriptor_is_immutable(self):
        descriptor = describe_command(FakeCommand('game'))
        self.assertIsInstance(descriptor, CommandDescriptor)
        with self.assertRaises(FrozenInstanceError):
            descriptor.name = 'elo'

    def test_assigned_but_unloaded_root_is_rejected(self):
        policy = build_capability_policy({10: ('core_user',)}, [10])

        with self.assertRaisesRegex(
                ApplicationCommandPolicyError, 'not present.*leaderboard'):
            plan_application_commands(
                policy,
                (FakeCommand('game'), FakeCommand('player')),
            )

    def test_unassigned_future_roots_need_not_be_loaded(self):
        policy = build_capability_policy({}, [10])

        plans = plan_application_commands(policy, ())

        self.assertEqual(plans[0].desired, ())


if __name__ == '__main__':
    unittest.main()
