"""Focused offline coverage for WB1.3a readiness inventory and planning."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
import inspect
import io
import json
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest import mock

from modules import beta_operations, beta_readiness
from scripts import manage_beta_readiness


CHECKPOINT = 'a' * 40


def profile(root: Path, **overrides):
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
        'server_settings': SimpleNamespace(
            application_command_capabilities={
                beta_readiness.BETA_GUILD_ID: (
                    'core_user', 'elo_maintenance', 'team'
                ),
            }
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakePermissions:
    def __init__(self, value):
        self.value = value


class FakeRole:
    def __init__(
            self,
            role_id,
            name,
            *,
            position=1,
            members=(),
            managed=False,
            target_kind=None):
        self.id = role_id
        self.name = name
        self.position = position
        self.members = tuple(members)
        self.managed = managed
        self.permissions = FakePermissions(8)
        self.mentionable = False
        self.hoist = False
        if target_kind is not None:
            self.target_kind = target_kind

    def is_default(self):
        return self.name == '@everyone'


class FakeChannel:
    def __init__(
            self,
            guild,
            channel_id,
            name,
            *,
            channel_type='text',
            category=None,
            overwrites=()):
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.type = SimpleNamespace(name=channel_type, value=0)
        self.category = category
        self.category_id = getattr(category, 'id', None)
        self.position = 1
        self.nsfw = False
        self.overwrites = tuple(overwrites)

    def permissions_for(self, member):
        return FakePermissions(1024)


class FakeGuild:
    def __init__(self, *, guild_id=beta_readiness.BETA_GUILD_ID):
        self.id = guild_id
        self.name = 'Development Test Guild'
        self.member_count = 37
        self.me = SimpleNamespace(id=beta_readiness.BETA_APPLICATION_ID)
        self.roles = []
        self.categories = []
        self.channels = []

    def get_channel(self, channel_id):
        return next((channel for channel in self.channels if channel.id == channel_id), None)

    def get_member(self, member_id):
        return self.me if member_id == self.me.id else None


class FakeBot:
    def __init__(self, guild, *, bot_id=beta_readiness.BETA_APPLICATION_ID):
        self.user = SimpleNamespace(id=bot_id, name='PolyELO Bot Beta')
        self.guild = guild

    def is_ready(self):
        return True

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


def make_discord_world(*, role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID):
    guild = FakeGuild()
    category = FakeChannel(
        guild,
        490000000000000001,
        'beta-category',
        channel_type='category',
    )
    staff_role = FakeRole(490000000000000002, 'Helper', position=2)
    tester_role = FakeRole(role_id, beta_readiness.BETA_TESTER_ROLE_NAME, position=3, members=(1, 2, 3))
    everyone = FakeRole(beta_readiness.BETA_GUILD_ID, '@everyone', position=0)
    public = FakeChannel(
        guild,
        beta_readiness.BETA_PUBLIC_RELEASE_CHANNEL_ID,
        beta_readiness.BETA_PUBLIC_RELEASE_CHANNEL_NAME,
        category=category,
        overwrites=((everyone, {'allow': 1024, 'deny': 0}), (staff_role, {'allow': 2048, 'deny': 0})),
    )
    private = FakeChannel(
        guild,
        beta_readiness.BETA_STAFFHELP_MIRROR_CHANNEL_ID,
        beta_readiness.BETA_STAFFHELP_MIRROR_CHANNEL_NAME,
        category=category,
    )
    extra = FakeChannel(guild, 490000000000000003, 'beta-smoke', category=category)
    guild.roles = [tester_role, staff_role, everyone]
    guild.categories = [category]
    guild.channels = [public, private, extra]
    return guild


def build_inventory(root: Path | None = None):
    root = root or Path(tempfile.mkdtemp())
    guild = make_discord_world()
    return beta_readiness.build_discord_inventory(
        bot=FakeBot(guild),
        profile=profile(root),
        pinned_tester_role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
    )


class DiscordInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_inventory_is_deterministic_bounded_and_primitive_only(self):
        first = build_inventory(self.root)
        second = build_inventory(self.root)
        self.assertEqual(first, second)
        json.dumps(first, allow_nan=False)
        self.assertEqual(first['target']['guild_id'], beta_readiness.BETA_GUILD_ID)
        self.assertEqual(first['bot']['id'], beta_readiness.BETA_APPLICATION_ID)
        self.assertEqual(first['tester_role']['pinned_id'], beta_readiness.BETA_PINNED_TESTER_ROLE_ID)
        self.assertEqual(first['tester_role']['match_count'], 1)
        self.assertEqual(first['fixed_channels']['public_release']['name'], 'todo-and-changelog')
        self.assertEqual(first['fixed_channels']['staffhelp_mirror']['name'], 'admin-spam')
        self.assertFalse(first['privacy']['member_lists_included'])
        self.assertFalse(first['privacy']['message_content_included'])
        self.assertNotIn('members', json.dumps(first).lower())
        self.assertNotIn('messages', json.dumps(first).lower())
        self.assertIn('member_count', first['roles'][0])
        public = next(
            item for item in first['channels']
            if item['id'] == beta_readiness.BETA_PUBLIC_RELEASE_CHANNEL_ID
        )
        self.assertIn('permission_overwrites', public)
        self.assertTrue(public['permission_overwrites']['entries'])

    def test_large_cached_collections_are_bounded_without_member_lists(self):
        guild = make_discord_world()
        guild.roles.extend(
            FakeRole(500000000000000000 + index, f'role-{index}')
            for index in range(beta_readiness.MAX_DISCORD_ROLES + 10)
        )
        guild.channels.extend(
            FakeChannel(guild, 600000000000000000 + index, f'channel-{index}')
            for index in range(beta_readiness.MAX_DISCORD_CHANNELS + 10)
        )
        result = beta_readiness.build_discord_inventory(
            bot=FakeBot(guild),
            profile=profile(self.root),
            pinned_tester_role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
        )
        self.assertTrue(result['roles_truncated'])
        self.assertTrue(result['channels_truncated'])
        self.assertLessEqual(len(result['roles']), beta_readiness.MAX_DISCORD_ROLES)
        self.assertLessEqual(len(result['channels']), beta_readiness.MAX_DISCORD_CHANNELS)

    def test_wrong_bot_guild_channel_or_role_fails_closed(self):
        cases = [
            ('bot', lambda: FakeBot(make_discord_world(), bot_id=1)),
            ('guild', lambda: FakeBot(FakeGuild(guild_id=1))),
        ]
        for label, make_bot in cases:
            with self.subTest(label=label):
                with self.assertRaises(beta_readiness.ReadinessInventoryError):
                    beta_readiness.build_discord_inventory(
                        bot=make_bot(),
                        profile=profile(self.root),
                        pinned_tester_role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
                    )

        wrong_channel_guild = make_discord_world()
        wrong_channel_guild.channels[0].name = 'wrong-channel'
        with self.assertRaises(beta_readiness.ReadinessInventoryError):
            beta_readiness.build_discord_inventory(
                bot=FakeBot(wrong_channel_guild),
                profile=profile(self.root),
                pinned_tester_role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
            )

        wrong_role_guild = make_discord_world(role_id=999)
        with self.assertRaises(beta_readiness.ReadinessInventoryError):
            beta_readiness.build_discord_inventory(
                bot=FakeBot(wrong_role_guild),
                profile=profile(self.root),
                pinned_tester_role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
            )

        duplicate_role_guild = make_discord_world()
        duplicate_role_guild.roles.append(FakeRole(998, 'testers'))
        with self.assertRaises(beta_readiness.ReadinessInventoryError):
            beta_readiness.build_discord_inventory(
                bot=FakeBot(duplicate_role_guild),
                profile=profile(self.root),
                pinned_tester_role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
            )

    def test_inventory_builder_has_no_discord_mutation_or_message_access(self):
        source = inspect.getsource(beta_readiness.build_discord_inventory)
        for forbidden in ('.send(', '.edit(', '.delete(', '.history(', 'fetch_channel'):
            self.assertNotIn(forbidden, source)


class SocketInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_socket_inventory_is_read_only_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_profile = profile(root)
            environment = {
                'POLYBOT_BETA_CONTROL': 'enabled',
                'POLYBOT_BETA_STARTUP_SYNC': 'disabled',
                'POLYBOT_ENV': 'development',
                'POLYBOT_BETA_APPLICATION_ID': str(beta_operations.BETA_APPLICATION_ID),
                'POLYBOT_BETA_GUILD_ID': str(beta_operations.BETA_GUILD_ID),
                'POLYBOT_BETA_DATABASE': beta_operations.BETA_DATABASE_NAME,
                'POLYBOT_BETA_DATABASE_ROLE': beta_operations.BETA_DATABASE_ROLE,
            }
            guild = make_discord_world()
            bot = FakeBot(guild)
            with mock.patch.dict(os.environ, environment, clear=False):
                control = beta_operations.BetaReleaseControl(bot, selected_profile, CHECKPOINT)
                beta_operations._write_role_binding(
                    control.paths,
                    beta_operations.TesterRoleBinding(
                        guild_id=beta_operations.BETA_GUILD_ID,
                        role_name=beta_operations.BETA_TESTER_ROLE_NAME,
                        role_id=beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
                        resolved_at='2026-08-03T00:00:00.000Z',
                    ),
                )
                fake_server = SimpleNamespace(
                    close=mock.Mock(),
                    wait_closed=mock.AsyncMock(),
                )
                with mock.patch.object(
                        asyncio,
                        'start_unix_server',
                        new=mock.AsyncMock(return_value=fake_server)), \
                        mock.patch.object(beta_operations.os, 'chmod'):
                    await control.start()
                    try:
                        result = await control._dispatch(
                            {'operation': 'readiness-inventory'}
                        )
                    finally:
                        await control.stop()
                    self.assertEqual(result['kind'], 'discord_guild_inventory')
                    self.assertFalse(result['privacy']['tokens_included'])
                    self.assertEqual(
                        result['tester_role']['pinned_id'],
                        beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
                    )
                    self.assertEqual(guild.channels[0].name, 'todo-and-changelog')

    async def test_local_control_request_and_response_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_profile = profile(root)
            environment = {
                'POLYBOT_BETA_CONTROL': 'enabled',
                'POLYBOT_BETA_STARTUP_SYNC': 'disabled',
                'POLYBOT_ENV': 'development',
                'POLYBOT_BETA_APPLICATION_ID': str(beta_operations.BETA_APPLICATION_ID),
                'POLYBOT_BETA_GUILD_ID': str(beta_operations.BETA_GUILD_ID),
                'POLYBOT_BETA_DATABASE': beta_operations.BETA_DATABASE_NAME,
                'POLYBOT_BETA_DATABASE_ROLE': beta_operations.BETA_DATABASE_ROLE,
            }

            class Reader:
                def __init__(self, payload):
                    self.payload = payload

                async def readline(self):
                    return self.payload

            class Writer:
                def __init__(self):
                    self.payload = b''
                    self.closed = False

                def write(self, payload):
                    self.payload += payload

                async def drain(self):
                    return None

                def close(self):
                    self.closed = True

                async def wait_closed(self):
                    return None

            with mock.patch.dict(os.environ, environment, clear=False):
                control = beta_operations.BetaReleaseControl(
                    FakeBot(make_discord_world()), selected_profile, CHECKPOINT
                )
                oversized_request = Writer()
                await control._handle_client(
                    Reader(b'{' + b'x' * beta_operations.MAX_SOCKET_REQUEST_BYTES + b'}\n'),
                    oversized_request,
                )
                request_result = json.loads(oversized_request.payload)
                self.assertFalse(request_result['ok'])
                self.assertIn('too large', request_result['error'])

                with mock.patch.object(
                        control,
                        '_dispatch',
                        new=mock.AsyncMock(return_value={
                            'payload': 'x' * beta_operations.MAX_SOCKET_RESPONSE_BYTES,
                        })):
                    oversized_response = Writer()
                    await control._handle_client(
                        Reader(b'{"operation":"status"}\n'),
                        oversized_response,
                    )
                response_result = json.loads(oversized_response.payload)
                self.assertFalse(response_result['ok'])
                self.assertIn('response is too large', response_result['error'])


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)


class FakeDatabase:
    def __init__(self):
        self.events = []
        self.queries = []
        self.closed = False
        self.team_row = (1, 'Existing Team', beta_readiness.BETA_GUILD_ID, None, False, False, None, None, 2)
        self.house_row = (1, 'Existing House')
        self.fixture_rows = [
            (149, 'Beta Fixture Ready', False, False, False, False, None),
            (150, 'Beta Fixture Unconfirmed', False, False, True, False, None),
            (151, 'Beta Fixture Completed', True, True, True, False, None),
        ]
        self.leaderboard_players = [
            (100 + index, 9_000_000_000_100_000_000 + index)
            for index in range(1, 25)
        ]
        self.leaderboard_games = [(200 + index,) for index in range(48)]

    @contextmanager
    def connection_context(self):
        self.events.append('connection-open')
        try:
            yield self
        finally:
            self.events.append('connection-close')
            self.closed = True

    @contextmanager
    def atomic(self):
        self.events.append('transaction-open')
        try:
            yield self
        finally:
            self.events.append('transaction-close')

    def execute_sql(self, query, params=()):
        normalized = ' '.join(str(query).split()).lower()
        self.queries.append((normalized, tuple(params)))
        if normalized.startswith('set transaction read only'):
            return FakeCursor([])
        if 'current_database()' in normalized:
            return FakeCursor([('polytopia_dev', 'polybot_dev')])
        if 'count(*) from player' in normalized:
            return FakeCursor([(24,)])
        if 'count(*) from team' in normalized:
            return FakeCursor([(1,)])
        if 'count(*) from house' in normalized:
            return FakeCursor([(1,)])
        if 'count(*) from game' in normalized:
            return FakeCursor([(51,)])
        if normalized.startswith('select t.id'):
            return FakeCursor([self.team_row])
        if normalized.startswith('select id, name, is_completed'):
            return FakeCursor(self.fixture_rows)
        if normalized.startswith('select id, name from house'):
            return FakeCursor([self.house_row])
        if normalized.startswith('select p.id, dm.discord_id'):
            return FakeCursor(self.leaderboard_players)
        if normalized.startswith('select id from game'):
            return FakeCursor(self.leaderboard_games)
        raise AssertionError(f'unexpected query: {query!r}')


class DatabaseInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_database_profile_and_live_identity_refuse_wrong_targets(self):
        good = profile(self.root)
        beta_readiness.validate_database_profile(good)
        for field, value in (
                ('environment', 'production'),
                ('database_name', 'polytopia2'),
                ('database_user', 'postgres'),
                ('allowed_guild_ids', (1,)),
                ('api_enabled', True)):
            with self.subTest(field=field):
                with self.assertRaises(beta_readiness.ReadinessInventoryError):
                    beta_readiness.validate_database_profile(
                        profile(self.root, **{field: value})
                    )
        with self.assertRaises(beta_readiness.ReadinessInventoryError):
            beta_readiness.validate_live_database_identity('polytopia2', 'polybot_dev')

    def test_database_inventory_uses_read_only_worker_local_connection(self):
        fake_database = FakeDatabase()
        result = beta_readiness.read_development_database_inventory(
            profile=profile(self.root),
            database_factory=lambda _profile: fake_database,
        )
        self.assertEqual(
            fake_database.events,
            ['connection-open', 'transaction-open', 'transaction-close', 'connection-close'],
        )
        self.assertTrue(fake_database.closed)
        self.assertEqual(result['counts']['teams'], 1)
        self.assertEqual(result['fixtures']['beta_games']['games'][0]['id'], 149)
        self.assertEqual(result['fixtures']['leaderboard_showcase']['players']['count'], 24)
        self.assertEqual(result['fixtures']['leaderboard_showcase']['games']['count'], 48)
        self.assertFalse(result['role_binding_identifiers']['role_ids_resolved'])
        self.assertNotIn('"notes":', json.dumps(result).lower())
        json.dumps(result, allow_nan=False)
        mutation_tokens = ('insert ', 'update ', 'delete ', 'create table', 'alter table', 'drop table')
        self.assertFalse(any(any(token in query for token in mutation_tokens) for query, _ in fake_database.queries))

    def test_database_inventory_source_has_no_mutation_methods(self):
        source = inspect.getsource(beta_readiness.read_development_database_inventory)
        for forbidden in ('.create(', '.save(', '.delete(', '.execute(', 'db.atomic()'):
            if forbidden == 'db.atomic()':
                continue
            self.assertNotIn(forbidden, source)

    def test_database_inventory_invalid_connection_or_rows_fail_closed(self):
        with self.assertRaises(beta_readiness.ReadinessInventoryError):
            beta_readiness.read_development_database_inventory(
                profile=profile(self.root),
                database_factory=lambda _profile: (_ for _ in ()).throw(
                    RuntimeError('connection unavailable')
                ),
            )

        fake_database = FakeDatabase()
        fake_database.team_row = (1,)
        with self.assertRaises(beta_readiness.ReadinessInventoryError):
            beta_readiness.read_development_database_inventory(
                profile=profile(self.root),
                database_factory=lambda _profile: fake_database,
            )


def minimal_manifest():
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / 'readiness-manifests/template.json').read_text(encoding='utf-8'))


class ReadinessManifestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_repository_template_is_valid_and_keeps_unresolved_choices_explicit(self):
        value = minimal_manifest()
        normalized = beta_readiness.validate_readiness_manifest(value)
        self.assertEqual(normalized['target']['guild_id'], beta_readiness.BETA_GUILD_ID)
        self.assertEqual(normalized['discord']['tester_role']['expected_id'], beta_readiness.BETA_PINNED_TESTER_ROLE_ID)
        self.assertEqual(normalized['database']['teams']['proposed'], [])
        self.assertEqual(normalized['database']['houses']['proposed'], [])
        self.assertEqual(normalized['capabilities']['optional'][0]['decision'], 'unresolved')
        self.assertEqual(normalized['smoke']['tester_range'], {'minimum': 5, 'maximum': 20})

    def test_manifest_schema_bounds_unknown_fields_and_path_safety(self):
        value = minimal_manifest()
        with self.assertRaises(beta_readiness.ReadinessManifestError):
            beta_readiness.validate_readiness_manifest({**value, 'unknown': True})
        oversized = copy.deepcopy(value)
        oversized['smoke']['checklist'] = ['x'] * (beta_readiness.MAX_CHECKLIST_ITEMS + 1)
        with self.assertRaises(beta_readiness.ReadinessManifestError):
            beta_readiness.validate_readiness_manifest(oversized)
        invalid_range = copy.deepcopy(value)
        invalid_range['smoke']['tester_range'] = {'minimum': 4, 'maximum': 21}
        with self.assertRaises(beta_readiness.ReadinessManifestError):
            beta_readiness.validate_readiness_manifest(invalid_range)
        with self.assertRaises(beta_readiness.ReadinessManifestError):
            beta_readiness.validate_readiness_manifest({**value, 'discord': {'token': 'secret'}})

        manifest_path = self.root / 'manifest.json'
        manifest_path.write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(
            beta_readiness.safe_read_path(self.root, 'manifest.json', label='manifest'),
            manifest_path,
        )
        with self.assertRaises(beta_readiness.ReadinessPathError):
            beta_readiness.safe_read_path(self.root, '../manifest.json', label='manifest')
        link = self.root / 'link.json'
        link.symlink_to(manifest_path)
        with self.assertRaises(beta_readiness.ReadinessPathError):
            beta_readiness.safe_read_path(self.root, 'link.json', label='manifest')

    def test_offline_cli_validates_without_profile_or_database(self):
        output = io.StringIO()
        with mock.patch('sys.stdout', output), mock.patch.object(
                manage_beta_readiness,
                '_selected_profile',
                side_effect=AssertionError('offline validation loaded a profile')):
            result = manage_beta_readiness.main([
                '--json', 'validate', '--manifest', 'readiness-manifests/template.json'
            ])
        self.assertEqual(result, 0)
        self.assertIn('"status":"valid"', output.getvalue())


class ReadinessPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_plan_is_deterministic_diff_only_and_never_apply(self):
        manifest = minimal_manifest()
        discord_inventory = build_inventory(self.root)
        fake_database = FakeDatabase()
        database_inventory = beta_readiness.read_development_database_inventory(
            profile=profile(self.root),
            database_factory=lambda _profile: fake_database,
        )
        first = beta_readiness.plan_readiness(
            manifest=manifest,
            discord_inventory=discord_inventory,
            database_inventory=database_inventory,
        )
        second = beta_readiness.plan_readiness(
            manifest=manifest,
            discord_inventory=discord_inventory,
            database_inventory=database_inventory,
        )
        self.assertEqual(first, second)
        self.assertTrue(first['valid'])
        self.assertFalse(first['ready_for_live_apply'])
        self.assertFalse(first['boundaries']['discord_mutation_applied'])
        self.assertIn('tools_support', ' '.join(first['unresolved']))
        self.assertEqual(first['diff']['fixtures']['retention'][0]['missing'], [])

    def test_plan_reports_capability_changes_and_wrong_target(self):
        manifest = minimal_manifest()
        manifest['capabilities']['proposed'].append('tools_support')
        discord_inventory = build_inventory(self.root)
        fake_database = FakeDatabase()
        database_inventory = beta_readiness.read_development_database_inventory(
            profile=profile(self.root),
            database_factory=lambda _profile: fake_database,
        )
        database_inventory['target']['guild_id'] = 99
        result = beta_readiness.plan_readiness(
            manifest=manifest,
            discord_inventory=discord_inventory,
            database_inventory=database_inventory,
        )
        self.assertFalse(result['valid'])
        self.assertIn('tools_support', result['diff']['capabilities']['add'])
        self.assertTrue(any('target.guild_id' in error for error in result['errors']))
        self.assertFalse(result['ready_for_invitation'])

    def test_plan_malformed_snapshot_shapes_fail_closed_as_a_diff(self):
        manifest = minimal_manifest()
        result = beta_readiness.plan_readiness(
            manifest=manifest,
            discord_inventory={
                'schema_version': beta_readiness.DISCORD_INVENTORY_SCHEMA_VERSION,
                'kind': 'discord_guild_inventory',
                'target': [],
                'tester_role': [],
                'fixed_channels': [],
                'capabilities': [],
                'roles': None,
            },
            database_inventory={
                'schema_version': beta_readiness.DATABASE_INVENTORY_SCHEMA_VERSION,
                'kind': 'development_database_inventory',
                'target': [],
                'teams': None,
                'houses': None,
                'fixtures': [],
            },
        )
        self.assertFalse(result['valid'])
        self.assertFalse(result['ready_for_live_apply'])
        self.assertTrue(result['errors'])


if __name__ == '__main__':
    unittest.main()
