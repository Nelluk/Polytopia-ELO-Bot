"""Focused safety coverage for owned guided Beta Lab personas."""

import contextlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


personas = import_offline_runtime('modules.beta_lab_personas')
persona_manifest = import_offline_runtime('modules.beta_lab_persona_manifest')


class FakeRole:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name
        self.permissions = SimpleNamespace(value=0)
        self.managed = False
        self.hoist = False
        self.mentionable = False
        self.members = []
        self.delete = mock.AsyncMock()

    def is_assignable(self):
        return True


class FakeGuild:
    def __init__(self, roles=()):
        self.id = persona_manifest.EXPECTED_GUILD_ID
        self.roles = list(roles)
        self.created = []

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    async def create_role(self, **kwargs):
        role = FakeRole(900 + len(self.created), kwargs['name'])
        self.created.append((role, kwargs))
        self.roles.append(role)
        return role


class FakeMember:
    def __init__(self, member_id, roles=()):
        self.id = member_id
        self.roles = list(roles)

    async def add_roles(self, *roles, **_kwargs):
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)
                role.members.append(self)

    async def remove_roles(self, *roles, **_kwargs):
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)
            if self in role.members:
                role.members.remove(self)


class PersonaManifestTests(unittest.TestCase):
    def test_manifest_is_exact(self):
        value = {
            'schema_version': 1,
            'guild_id': persona_manifest.EXPECTED_GUILD_ID,
            'tester_role_id': persona_manifest.EXPECTED_TESTER_ROLE_ID,
            'house_name': 'Beta Lab House',
            'team_name': 'Beta Lab Team',
            'staff_role_name': 'Beta Lab Staff',
        }
        result = persona_manifest.validate(value)
        self.assertEqual(result.team_name, 'Beta Lab Team')
        for field in ('guild_id', 'team_name', 'staff_role_name'):
            changed = dict(value)
            changed[field] = 999 if field == 'guild_id' else 'wrong'
            with self.assertRaises(persona_manifest.BetaLabPersonaManifestError):
                persona_manifest.validate(changed)


class PersonaRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = SimpleNamespace(project_root=Path('/tmp/project'), log_root=Path('/tmp/log'))
        self.policy = persona_manifest.BetaLabPersonaManifest(
            guild_id=persona_manifest.EXPECTED_GUILD_ID,
            tester_role_id=persona_manifest.EXPECTED_TESTER_ROLE_ID,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
            staff_role_name='Beta Lab Staff',
        )

    async def test_setup_creates_only_zero_permission_owned_roles(self):
        guild = FakeGuild()
        state = {}

        def write(_profile, _filename, value):
            state.update(value)

        with mock.patch.object(personas, 'manifest', return_value=self.policy), \
                mock.patch.object(personas.beta_operations, 'assert_beta_profile'), \
                mock.patch.object(personas, '_read_state', side_effect=lambda *_args: state or None), \
                mock.patch.object(personas, '_write_state', side_effect=write):
            result = await personas.setup_roles(self.profile, guild)
        self.assertEqual((result.team_role_id, result.staff_role_id), (900, 901))
        self.assertEqual([item[1]['permissions'].value for item in guild.created], [0, 0])
        self.assertTrue(all(not item[1]['hoist'] for item in guild.created))
        self.assertTrue(all(not item[1]['mentionable'] for item in guild.created))

    async def test_setup_refuses_unowned_name_and_compensates_partial_create(self):
        conflict = FakeGuild((FakeRole(1, self.policy.team_name),))
        with mock.patch.object(personas, 'manifest', return_value=self.policy), \
                mock.patch.object(personas.beta_operations, 'assert_beta_profile'), \
                mock.patch.object(personas, '_read_state', return_value=None):
            with self.assertRaises(personas.BetaLabPersonaError):
                await personas.setup_roles(self.profile, conflict)

        guild = FakeGuild()

        async def create(**kwargs):
            if guild.created:
                raise RuntimeError('second create failed')
            role = FakeRole(900, kwargs['name'])
            guild.created.append((role, kwargs))
            guild.roles.append(role)
            return role

        guild.create_role = create
        with mock.patch.object(personas, 'manifest', return_value=self.policy), \
                mock.patch.object(personas.beta_operations, 'assert_beta_profile'), \
                mock.patch.object(personas, '_read_state', return_value=None):
            with self.assertRaises(personas.BetaLabPersonaError):
                await personas.setup_roles(self.profile, guild)
        guild.created[0][0].delete.assert_awaited_once()

    async def test_member_assignment_and_orphan_reconciliation_are_exact(self):
        team = FakeRole(900, self.policy.team_name)
        staff = FakeRole(901, self.policy.staff_role_name)
        unrelated = FakeRole(902, 'Unrelated')
        guild = FakeGuild((team, staff, unrelated))
        binding = personas.PersonaRoleBinding(900, 901)
        active = FakeMember(10, (unrelated,))
        stray = FakeMember(11, (team, staff, unrelated))
        team.members.append(stray)
        staff.members.append(stray)
        with mock.patch.object(personas, 'load_role_binding', return_value=binding):
            await personas.set_member_active(self.profile, guild, active, active=True)
            await personas.reconcile_members(
                self.profile, guild, active_owner_ids=(10,),
            )
        self.assertEqual({role.id for role in active.roles}, {900, 901, 902})
        self.assertEqual({role.id for role in stray.roles}, {902})

    async def test_reconciliation_refuses_unbounded_role_membership(self):
        team = FakeRole(900, self.policy.team_name)
        staff = FakeRole(901, self.policy.staff_role_name)
        guild = FakeGuild((team, staff))
        team.members = [FakeMember(value, (team,)) for value in range(25)]
        with mock.patch.object(
            personas, 'load_role_binding',
            return_value=personas.PersonaRoleBinding(900, 901),
        ):
            with self.assertRaises(personas.BetaLabPersonaError):
                await personas.reconcile_members(
                    self.profile, guild, active_owner_ids=(),
                )

    async def test_startup_revokes_every_owned_persona_and_is_idempotent(self):
        team = FakeRole(900, self.policy.team_name)
        staff = FakeRole(901, self.policy.staff_role_name)
        member = FakeMember(10, (team, staff))
        team.members.append(member)
        staff.members.append(member)
        guild = FakeGuild((team, staff))
        with mock.patch.object(personas, '_read_state', return_value={'owned': True}), \
                mock.patch.object(
                    personas,
                    'load_role_binding',
                    return_value=personas.PersonaRoleBinding(900, 901),
                ):
            self.assertEqual(
                await personas.revoke_members_on_startup(self.profile, guild),
                1,
            )
            self.assertEqual(
                await personas.revoke_members_on_startup(self.profile, guild),
                0,
            )
        self.assertEqual(member.roles, [])

    async def test_unassignable_owned_role_is_refused(self):
        team = FakeRole(900, self.policy.team_name)
        staff = FakeRole(901, self.policy.staff_role_name)
        staff.is_assignable = lambda: False
        guild = FakeGuild((team, staff))
        state = {
            'schema_version': 1,
            'guild_id': self.policy.guild_id,
            'team_role_id': 900,
            'team_role_name': self.policy.team_name,
            'staff_role_id': 901,
            'staff_role_name': self.policy.staff_role_name,
        }
        with mock.patch.object(personas, 'manifest', return_value=self.policy), \
                mock.patch.object(personas, '_read_state', return_value=state):
            with self.assertRaisesRegex(
                personas.BetaLabPersonaError, 'cannot assign',
            ):
                personas.load_role_binding(self.profile, guild)


class PersonaDatabaseTests(unittest.TestCase):
    def test_seed_requires_role_evidence_and_stopped_writer_scope(self):
        profile = object()
        with mock.patch.object(personas, 'manifest') as policy, \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_read_state', return_value=None), \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope') as writer:
            policy.return_value = SimpleNamespace(guild_id=300)
            with self.assertRaises(personas.BetaLabPersonaError):
                personas.seed_database(profile)
        writer.assert_not_called()

    def test_seed_refuses_malformed_role_ownership_evidence(self):
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
            staff_role_name='Beta Lab Staff',
        )
        with mock.patch.object(personas, 'manifest', return_value=policy), \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_read_state', return_value={'owned': True}), \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope') as writer:
            with self.assertRaisesRegex(
                personas.BetaLabPersonaError, 'exact owned Discord roles',
            ):
                personas.seed_database(object())
        writer.assert_not_called()

    def test_seed_writes_pending_evidence_before_commit_then_publishes(self):
        events = []
        profile = object()
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
            staff_role_name='Beta Lab Staff',
        )

        class Scope:
            def __init__(self, name):
                self.name = name

            def __enter__(self):
                events.append(f'{self.name}-enter')

            def __exit__(self, exc_type, *_args):
                events.append(f'{self.name}-exit-{exc_type is None}')

        database = SimpleNamespace(
            connection_context=lambda: Scope('connection'),
            atomic=lambda: Scope('atomic'),
            execute_sql=lambda *_args: events.append('update'),
        )

        def read(_profile, filename):
            if filename == personas.ROLE_STATE_FILENAME:
                return {
                    'schema_version': 1,
                    'guild_id': 300,
                    'team_role_id': 900,
                    'team_role_name': policy.team_name,
                    'staff_role_id': 901,
                    'staff_role_name': 'Beta Lab Staff',
                }
            return None

        def write(_profile, filename, _value):
            events.append(f'write-{filename}')

        final = personas.PersonaDatabaseStatus(True, 'ready', 20, 10)
        with mock.patch.object(personas, 'manifest', return_value=policy), \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_read_state', side_effect=read), \
                mock.patch.object(personas, '_write_state', side_effect=write), \
                mock.patch.object(personas, '_publish_database_state', side_effect=lambda _profile: events.append('publish')), \
                mock.patch.object(personas, 'database_status', return_value=final), \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope', return_value=Scope('writer')), \
                mock.patch.object(personas.beta_wider_setup, '_default_database_factory', return_value=database), \
                mock.patch.object(personas.beta_wider_setup, '_identity'), \
                mock.patch.object(personas, '_database_rows', return_value=((), ())), \
                mock.patch.object(personas.beta_wider_setup, '_insert_house', return_value=10), \
                mock.patch.object(personas.beta_wider_setup, '_insert_team', return_value=20):
            self.assertIs(personas.seed_database(profile), final)

        write_event = f'write-{personas.DATABASE_PENDING_STATE_FILENAME}'
        self.assertLess(events.index(write_event), events.index('atomic-exit-True'))
        self.assertLess(events.index('atomic-exit-True'), events.index('publish'))
        self.assertNotIn(f'write-{personas.DATABASE_STATE_FILENAME}', events)

    def test_pending_evidence_blocks_status_and_seed_retry(self):
        profile = object()
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
            staff_role_name='Beta Lab Staff',
        )

        def read(_profile, filename):
            if filename == personas.ROLE_STATE_FILENAME:
                return {
                    'schema_version': 1,
                    'guild_id': 300,
                    'team_role_id': 900,
                    'team_role_name': policy.team_name,
                    'staff_role_id': 901,
                    'staff_role_name': 'Beta Lab Staff',
                }
            if filename == personas.DATABASE_PENDING_STATE_FILENAME:
                return {'pending': True}
            return None

        with mock.patch.object(personas, 'manifest', return_value=policy), \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_read_state', side_effect=read), \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope', return_value=contextlib.nullcontext()):
            with self.assertRaisesRegex(
                personas.BetaLabPersonaError, 'requires reconciliation',
            ):
                personas.seed_database(profile)


if __name__ == '__main__':
    unittest.main()
