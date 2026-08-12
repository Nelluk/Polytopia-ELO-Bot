"""Focused safety coverage for owned guided Beta Lab personas."""

import copy
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime
from scripts import manage_beta_lab_personas


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

    async def test_reconcile_adopts_only_one_exact_unused_role_pair(self):
        team = FakeRole(900, self.policy.team_name)
        staff = FakeRole(901, self.policy.staff_role_name)
        guild = FakeGuild((team, staff))
        state = {}

        def read(_profile, _filename):
            return state or None

        def write(_profile, _filename, value):
            state.update(value)

        with mock.patch.object(personas, 'manifest', return_value=self.policy), \
                mock.patch.object(personas.beta_operations, 'assert_beta_profile'), \
                mock.patch.object(personas, '_read_state', side_effect=read), \
                mock.patch.object(personas, '_write_state', side_effect=write):
            result = await personas.reconcile_roles(self.profile, guild)

        self.assertEqual((result.team_role_id, result.staff_role_id), (900, 901))
        self.assertEqual(guild.created, [])

    async def test_reconcile_refuses_duplicate_changed_or_used_roles(self):
        cases = []
        cases.append(FakeGuild((
            FakeRole(900, self.policy.team_name),
            FakeRole(902, self.policy.team_name),
            FakeRole(901, self.policy.staff_role_name),
        )))
        changed = FakeRole(900, self.policy.team_name)
        changed.permissions.value = 1
        cases.append(FakeGuild((changed, FakeRole(901, self.policy.staff_role_name))))
        used = FakeRole(900, self.policy.team_name)
        used.members.append(FakeMember(10, (used,)))
        cases.append(FakeGuild((used, FakeRole(901, self.policy.staff_role_name))))

        for guild in cases:
            with self.subTest(roles=[role.name for role in guild.roles]), \
                    mock.patch.object(personas, 'manifest', return_value=self.policy), \
                    mock.patch.object(personas.beta_operations, 'assert_beta_profile'), \
                    mock.patch.object(personas, '_read_state', return_value=None):
                with self.assertRaises(personas.BetaLabPersonaError):
                    await personas.reconcile_roles(self.profile, guild)

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
    def test_cli_reports_shared_writer_refusal_without_traceback(self):
        refusal = personas.beta_wider_setup.WiderBetaSetupSafetyError(
            'durable writer is active',
        )
        with mock.patch.object(
                manage_beta_lab_personas.beta_operations,
                'assert_operator_context',
        ), mock.patch.object(
                manage_beta_lab_personas,
                '_profile',
                return_value=object(),
        ), mock.patch.object(
                manage_beta_lab_personas.beta_lab_personas,
                'reconcile_pending_database',
                side_effect=refusal,
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = manage_beta_lab_personas.main([
                'database-reconcile',
                '--confirm',
                manage_beta_lab_personas.RECONCILE_CONFIRMATION,
            ])
        self.assertEqual(result, 2)
        self.assertIn('durable writer is active', stderr.getvalue())

    def test_database_adoption_requires_one_exact_pristine_unused_pair(self):
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
        )
        house = {
            'id': 10, 'name': policy.house_name, 'emoji': '',
            'image_url': None, 'league_tokens': 0,
        }
        team = {
            'id': 20, 'name': policy.team_name, 'guild_id': policy.guild_id,
            'house_id': 10, 'house_name': policy.house_name, 'hidden': False,
            'archived': False, 'league_tier': 1, 'external_server': None,
            'elo': 1000, 'elo_alltime': 1000, 'emoji': '',
            'image_url': None, 'pro_league': True,
        }
        house_usage = {
            'team_ids': [20], 'team_names': [policy.team_name],
            'team_guild_ids': [policy.guild_id], 'preference_count': 0,
            'bid_count': 0,
        }
        team_usage = {'player_count': 0, 'game_side_count': 0}

        def inspect(*, houses=(house,), teams=(team,), h_usage=house_usage,
                    t_usage=team_usage):
            with mock.patch.object(personas.beta_wider_setup, '_house', return_value=list(houses)), \
                    mock.patch.object(personas.beta_wider_setup, '_team', return_value=list(teams)), \
                    mock.patch.object(personas.beta_wider_setup, '_house_usage', return_value=h_usage), \
                    mock.patch.object(personas.beta_wider_setup, '_team_usage', return_value=t_usage):
                return personas._database_adoption_evidence(object(), policy)

        evidence = inspect()
        self.assertEqual(evidence['schema_version'], 2)
        self.assertEqual(evidence['origin'], 'adopted')
        self.assertEqual(evidence['team_id'], 20)
        self.assertEqual(
            evidence['baseline_sha256'],
            personas._canonical_digest(evidence['baseline']),
        )

        mutations = [
            {'houses': (house, dict(house, id=11))},
            {'teams': (team, dict(team, id=21))},
        ]
        for field, value in (
            ('name', 'wrong'), ('emoji', 'x'), ('image_url', 'x'),
            ('league_tokens', 1),
        ):
            mutations.append({'houses': (dict(house, **{field: value}),)})
        for field, value in (
            ('name', 'wrong'), ('guild_id', 301), ('house_id', 11),
            ('house_name', 'wrong'), ('hidden', True), ('archived', True),
            ('league_tier', 2), ('external_server', 1), ('elo', 1001),
            ('elo_alltime', 1001), ('emoji', 'x'), ('image_url', 'x'),
            ('pro_league', False),
        ):
            mutations.append({'teams': (dict(team, **{field: value}),)})
        for field, value in (
            ('team_ids', [21]), ('team_names', ['wrong']),
            ('team_guild_ids', [301]), ('preference_count', 1),
            ('bid_count', 1),
        ):
            mutations.append({'h_usage': dict(house_usage, **{field: value})})
        for field in ('player_count', 'game_side_count'):
            mutations.append({'t_usage': dict(team_usage, **{field: 1})})

        for mutation in mutations:
            with self.subTest(mutation=mutation), \
                    self.assertRaises(personas.BetaLabPersonaError):
                inspect(**mutation)

    def test_evidence_digest_covers_every_baseline_dimension(self):
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
        )
        baseline = {
            'house': {
                'id': 10, 'name': policy.house_name, 'emoji': '',
                'image_url': None, 'league_tokens': 0,
                'usage': {
                    'team_ids': [20], 'team_names': [policy.team_name],
                    'team_guild_ids': [300], 'preference_count': 0,
                    'bid_count': 0,
                },
            },
            'team': {
                'id': 20, 'name': policy.team_name, 'guild_id': 300,
                'house_id': 10, 'house_name': policy.house_name,
                'hidden': False, 'archived': False, 'league_tier': 1,
                'external_server': None, 'elo': 1000, 'elo_alltime': 1000,
                'emoji': '', 'image_url': None, 'pro_league': True,
                'usage': {'player_count': 0, 'game_side_count': 0},
            },
        }
        evidence = personas._evidence_from_baseline(
            baseline, policy, origin='created',
        )
        self.assertTrue(personas._database_evidence_matches(
            evidence, baseline, policy,
        ))
        changed = copy.deepcopy(baseline)
        changed['team']['usage']['game_side_count'] = 1
        self.assertFalse(personas._database_evidence_matches(
            evidence, changed, policy,
        ))
        forged = copy.deepcopy(evidence)
        forged['baseline']['team']['elo'] = 1001
        self.assertFalse(personas._database_evidence_matches(
            forged, baseline, policy,
        ))

    def test_database_reconcile_adopts_only_reviewed_pair(self):
        profile = object()
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
        )
        baseline = {
            'house': {'id': 10},
            'team': {'id': 20},
        }
        evidence = personas._evidence_from_baseline(
            baseline, policy, origin='adopted',
        )
        state = {}
        database = SimpleNamespace(
            connection_context=contextlib.nullcontext,
            atomic=contextlib.nullcontext,
            execute_sql=mock.Mock(),
        )

        def read(_profile, filename):
            return state.get(filename)

        def write(_profile, filename, value):
            state[filename] = value

        final = personas.PersonaDatabaseStatus(True, 'ready', 20, 10)
        with mock.patch.object(personas, 'manifest', return_value=policy), \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_role_state_for_database'), \
                mock.patch.object(personas, '_read_state', side_effect=read), \
                mock.patch.object(personas, '_write_state', side_effect=write), \
                mock.patch.object(personas, '_publish_database_state') as publish, \
                mock.patch.object(personas, 'database_status', return_value=final), \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope', return_value=contextlib.nullcontext()), \
                mock.patch.object(personas.beta_wider_setup, '_default_database_factory', return_value=database), \
                mock.patch.object(personas, '_read_only_database_baseline', side_effect=(baseline, baseline)):
            self.assertIs(personas.reconcile_pending_database(profile), final)

        self.assertEqual(
            state[personas.DATABASE_PENDING_STATE_FILENAME], evidence,
        )
        publish.assert_called_once_with(profile, replace_state=None)

    def test_reconcile_refuses_change_between_proof_and_publication(self):
        profile = object()
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
        )
        baseline = {'house': {'id': 10}, 'team': {'id': 20}}
        changed = {'house': {'id': 10}, 'team': {'id': 21}}
        state = {}
        database = SimpleNamespace(connection_context=contextlib.nullcontext)

        with mock.patch.object(personas, 'manifest', return_value=policy), \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_role_state_for_database'), \
                mock.patch.object(personas, '_read_state', side_effect=lambda _p, name: state.get(name)), \
                mock.patch.object(personas, '_write_state', side_effect=lambda _p, name, value: state.__setitem__(name, value)), \
                mock.patch.object(personas, '_publish_database_state') as publish, \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope', return_value=contextlib.nullcontext()), \
                mock.patch.object(personas.beta_wider_setup, '_default_database_factory', return_value=database), \
                mock.patch.object(personas, '_read_only_database_baseline', side_effect=(baseline, changed)):
            with self.assertRaisesRegex(
                personas.BetaLabPersonaError, 'changed between',
            ):
                personas.reconcile_pending_database(profile)

        self.assertIn(personas.DATABASE_PENDING_STATE_FILENAME, state)
        publish.assert_not_called()

    def test_reconcile_upgrades_only_exact_legacy_evidence(self):
        profile = object()
        policy = SimpleNamespace(
            guild_id=300,
            house_name='Beta Lab House',
            team_name='Beta Lab Team',
        )
        baseline = {'house': {'id': 10}, 'team': {'id': 20}}
        legacy = {
            'schema_version': 1, 'guild_id': 300, 'house_id': 10,
            'house_name': policy.house_name, 'team_id': 20,
            'team_name': policy.team_name,
        }
        state = {personas.DATABASE_STATE_FILENAME: legacy}
        database = SimpleNamespace(connection_context=contextlib.nullcontext)
        final = personas.PersonaDatabaseStatus(True, 'ready', 20, 10)

        with mock.patch.object(personas, 'manifest', return_value=policy), \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_role_state_for_database'), \
                mock.patch.object(personas, '_read_state', side_effect=lambda _p, name: state.get(name)), \
                mock.patch.object(personas, '_write_state', side_effect=lambda _p, name, value: state.__setitem__(name, value)), \
                mock.patch.object(personas, '_publish_database_state') as publish, \
                mock.patch.object(personas, 'database_status', return_value=final), \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope', return_value=contextlib.nullcontext()), \
                mock.patch.object(personas.beta_wider_setup, '_default_database_factory', return_value=database), \
                mock.patch.object(personas, '_read_only_database_baseline', side_effect=(baseline, baseline)):
            self.assertIs(personas.reconcile_pending_database(profile), final)

        upgraded = state[personas.DATABASE_PENDING_STATE_FILENAME]
        self.assertEqual(upgraded['schema_version'], 2)
        self.assertEqual(upgraded['origin'], 'adopted')
        publish.assert_called_once_with(profile, replace_state=legacy)

    def test_state_and_pending_conflict_fails_before_database_access(self):
        profile = object()
        policy = SimpleNamespace(guild_id=300)
        states = {
            personas.DATABASE_STATE_FILENAME: {'state': True},
            personas.DATABASE_PENDING_STATE_FILENAME: {'pending': True},
        }
        with mock.patch.object(personas, 'manifest', return_value=policy), \
                mock.patch.object(personas.beta_readiness, 'validate_database_profile'), \
                mock.patch.object(personas, '_role_state_for_database'), \
                mock.patch.object(personas, '_read_state', side_effect=lambda _p, name: states.get(name)), \
                mock.patch.object(personas.beta_wider_setup, '_mutation_writer_scope', return_value=contextlib.nullcontext()), \
                mock.patch.object(personas.beta_wider_setup, '_default_database_factory') as factory:
            with self.assertRaisesRegex(
                personas.BetaLabPersonaError, 'Published and pending',
            ):
                personas.reconcile_pending_database(profile)
        factory.assert_not_called()

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
                mock.patch.object(personas, '_database_baseline', return_value={
                    'house': {'id': 10}, 'team': {'id': 20},
                }), \
                mock.patch.object(personas, '_read_only_database_baseline', return_value={
                    'house': {'id': 10}, 'team': {'id': 20},
                }), \
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
