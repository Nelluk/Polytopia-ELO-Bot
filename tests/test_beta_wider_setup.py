"""Offline tests for the reviewed WB1.3b exact-scope setup boundary."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest import mock

from modules import application_command_policy, beta_readiness, beta_wider_setup
from scripts import manage_beta_wider_setup


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads(
    (ROOT / beta_wider_setup.DEFAULT_MANIFEST).read_text(encoding='utf-8')
)


def make_profile(root: Path, **overrides):
    values = {
        'environment': 'development',
        'project_root': root,
        'log_root': root / 'logs' / 'development',
        'expected_bot_id': beta_readiness.BETA_APPLICATION_ID,
        'allowed_guild_ids': (beta_readiness.BETA_GUILD_ID,),
        'database_name': beta_readiness.BETA_DATABASE_NAME,
        'database_user': beta_readiness.BETA_DATABASE_ROLE,
        'database_password': 'not-output',
        'database_host': 'localhost',
        'database_port': 5432,
        'background_tasks_enabled': False,
        'api_enabled': False,
        'bullet_enabled': False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Cursor:
    def __init__(self, rows=(), *, rowcount=None):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class SetupDatabase:
    """Small in-memory Peewee-like connection with transactional snapshots."""

    def __init__(self, *, fail_team_name=None):
        self.events = []
        self.queries = []
        self.houses = {}
        self.teams = {}
        self.team_usage = {}
        self.house_preferences = {}
        self.house_bids = {}
        self.next_house_id = 100
        self.next_team_id = 200
        self.fail_team_name = fail_team_name

    @contextmanager
    def connection_context(self):
        self.events.append('connection-open')
        try:
            yield self
        finally:
            self.events.append('connection-close')

    @contextmanager
    def atomic(self):
        self.events.append('transaction-open')
        snapshot = copy.deepcopy((
            self.houses,
            self.teams,
            self.team_usage,
            self.house_preferences,
            self.house_bids,
            self.next_house_id,
            self.next_team_id,
        ))
        try:
            yield self
        except Exception:
            (
                self.houses,
                self.teams,
                self.team_usage,
                self.house_preferences,
                self.house_bids,
                self.next_house_id,
                self.next_team_id,
            ) = snapshot
            self.events.append('transaction-rollback')
            raise
        else:
            self.events.append('transaction-commit')
        finally:
            self.events.append('transaction-close')

    def add_house(self, name, *, house_id=None):
        house_id = house_id or self.next_house_id
        self.next_house_id = max(self.next_house_id, house_id + 1)
        self.houses[name] = {
            'id': house_id,
            'name': name,
            'emoji': '',
            'image_url': None,
            'league_tokens': 0,
        }
        self.house_preferences.setdefault(house_id, 0)
        self.house_bids.setdefault(house_id, 0)
        return house_id

    def add_team(
            self,
            name,
            house_name,
            *,
            guild_id=beta_readiness.BETA_GUILD_ID,
            team_id=None,
            hidden=False,
            archived=False,
            league_tier=None):
        if house_name not in self.houses:
            self.add_house(house_name)
        team_id = team_id or self.next_team_id
        self.next_team_id = max(self.next_team_id, team_id + 1)
        house = self.houses[house_name]
        self.teams[name] = {
            'id': team_id,
            'name': name,
            'guild_id': guild_id,
            'house_id': house['id'],
            'house_name': house_name,
            'hidden': hidden,
            'archived': archived,
            'league_tier': league_tier,
            'external_server': None,
            'elo': 1000,
            'elo_alltime': 1000,
            'emoji': '',
            'image_url': None,
            'pro_league': True,
        }
        self.team_usage.setdefault(team_id, {'player_count': 0, 'game_side_count': 0})
        return team_id

    def execute_sql(self, query, params=()):
        normalized = ' '.join(str(query).split()).lower()
        params = tuple(params)
        self.queries.append((normalized, params))
        if normalized.startswith('set transaction read only'):
            return Cursor()
        if 'current_database()' in normalized:
            return Cursor([('polytopia_dev', 'polybot_dev')])
        if normalized.startswith(
                'select id, name, emoji, image_url, league_tokens from house'):
            name = params[0]
            rows = [self.houses[name]] if name in self.houses else []
            return Cursor([
                (
                    row['id'], row['name'], row['emoji'], row['image_url'],
                    row['league_tokens'],
                )
                for row in rows
            ])
        if normalized.startswith('select t.id, t.name, t.guild_id, t.house_id'):
            guild_id, name = params
            row = self.teams.get(name)
            if row is None or row['guild_id'] != guild_id:
                return Cursor()
            house = self.houses.get(row['house_name'])
            return Cursor([(
                row['id'], row['name'], row['guild_id'], row['house_id'],
                house['name'] if house else None, row['hidden'],
                row['archived'], row['league_tier'], row['external_server'],
                row['elo'], row['elo_alltime'], row['emoji'], row['image_url'],
                row['pro_league'],
            )])
        if normalized.startswith('select id, name, guild_id from team'):
            house_id = params[0]
            rows = sorted(
                (
                    (row['id'], row['name'], row['guild_id'])
                    for row in self.teams.values()
                    if row['house_id'] == house_id
                ),
                key=lambda row: row[0],
            )
            return Cursor(rows)
        if normalized.startswith('select count(*) from playerhousepreference'):
            return Cursor([(self.house_preferences.get(params[0], 0),)])
        if normalized.startswith('select count(*) from bid'):
            return Cursor([(self.house_bids.get(params[0], 0),)])
        if normalized.startswith('select count(*) from player'):
            return Cursor([(self.team_usage.get(params[0], {}).get('player_count', 0),)])
        if normalized.startswith('select count(*) from gameside'):
            return Cursor([(self.team_usage.get(params[0], {}).get('game_side_count', 0),)])
        if normalized.startswith('insert into house'):
            name = params[0]
            if name in self.houses:
                raise RuntimeError('duplicate house')
            house_id = self.add_house(name)
            return Cursor([(house_id,)])
        if normalized.startswith('insert into team'):
            guild_id, name, house_id = params[:3]
            if name == self.fail_team_name:
                raise RuntimeError('injected team insert failure')
            house = next(
                row for row in self.houses.values() if row['id'] == house_id
            )
            team_id = self.add_team(name, house['name'], guild_id=guild_id)
            return Cursor([(team_id,)])
        if normalized.startswith('delete from team'):
            record_id = params[0]
            names = [name for name, row in self.teams.items() if row['id'] == record_id]
            for name in names:
                del self.teams[name]
            return Cursor(rowcount=len(names))
        if normalized.startswith('delete from house'):
            record_id = params[0]
            names = [name for name, row in self.houses.items() if row['id'] == record_id]
            for name in names:
                del self.houses[name]
            return Cursor(rowcount=len(names))
        raise AssertionError(f'unexpected setup query: {query!r}')


class WiderBetaSetupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.profile = make_profile(self.root)
        self.manifest = copy.deepcopy(MANIFEST)

    def tearDown(self):
        self.tempdir.cleanup()

    def seed(self, database, **kwargs):
        kwargs.setdefault('writer_check', lambda _profile: None)
        return beta_wider_setup.seed_wider_beta_setup(
            profile=self.profile,
            manifest=self.manifest,
            database_factory=lambda _profile: database,
            **kwargs,
        )

    def test_reviewed_manifest_roles_and_capability_root_expansion(self):
        reviewed = beta_wider_setup.validate_reviewed_manifest(self.manifest)
        self.assertEqual(
            [item['name'] for item in reviewed['database']['teams']['proposed']],
            ['The Ronin', 'The Jets', 'The Sparkies'],
        )
        self.assertEqual(
            [item['role_id'] for item in reviewed['database']['role_bindings']['proposed']],
            [item[2] for item in beta_wider_setup.EXPECTED_TEAMS],
        )
        self.assertEqual(
            application_command_policy.build_capability_policy(
                {beta_readiness.BETA_GUILD_ID: ('tools_support',)},
                [beta_readiness.BETA_GUILD_ID],
            ).roots_for_guild(beta_readiness.BETA_GUILD_ID),
            ('staffhelp',),
        )
        self.assertEqual(
            application_command_policy.TOOLS_SUPPORT_RESERVED_ROOTS,
            ('about', 'guide', 'help', 'support', 'tools'),
        )
        self.assertIn('/staffhelp', reviewed['capabilities']['optional'][0]['reason'])
        for root in application_command_policy.TOOLS_SUPPORT_RESERVED_ROOTS:
            self.assertIn(f'/{root}', reviewed['capabilities']['optional'][0]['reason'])

    def test_plan_reports_root_review_without_apply(self):
        discord = {
            'schema_version': 1,
            'kind': 'discord_guild_inventory',
            'target': {
                'environment': 'development',
                'guild_id': beta_readiness.BETA_GUILD_ID,
                'application_id': beta_readiness.BETA_APPLICATION_ID,
            },
            'tester_role': {
                'live_id': beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
                'pinned_id': beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
                'name': 'testers',
                'verified': True,
            },
            'fixed_channels': {
                'public_release': {
                    'id': beta_readiness.BETA_PUBLIC_RELEASE_CHANNEL_ID,
                    'name': beta_readiness.BETA_PUBLIC_RELEASE_CHANNEL_NAME,
                },
                'staffhelp_mirror': {
                    'id': beta_readiness.BETA_STAFFHELP_MIRROR_CHANNEL_ID,
                    'name': beta_readiness.BETA_STAFFHELP_MIRROR_CHANNEL_NAME,
                },
            },
            'capabilities': {'current': ['core_user', 'elo_maintenance', 'team']},
            'roles': [
                {'id': role_id, 'name': name}
                for name, _house, role_id in beta_wider_setup.EXPECTED_TEAMS
            ],
        }
        database = {
            'schema_version': 1,
            'kind': 'development_database_inventory',
            'target': {
                'environment': 'development',
                'guild_id': beta_readiness.BETA_GUILD_ID,
                'database': beta_readiness.BETA_DATABASE_NAME,
                'database_role': beta_readiness.BETA_DATABASE_ROLE,
            },
            'teams': [],
            'houses': [],
            'fixtures': {},
        }
        result = beta_readiness.plan_readiness(
            manifest=self.manifest,
            discord_inventory=discord,
            database_inventory=database,
        )
        self.assertEqual(result['diff']['capabilities']['add'], ['tools_support'])
        self.assertEqual(
            result['diff']['capabilities']['root_review']['tools_support'],
            {
                'implemented_roots': ['staffhelp'],
                'reserved_unloaded_roots': ['about', 'guide', 'help', 'support', 'tools'],
            },
        )
        self.assertFalse(result['ready_for_live_apply'])
        self.assertFalse(result['boundaries']['database_mutation_applied'])

    def test_seed_is_idempotent_and_marks_only_new_rows_owned(self):
        database = SetupDatabase()
        first = self.seed(database)
        self.assertEqual(first['status'], 'seeded')
        self.assertEqual(
            [item['name'] for item in first['state']['houses'] if item['owned']],
            ['Beta House Alpha', 'Beta House Beta'],
        )
        self.assertEqual(
            [item['name'] for item in first['state']['teams'] if item['owned']],
            ['The Ronin', 'The Jets', 'The Sparkies'],
        )
        insert_queries = [query for query, _params in database.queries if query.startswith('insert into')]
        self.assertEqual(
            [query.split()[2] for query in insert_queries],
            ['house', 'house', 'team', 'team', 'team'],
        )
        second = self.seed(database)
        self.assertEqual(second['status'], 'idempotent')
        self.assertEqual(
            len([query for query, _params in database.queries if query.startswith('insert into')]),
            5,
        )
        self.assertEqual(database.events.count('connection-open'), 2)
        self.assertEqual(database.events.count('connection-close'), 2)
        self.assertTrue(
            (self.root / 'logs' / 'development' / 'beta-operations' /
             beta_wider_setup.SETUP_STATE_FILENAME).is_file()
        )

    def test_preexisting_compatible_rows_are_preserved_and_unowned(self):
        database = SetupDatabase()
        for house_name in beta_wider_setup.EXPECTED_HOUSES:
            database.add_house(house_name)
        for team_name, house_name, _role_id in beta_wider_setup.EXPECTED_TEAMS:
            database.add_team(team_name, house_name)
        self.seed(database)
        state_path = self.root / 'logs' / 'development' / 'beta-operations' / beta_wider_setup.SETUP_STATE_FILENAME
        state = json.loads(state_path.read_text(encoding='utf-8'))
        self.assertFalse(any(item['owned'] for item in state['houses']))
        self.assertFalse(any(item['owned'] for item in state['teams']))
        before = (set(database.houses), set(database.teams))
        result = beta_wider_setup.cleanup_wider_beta_setup(
            profile=self.profile,
            manifest=self.manifest,
            confirmed=True,
            database_factory=lambda _profile: database,
            writer_check=lambda _profile: None,
        )
        self.assertEqual(result['removed_house_ids'], [])
        self.assertEqual(result['removed_team_ids'], [])
        self.assertEqual(before, (set(database.houses), set(database.teams)))
        self.assertFalse(state_path.exists())

    def test_incompatible_existing_rows_refuse_before_mutation(self):
        database = SetupDatabase()
        database.add_house('Beta House Alpha')
        database.add_team('The Ronin', 'Beta House Alpha', hidden=True)
        with self.assertRaises(beta_wider_setup.WiderBetaSetupConflictError):
            self.seed(database)
        self.assertEqual(
            [query for query, _params in database.queries if query.startswith('insert into')],
            [],
        )
        self.assertIn('transaction-rollback', database.events)

    def test_transaction_rolls_back_on_later_team_failure(self):
        database = SetupDatabase(fail_team_name='The Jets')
        with self.assertRaises(beta_wider_setup.WiderBetaSetupSafetyError):
            self.seed(database)
        self.assertEqual(database.houses, {})
        self.assertEqual(database.teams, {})
        state_path = self.root / 'logs' / 'development' / 'beta-operations' / beta_wider_setup.SETUP_STATE_FILENAME
        self.assertFalse(state_path.exists())
        self.assertIn('transaction-rollback', database.events)

    def test_writer_active_refuses_before_opening_worker_connection(self):
        database = SetupDatabase()
        with self.assertRaises(beta_wider_setup.WiderBetaSetupSafetyError):
            self.seed(
                database,
                writer_check=lambda _profile: (_ for _ in ()).throw(
                    beta_wider_setup.WiderBetaSetupSafetyError('writer active')
                ),
            )
        self.assertEqual(database.events, [])

    def test_cleanup_refuses_subsequent_team_usage_and_shared_house(self):
        database = SetupDatabase()
        self.seed(database)
        database.team_usage[database.teams['The Ronin']['id']]['player_count'] = 1
        with self.assertRaises(beta_wider_setup.WiderBetaSetupOwnershipError):
            beta_wider_setup.cleanup_wider_beta_setup(
                profile=self.profile,
                manifest=self.manifest,
                confirmed=True,
                database_factory=lambda _profile: database,
                writer_check=lambda _profile: None,
            )
        self.assertTrue(
            (self.root / 'logs' / 'development' / 'beta-operations' /
             beta_wider_setup.SETUP_STATE_FILENAME).exists()
        )

        database.team_usage[database.teams['The Ronin']['id']]['player_count'] = 0
        database.add_team('Unowned Extra', 'Beta House Alpha')
        with self.assertRaises(beta_wider_setup.WiderBetaSetupOwnershipError):
            beta_wider_setup.cleanup_wider_beta_setup(
                profile=self.profile,
                manifest=self.manifest,
                confirmed=True,
                database_factory=lambda _profile: database,
                writer_check=lambda _profile: None,
            )
        self.assertIn('Unowned Extra', database.teams)

    def test_cleanup_requires_exact_confirmation_and_has_no_other_mutation_path(self):
        with self.assertRaises(beta_wider_setup.WiderBetaSetupConfirmationError):
            beta_wider_setup.cleanup_wider_beta_setup(
                profile=self.profile,
                manifest=self.manifest,
                confirmed=False,
                database_factory=lambda _profile: SetupDatabase(),
                writer_check=lambda _profile: None,
            )
        source = inspect.getsource(beta_wider_setup)
        for forbidden in (
                'import discord', '.send(', '.edit(', '.sync(',
                'Game.create', 'recalculate_elo', 'DELETE FROM game',
                'DELETE FROM gameside', 'create_tables'):
            self.assertNotIn(forbidden, source)

    def test_status_owns_worker_local_read_only_connection(self):
        database = SetupDatabase()
        result = beta_wider_setup.status_wider_beta_setup(
            profile=self.profile,
            manifest=self.manifest,
            database_factory=lambda _profile: database,
        )
        self.assertEqual(result['kind'], 'wb1_3b_setup_status')
        self.assertFalse(result['ready_for_cleanup'])
        self.assertIn('set transaction read only', database.queries[0][0])
        self.assertEqual(
            database.events,
            [
                'connection-open', 'transaction-open',
                'transaction-commit', 'transaction-close', 'connection-close',
            ],
        )


class WiderBetaSetupCliTests(unittest.TestCase):
    def test_wrong_cleanup_confirmation_refuses_before_profile_or_database(self):
        with mock.patch.object(
                manage_beta_wider_setup,
                '_selected_profile',
                side_effect=AssertionError('profile must not load')):
            result = manage_beta_wider_setup.main([
                '--json', 'cleanup', '--confirm', 'wrong-token',
            ])
        self.assertEqual(result, 2)

    def test_cli_refuses_non_development_before_connection(self):
        with mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}, clear=False):
            result = manage_beta_wider_setup.main(['--json', 'status'])
        self.assertEqual(result, 2)


if __name__ == '__main__':
    unittest.main()
