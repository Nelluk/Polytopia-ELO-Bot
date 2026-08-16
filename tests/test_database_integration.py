"""Explicitly gated integration tests for the development PostgreSQL database."""

import asyncio
from contextlib import contextmanager, nullcontext
import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import warnings

import peewee


INTEGRATION_FLAG = 'POLYBOT_RUN_DB_INTEGRATION'
RUN_DATABASE_INTEGRATION = os.environ.get(INTEGRATION_FLAG) == '1'

# discord.py 2.7.1 still imports Python 3.12's deprecated stdlib audioop
# module. Keep the integration gate strict except for this exact upstream
# warning, matching the default compatibility suite.
warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)


@unittest.skipUnless(
    RUN_DATABASE_INTEGRATION,
    f'set {INTEGRATION_FLAG}=1 to run development-database integration tests',
)
class DevelopmentDatabaseIntegrationTests(unittest.TestCase):
    """Exercise production-shaped code only after a strict database preflight."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get('POLYBOT_ENV') != 'development':
            raise RuntimeError(
                f'{INTEGRATION_FLAG}=1 requires POLYBOT_ENV=development'
            )

        import psycopg2
        from runtime_config import get_runtime_profile

        cls.profile = get_runtime_profile()
        if (
            cls.profile.environment != 'development'
            or cls.profile.database_name != 'polytopia_dev'
            or cls.profile.database_user != 'polybot_dev'
        ):
            raise RuntimeError(
                'Integration tests require the polytopia_dev database and '
                'polybot_dev role.'
            )
        if cls.profile.background_tasks_enabled or cls.profile.api_enabled:
            raise RuntimeError(
                'Integration tests require background tasks and the API to '
                'remain disabled.'
            )

        connection = psycopg2.connect(
            dbname=cls.profile.database_name,
            user=cls.profile.database_user,
            password=cls.profile.database_password,
            host=cls.profile.database_host,
            port=cls.profile.database_port,
        )
        try:
            connection.set_session(readonly=True, autocommit=True)
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_database(), current_user')
                database_name, database_user = cursor.fetchone()
        finally:
            connection.close()

        if (
            database_name != cls.profile.database_name
            or database_user != cls.profile.database_user
        ):
            raise RuntimeError(
                'PostgreSQL session identity does not match the development '
                'runtime profile.'
            )

        import settings
        from modules import models

        cls.settings = settings
        cls.models = models
        if cls.profile.guild_configuration_source == 'database':
            from modules import guild_configuration_runtime as runtime
            from modules import guild_configuration_shadow as shadow

            snapshot_value = os.environ.get(
                'POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT',
                '',
            ).strip()
            if not snapshot_value:
                raise RuntimeError(
                    'Database-backed integration tests require the reviewed '
                    'development guild-configuration snapshot.'
                )
            snapshot_path = Path(snapshot_value)
            if not snapshot_path.is_absolute() or not snapshot_path.is_file():
                raise RuntimeError(
                    'The development guild-configuration snapshot must be an '
                    'existing absolute path.'
                )
            discord_snapshot = json.loads(
                snapshot_path.read_text(encoding='utf-8')
            )
            cls.guild_configuration_discord_snapshot = discord_snapshot
            stored = asyncio.run(shadow.run_active_configuration(
                shadow.active_request_from_profile(cls.profile)
            ))
            active_guild_ids = tuple(value.guild_id for value in stored)
            settings.activate_database_guild_configuration(
                runtime.build_runtime_snapshot_from_stored(
                    stored_configurations=stored,
                    discord_snapshot=discord_snapshot,
                    allowed_guild_ids=active_guild_ids,
                )
            )
        cls.models.db.connect(reuse_if_open=True)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'models') and not cls.models.db.is_closed():
            cls.models.db.close()

    def setUp(self):
        self.models.db.connect(reuse_if_open=True)
        database_name, database_user = self.models.db.execute_sql(
            'SELECT current_database(), current_user'
        ).fetchone()
        self.assertEqual(database_name, 'polytopia_dev')
        self.assertEqual(database_user, 'polybot_dev')

    @contextmanager
    def rollback_scope(self):
        with self.models.db.atomic() as transaction:
            try:
                yield
            finally:
                transaction.rollback()

    def test_existing_schema_is_complete_without_model_import_ddl(self):
        expected_tables = {
            'apiapplication',
            'configuration',
            'discordmember',
            'game',
            'gameside',
            'lineup',
            'player',
            'team',
            'tribe',
        }
        rows = self.models.db.execute_sql(
            'SELECT table_name FROM information_schema.tables '
            "WHERE table_schema = 'public'"
        ).fetchall()
        actual_tables = {row[0] for row in rows}
        self.assertTrue(expected_tables.issubset(actual_tables))

    def test_startup_schema_preflight_is_read_only_and_complete(self):
        """Prove the ordinary startup contract on the stopped dev writer."""

        from modules import startup_schema_preflight as preflight
        from modules.database_schema_contract import REQUIRED_TABLES

        request = preflight.StartupSchemaPreflightRequest(
            database_name=self.profile.database_name,
            database_user=self.profile.database_user,
            database_password=self.profile.database_password,
            database_host=self.profile.database_host,
            database_port=self.profile.database_port,
        )
        self.models.db.close()
        try:
            result = preflight.inspect_startup_schema(request)
        finally:
            self.models.db.connect(reuse_if_open=True)

        self.assertEqual(result.database_name, 'polytopia_dev')
        self.assertEqual(result.database_user, 'polybot_dev')
        self.assertEqual(result.verified_tables, REQUIRED_TABLES)
        self.assertTrue(result.winner_foreign_key_verified)

    def test_production_timezone_tooling_is_idempotent_on_development_schema(self):
        """Exercise B1 transaction logic only after proving no DDL is needed."""

        import psycopg2
        from modules import player_timezone_production_migration as migration

        policy = migration.MigrationPolicy(
            environment='development',
            database_name='polytopia_dev',
            apply_confirmation='P9-B1-DEVELOPMENT-IDEMPOTENCY-TEST',
        )
        target = migration.MigrationTarget(
            environment=self.profile.environment,
            database_name=self.profile.database_name,
            database_user=self.profile.database_user,
        )
        self.models.db.close()
        connection = psycopg2.connect(
            dbname=self.profile.database_name,
            user=self.profile.database_user,
            password=self.profile.database_password,
            host=self.profile.database_host,
            port=self.profile.database_port,
        )
        try:
            before = migration.verify_migration(
                connection,
                target=target,
                policy=policy,
            )
            self.assertTrue(
                before.already_applied,
                'Development schema is missing a reviewed timezone column; '
                'refusing to let this test enter the apply path.',
            )
            applied = migration.apply_migration(
                connection,
                target=target,
                policy=policy,
                confirmation=policy.apply_confirmation,
            )
            self.assertTrue(applied.already_applied)
            after = migration.verify_migration(
                connection,
                target=target,
                policy=policy,
            )
            self.assertTrue(after.already_applied)
        finally:
            connection.close()
            self.models.db.connect(reuse_if_open=True)

    def test_registration_check_reads_real_schema_without_writes(self):
        from modules import registration_checks

        existing_id = (
            self.models.DiscordMember
            .select(self.models.DiscordMember.discord_id)
            .order_by(self.models.DiscordMember.discord_id)
            .scalar()
        )
        self.assertIsNotNone(existing_id)
        missing_id = 9_000_000_000_000_000_000

        self.models.db.close()
        found = asyncio.run(registration_checks.run_registration_check(
            registration_checks.RegistrationCheckRequest(
                discord_id=int(existing_id),
            )
        ))
        missing = asyncio.run(registration_checks.run_registration_check(
            registration_checks.RegistrationCheckRequest(
                discord_id=missing_id,
            )
        ))

        self.assertTrue(self.models.db.is_closed())
        self.assertTrue(found.registered)
        self.assertFalse(missing.registered)
        self.assertEqual(found.discord_id, int(existing_id))
        self.assertEqual(missing.discord_id, missing_id)
        self.models.db.connect(reuse_if_open=True)

    def test_readiness_inventory_reads_real_development_database_without_writes(self):
        from modules import beta_readiness

        result = beta_readiness.read_development_database_inventory(
            profile=self.profile,
            guild_id=beta_readiness.BETA_GUILD_ID,
        )
        self.assertEqual(
            result['schema_version'],
            beta_readiness.DATABASE_INVENTORY_SCHEMA_VERSION,
        )
        self.assertEqual(result['kind'], 'development_database_inventory')
        self.assertEqual(
            result['target'],
            {
                'environment': 'development',
                'guild_id': beta_readiness.BETA_GUILD_ID,
                'database': 'polytopia_dev',
                'database_role': 'polybot_dev',
            },
        )
        self.assertEqual(
            set(result['counts']),
            {'players', 'teams', 'houses', 'games'},
        )
        self.assertIsInstance(result['teams'], list)
        self.assertIsInstance(result['houses'], list)
        self.assertIsInstance(result['role_binding_identifiers'], dict)
        self.assertIn('beta_games', result['fixtures'])
        self.assertIn('leaderboard_showcase', result['fixtures'])
        self.assertFalse(result['privacy']['game_notes_included'])
        self.assertFalse(result['privacy']['tokens_included'])
        self.assertFalse(result['role_binding_identifiers']['role_ids_resolved'])
        self.assertLessEqual(
            len(result['teams']), beta_readiness.MAX_DATABASE_TEAMS
        )
        self.assertLessEqual(
            len(result['houses']), beta_readiness.MAX_DATABASE_HOUSES
        )

    def test_whattotest_fixture_readiness_reads_owned_bundle_without_writes(self):
        from modules import operator_beta_fixtures_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        before = tuple(
            self.models.Game.select(
                self.models.Game.id,
                self.models.Game.is_completed,
                self.models.Game.is_confirmed,
                self.models.Game.is_pending,
            )
            .where(
                (self.models.Game.guild_id == guild_id)
                & (
                    self.models.Game.notes
                    == workers.dev_fixtures.FIXTURE_NOTES_MARKER
                )
            )
            .order_by(self.models.Game.id)
            .tuples()
        )
        snapshot = asyncio.run(workers.run_readiness(
            workers.BetaFixtureReadRequest(guild_id=guild_id)
        ))
        after = tuple(
            self.models.Game.select(
                self.models.Game.id,
                self.models.Game.is_completed,
                self.models.Game.is_confirmed,
                self.models.Game.is_pending,
            )
            .where(
                (self.models.Game.guild_id == guild_id)
                & (
                    self.models.Game.notes
                    == workers.dev_fixtures.FIXTURE_NOTES_MARKER
                )
            )
            .order_by(self.models.Game.id)
            .tuples()
        )
        self.assertEqual(after, before)
        self.assertEqual(snapshot.game_ids, tuple(row[0] for row in before))
        self.assertIn(
            snapshot.readiness,
            {'ready', 'needs reset', 'needs preparation'},
        )
        self.assertTrue(all(
            isinstance(item, workers.BetaFixtureScenario)
            for item in snapshot.scenarios
        ))

    def test_beta_lab_combined_status_reads_real_owned_packs_without_writes(self):
        from modules import beta_lab_workers, beta_readiness

        guild_id = beta_readiness.BETA_GUILD_ID
        before = self.models.Game.select().count()
        status = asyncio.run(beta_lab_workers.run_status(guild_id))
        after = self.models.Game.select().count()

        self.assertEqual(status.guild_id, guild_id)
        self.assertEqual(
            tuple(pack.key for pack in status.packs),
            beta_lab_workers.PACKS,
        )
        self.assertEqual(before, after)

    def test_beta_lab_self_service_lane_round_trip_is_exact_and_rollback_safe(self):
        """Exercise a tester lane without retaining any development rows."""

        from modules import beta_lab_manifest, beta_lab_sessions, beta_readiness

        guild_id = beta_readiness.BETA_GUILD_ID
        reviewed_manifest = beta_lab_manifest.load(
            Path(__file__).resolve().parents[1]
        )
        suffix = uuid.uuid4().hex[:10]
        requester_id = 8_850_000_000_000_000 + (uuid.uuid4().int % 1_000_000)
        opponent_id = 8_851_000_000_000_000 + (uuid.uuid4().int % 1_000_000)
        manifest = beta_lab_manifest.BetaLabManifest(
            guild_id=guild_id,
            tester_role_id=reviewed_manifest.tester_role_id,
            opponent_user_ids=(opponent_id, opponent_id + 1),
            maximum_active_game_lanes=reviewed_manifest.maximum_active_game_lanes,
            lease_minutes=reviewed_manifest.lease_minutes,
        )
        request = beta_lab_sessions.BetaLabSessionRequest(
            guild_id=guild_id,
            requester_id=requester_id,
            requester_name=f'Lane Tester {suffix}',
            role_ids=(manifest.tester_role_id,),
        )

        claimed = None
        try:
            opponent_member = self.models.DiscordMember.create(
                discord_id=opponent_id,
                name=f'Lane Opponent {suffix}',
            )
            opponent = self.models.Player.create(
                discord_member=opponent_member,
                guild_id=guild_id,
                name=f'Lane Opponent {suffix}',
            )
            original_opponent_elo = (
                int(opponent.elo),
                int(opponent.elo_alltime),
                int(opponent.discord_member.elo),
                int(opponent.discord_member.elo_alltime),
            )
            member = self.models.DiscordMember.create(
                discord_id=requester_id,
                name=f'Lane Tester {suffix}',
            )
            self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=f'Lane Tester {suffix}',
            )
            with mock.patch.object(
                beta_lab_sessions,
                '_manifest',
                return_value=manifest,
            ):
                with mock.patch.object(
                    self.models.GameLog,
                    'write',
                    side_effect=peewee.OperationalError('injected lane audit failure'),
                ), self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected lane audit failure',
                ):
                    beta_lab_sessions.claim_session(
                        request,
                        now_epoch=1_900_000_000,
                        session_id_factory=lambda _size: '111111111111',
                    )
                self.models.db.connect(reuse_if_open=True)
                self.assertFalse(self.models.Game.select().where(
                    self.models.Game.notes.startswith(beta_lab_sessions.NOTES_PREFIX)
                    & self.models.Game.notes.contains('lease=111111111111')
                ).exists())

                claimed = beta_lab_sessions.claim_session(
                    request,
                    now_epoch=1_900_000_000,
                    session_id_factory=lambda _size: '222222222222',
                )
                self.assertEqual(claimed.requester_name, f'Lane Tester {suffix}')
                self.assertEqual(claimed.opponent_id, opponent_id)
                self.assertEqual(
                    tuple(item.scenario for item in claimed.scenarios),
                    beta_lab_sessions.SCENARIOS,
                )
                self.assertEqual(len(claimed.game_ids), 3)
                self.assertIsInstance(claimed.fingerprint, str)

                loaded = beta_lab_sessions.load_requester_session(
                    request,
                    now_epoch=1_900_000_000,
                )
                self.assertEqual(loaded, claimed)
                released = beta_lab_sessions.release_session(
                    beta_lab_sessions.BetaLabSessionReleaseRequest(
                        guild_id=guild_id,
                        requester_id=requester_id,
                        requester_name=f'Lane Tester {suffix}',
                        role_ids=(manifest.tester_role_id,),
                        session_id=claimed.session_id,
                        outcome='finished',
                    ),
                    now_epoch=1_900_000_001,
                )
                self.assertTrue(released.released)
                self.assertEqual(released.removed_game_ids, claimed.game_ids)
                self.assertIsNone(beta_lab_sessions.load_requester_session(
                    request,
                    now_epoch=1_900_000_001,
                ))
            self.models.db.connect(reuse_if_open=True)
            reloaded_opponent = self.models.Player.get_by_id(opponent.id)
            self.assertEqual(
                (
                    int(reloaded_opponent.elo),
                    int(reloaded_opponent.elo_alltime),
                    int(reloaded_opponent.discord_member.elo),
                    int(reloaded_opponent.discord_member.elo_alltime),
                ),
                original_opponent_elo,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            owned_games = tuple(self.models.Game.select().where(
                (self.models.Game.guild_id == guild_id)
                & self.models.Game.notes.startswith(
                    beta_lab_sessions.NOTES_PREFIX
                )
                & self.models.Game.notes.contains(f'owner={requester_id};')
            ))
            for game in sorted(
                owned_games,
                key=lambda item: (
                    item.completed_ts.timestamp()
                    if item.completed_ts is not None
                    else float('-inf')
                ),
                reverse=True,
            ):
                game.delete_game()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(str(requester_id))
            ).execute()
            for cleanup_discord_id in (requester_id, opponent_id):
                cleanup_member = self.models.DiscordMember.get_or_none(
                    self.models.DiscordMember.discord_id == cleanup_discord_id
                )
                if cleanup_member is not None:
                    self.models.Player.delete().where(
                        (self.models.Player.guild_id == guild_id)
                        & (self.models.Player.discord_member == cleanup_member.id)
                    ).execute()
                    cleanup_member.delete_instance()

    def test_wb13b_setup_is_rollback_isolated_and_preserves_retained_fixtures(self):
        """Exercise the real schema through the existing strict gate only."""

        from modules import beta_wider_setup

        guild_id = beta_wider_setup.beta_readiness.BETA_GUILD_ID
        fixture_ids = (149, 150, 151)
        before_fixture = tuple(
            self.models.Game.select(self.models.Game.id)
            .where(self.models.Game.id.in_(fixture_ids))
            .order_by(self.models.Game.id)
            .tuples()
        )
        before_showcase_games = self.models.Game.select().where(
            (self.models.Game.guild_id == guild_id)
            & (self.models.Game.id >= 200)
            & (self.models.Game.id <= 247)
        ).count()
        before_showcase_players = self.models.Player.select().join(
            self.models.DiscordMember
        ).where(
            (self.models.Player.guild_id == guild_id)
            & (self.models.DiscordMember.discord_id >= 9_000_000_000_100_000_001)
            & (self.models.DiscordMember.discord_id <= 9_000_000_000_100_000_024)
        ).count()

        manifest = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / 'readiness-manifests/wb1-3b-reviewed.json'
            ).read_text(encoding='utf-8')
        )
        with self.rollback_scope():
            with mock.patch.object(
                    beta_wider_setup,
                    '_read_state',
                    return_value=None), mock.patch.object(
                        beta_wider_setup,
                        '_write_state',
                        return_value=Path('/tmp/wb1-3b-integration-state.json')), mock.patch.object(
                            beta_wider_setup,
                            '_publish_state',
                            return_value=Path('/tmp/wb1-3b-integration-state.json')):
                try:
                    result = beta_wider_setup.seed_wider_beta_setup(
                        profile=self.profile,
                        manifest=manifest,
                        guild_id=guild_id,
                        database_factory=lambda _profile: self.models.db,
                        # The class gate owns the independently checked DB
                        # identity; this test never probes the durable beta lock.
                        writer_guard=lambda _profile: nullcontext(),
                    )
                except beta_wider_setup.WiderBetaSetupConflictError as exc:
                    issues = tuple(
                        issue.strip()
                        for issue in str(exc).split(';')
                        if issue.strip()
                    )
                    expected_issues = {
                        f"team '{team}' has incompatible {field} state"
                        for team in ('The Jets', 'The Ronin', 'The Sparkies')
                        for field in ('archived', 'house', 'league_tier')
                    }
                    if not issues or any(
                        issue not in expected_issues for issue in issues
                    ):
                        raise
                    self.skipTest(
                        'historical mirror retains incompatible WB1.3b '
                        'showcase Team state'
                    )
            self.assertEqual(result['kind'], 'wb1_3b_setup_seed_result')
            self.assertEqual(
                [item['name'] for item in result['state']['houses']],
                ['Beta House Alpha', 'Beta House Beta'],
            )
            self.assertEqual(
                [item['name'] for item in result['state']['teams']],
                ['The Ronin', 'The Jets', 'The Sparkies'],
            )
            self.assertEqual(
                [item['role_id'] for item in result['state']['role_bindings']],
                [item[2] for item in beta_wider_setup.EXPECTED_TEAMS],
            )
            for item in result['state']['teams']:
                row = self.models.db.execute_sql(
                    'SELECT t.guild_id, t.is_hidden, t.is_archived, '
                    't.league_tier, h.name '
                    'FROM team AS t JOIN house AS h ON h.id = t.house_id '
                    'WHERE t.id = %s',
                    (item['id'],),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], guild_id)
                self.assertFalse(row[1])
                self.assertFalse(row[2])
                self.assertIn(
                    row[3],
                    (
                        None,
                        beta_wider_setup.EXPECTED_SHOWCASE_TEAM_TIERS[
                            item['name']
                        ],
                    ),
                )
                self.assertEqual(row[4], item['baseline']['house_name'])

            self.assertEqual(
                tuple(
                    self.models.Game.select(self.models.Game.id)
                    .where(self.models.Game.id.in_(fixture_ids))
                    .order_by(self.models.Game.id)
                    .tuples()
                ),
                before_fixture,
            )
            self.assertEqual(
                self.models.Game.select().where(
                    (self.models.Game.guild_id == guild_id)
                    & (self.models.Game.id >= 200)
                    & (self.models.Game.id <= 247)
                ).count(),
                before_showcase_games,
            )
            self.assertEqual(
                self.models.Player.select().join(self.models.DiscordMember).where(
                    (self.models.Player.guild_id == guild_id)
                    & (self.models.DiscordMember.discord_id >= 9_000_000_000_100_000_001)
                    & (self.models.DiscordMember.discord_id <= 9_000_000_000_100_000_024)
                ).count(),
                before_showcase_players,
            )

        self.assertEqual(
            tuple(
                self.models.Game.select(self.models.Game.id)
                .where(self.models.Game.id.in_(fixture_ids))
                .order_by(self.models.Game.id)
                .tuples()
            ),
            before_fixture,
        )
        self.assertEqual(
            self.models.Game.select().where(
                (self.models.Game.guild_id == guild_id)
                & (self.models.Game.id >= 200)
                & (self.models.Game.id <= 247)
            ).count(),
            before_showcase_games,
        )
        self.assertEqual(
            self.models.Player.select().join(self.models.DiscordMember).where(
                (self.models.Player.guild_id == guild_id)
                & (self.models.DiscordMember.discord_id >= 9_000_000_000_100_000_001)
                & (self.models.DiscordMember.discord_id <= 9_000_000_000_100_000_024)
            ).count(),
            before_showcase_players,
        )

    def test_leaderboard_workers_read_real_schema(self):
        from modules import leaderboard_workers

        guild_id = self.settings.server_ids['test']
        requests = (
            (
                leaderboard_workers.run_player_leaderboard,
                leaderboard_workers.PlayerLeaderboardRequest(
                    guild_id=guild_id,
                    scope='global',
                    rating='peak',
                    era='all-time',
                    population='all',
                    active_cutoff=self.settings.date_cutoff,
                ),
                leaderboard_workers.PlayerLeaderboardResult,
                leaderboard_workers.PlayerLeaderboardRow,
            ),
            (
                leaderboard_workers.run_activity_leaderboard,
                leaderboard_workers.ActivityLeaderboardRequest(
                    guild_id=guild_id,
                    view='server-30-days',
                    recent_cutoff=(
                        datetime.datetime.now()
                        - datetime.timedelta(days=30)
                    ),
                ),
                leaderboard_workers.ActivityLeaderboardResult,
                leaderboard_workers.ActivityLeaderboardRow,
            ),
            (
                leaderboard_workers.run_squad_leaderboard,
                leaderboard_workers.SquadLeaderboardRequest(
                    guild_id=guild_id,
                    period='all-time',
                    active_cutoff=self.settings.date_cutoff,
                ),
                leaderboard_workers.SquadLeaderboardResult,
                leaderboard_workers.SquadLeaderboardRow,
            ),
        )
        results = []
        for runner, request, result_type, row_type in requests:
            with self.subTest(result_type=result_type.__name__):
                result = asyncio.run(runner(request))
                results.append(result)
                self.assertIsInstance(result, result_type)
                self.assertIsInstance(result.rows, tuple)
                self.assertEqual(
                    [row.rank for row in result.rows],
                    list(range(1, len(result.rows) + 1)),
                )
                for row in result.rows:
                    self.assertIsInstance(row, row_type)

        player_result = results[0]
        self.assertGreaterEqual(
            player_result.total_ranked,
            len(player_result.rows),
        )
        if not self.settings.servers_included_in_global_lb():
            self.assertEqual(player_result.total_ranked, 0)
            self.assertEqual(player_result.rows, ())
        for row in player_result.rows:
            self.assertIsInstance(row.name, str)
            self.assertIsInstance(row.elo, int)
            self.assertIsInstance(row.wins, int)
            self.assertIsInstance(row.losses, int)
            self.assertIsInstance(row.team_emoji, str)

    def test_team_leaderboard_worker_reads_real_schema_without_writes(self):
        """Read-only gate for the P7.10 team snapshot on the next beta window."""

        from modules import leaderboard_workers

        guild_id = self.settings.server_ids['test']
        database_guild_id = self.settings.server_ids['polychampions']
        before = (
            self.models.Team.select().count(),
            self.models.GameSide.select().count(),
        )
        result = asyncio.run(
            leaderboard_workers.run_team_leaderboard(
                leaderboard_workers.TeamLeaderboardRequest(
                    guild_id=guild_id,
                    database_guild_id=database_guild_id,
                    include_archived=True,
                    load_all_filters=True,
                    team_enabled=True,
                    channel_allowed=True,
                    graph_attachment_name='gated-team-leaderboard.png',
                )
            )
        )
        self.assertIsInstance(result, leaderboard_workers.TeamLeaderboardResult)
        self.assertIsInstance(result.rows, tuple)
        self.assertEqual(
            [row.rank for row in result.rows],
            list(range(1, len(result.rows) + 1)),
        )
        for row in result.rows:
            self.assertIsInstance(row, leaderboard_workers.TeamLeaderboardRow)
            self.assertIsInstance(row.elo, int)
            self.assertIsInstance(row.wins, int)
            self.assertIsInstance(row.losses, int)
        self.assertEqual(
            before,
            (
                self.models.Team.select().count(),
                self.models.GameSide.select().count(),
            ),
        )

    def test_role_leaderboard_worker_reads_real_schema_without_writes(self):
        """Read-only P7.13 gate for the next approved stopped-writer window."""

        from modules import role_leaderboard_workers

        guild_id = self.settings.server_ids['test']
        member_ids = tuple(
            discord_id
            for _player_id, discord_id in (
                self.models.Player
                .select(
                    self.models.Player.id,
                    self.models.DiscordMember.discord_id,
                )
                .join(self.models.DiscordMember)
                .where(self.models.Player.guild_id == guild_id)
                .order_by(self.models.Player.id)
                .limit(role_leaderboard_workers.MAX_ROLE_MEMBER_SNAPSHOTS)
                .tuples()
            )
        )
        if not member_ids:
            self.skipTest('development guild has no registered player fixture')
        request = role_leaderboard_workers.RoleLeaderboardRequest(
            guild_id=guild_id,
            selected_role_ids=(1,),
            selected_role_names=('Integration role',),
            member_snapshots=tuple(
                role_leaderboard_workers.RoleLeaderboardMemberSnapshot(
                    discord_id=int(discord_id),
                    name=f'Integration {discord_id}',
                    role_ids=(1,),
                )
                for discord_id in member_ids
            ),
            role_snapshots=(
                role_leaderboard_workers.RoleLeaderboardRoleSnapshot(
                    role_id=1,
                    name='Integration role',
                ),
            ),
            inactive_role_id=None,
            global_guild_ids=tuple(
                self.settings.servers_included_in_global_lb()
            ),
            recent_cutoff=(
                datetime.datetime.now()
                - datetime.timedelta(days=14)
            ),
        )
        before = (
            self.models.Game.select().count(),
            self.models.Lineup.select().count(),
        )
        result = asyncio.run(
            role_leaderboard_workers.run_role_leaderboard(request)
        )
        self.assertIsInstance(result, role_leaderboard_workers.RoleLeaderboardResult)
        self.assertIsInstance(result.rows, tuple)
        page = role_leaderboard_workers.role_leaderboard_page(
            result,
            selected_role_ids=(1,),
            selected_role_names=('Integration role',),
            match_mode='all',
            sort_key='global_elo',
            scope='global',
        )
        self.assertEqual(
            [row.rank for row in page.rows],
            list(range(1, len(page.rows) + 1)),
        )
        for row in page.rows:
            self.assertIsInstance(row, role_leaderboard_workers.RoleLeaderboardRow)
            self.assertIsInstance(row.global_elo, int)
            self.assertIsInstance(row.local_elo, int)
            self.assertIsInstance(row.global_wins, int)
            self.assertIsInstance(row.local_wins, int)
        self.assertEqual(
            before,
            (
                self.models.Game.select().count(),
                self.models.Lineup.select().count(),
            ),
        )

    def test_squad_show_worker_reads_real_schema_without_writes(self):
        """Read-only P7.11 exact and requester-discovery regression gate."""

        from modules import squad_show_workers

        guild_id = self.settings.server_ids['polychampions']
        player = (
            self.models.Player
            .select(self.models.Player, self.models.DiscordMember)
            .join(self.models.DiscordMember)
            .where(self.models.Player.guild_id == guild_id)
            .order_by(self.models.Player.id)
            .first()
        )
        if player is None:
            self.skipTest('development guild has no registered player fixture')
        before = (
            self.models.Squad.select().count(),
            self.models.SquadMember.select().count(),
            self.models.GameSide.select().count(),
        )
        squad = (
            self.models.Squad
            .select()
            .where(self.models.Squad.guild_id == guild_id)
            .order_by(self.models.Squad.id)
            .first()
        )
        if squad is not None:
            result = asyncio.run(
                squad_show_workers.run_squad_show(
                    squad_show_workers.SquadShowRequest(
                        guild_id=guild_id,
                        requester_id=1,
                        member_ids=(1,),
                        squad_id=int(squad.id),
                        team_enabled=True,
                        channel_allowed=True,
                    )
                )
            )
            self.assertIsInstance(result, squad_show_workers.SquadShowResult)
            self.assertEqual(len(result.cards), 1)
            self.assertEqual(result.cards[0].squad_id, int(squad.id))
            self.assertIsInstance(result.cards[0].members, tuple)

        requester_id = int(player.discord_member.discord_id)
        started = time.monotonic()
        discovery = asyncio.run(
            squad_show_workers.run_squad_show(
                squad_show_workers.SquadShowRequest(
                    guild_id=guild_id,
                    requester_id=requester_id,
                    member_ids=(requester_id,),
                    team_enabled=True,
                    channel_allowed=True,
                )
            )
        )
        elapsed = time.monotonic() - started
        self.assertIsInstance(discovery, squad_show_workers.SquadShowResult)
        self.assertLessEqual(
            len(discovery.cards),
            squad_show_workers.MAX_SQUAD_MATCHES,
        )
        self.assertLess(
            elapsed,
            5.0,
            f'requester squad discovery took {elapsed:.3f}s',
        )
        self.assertEqual(
            before,
            (
                self.models.Squad.select().count(),
                self.models.SquadMember.select().count(),
                self.models.GameSide.select().count(),
            ),
        )

    def test_squad_identity_worker_commit_and_outer_rollback(self):
        """Gated P7.12 write/audit coverage for a stopped-writer window."""

        from modules import squad_identity_workers

        squad = self.models.Squad.select().order_by(self.models.Squad.id).first()
        if squad is None:
            self.skipTest('development database has no squad fixture')
        members = tuple(squad.get_members())
        if not members:
            self.skipTest('development squad has no member fixture')
        actor_id = int(members[0].discord_member.discord_id)
        marker = f'p7-12-rollback-{uuid.uuid4().hex[:12]}'
        before_name = str(squad.name or '')
        request = squad_identity_workers.SquadNameMutationRequest(
            guild_id=int(squad.guild_id),
            squad_id=int(squad.id),
            requester_id=actor_id,
            requester_is_staff=False,
            requester_description=f'**P7.12 test** (`{actor_id}`)',
            name=marker,
        )

        with self.rollback_scope():
            # This gated case owns the main-thread connection and outer
            # rollback. Offline tests independently prove that the production
            # worker opens and closes its own connection; avoid nesting that
            # lifecycle inside this test-owned transaction.
            with mock.patch.object(
                self.models.db,
                'connection_context',
                return_value=nullcontext(),
            ):
                result = squad_identity_workers.set_squad_name(request)
            self.assertEqual(result.name, marker)
            self.assertEqual(
                self.models.Squad.get_by_id(squad.id).name,
                marker,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

        self.assertEqual(
            self.models.Squad.get_by_id(squad.id).name,
            before_name,
        )
        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count(),
            0,
        )

    def test_representative_write_is_rolled_back(self):
        marker = f'phase6-rollback-{uuid.uuid4()}'
        with self.rollback_scope():
            record = self.models.GameLog.create(
                guild_id=self.profile.allowed_guild_ids[0],
                message=marker,
            )
            self.assertGreater(record.id, 0)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message == marker
                ).count(),
                1,
            )
        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message == marker
            ).count(),
            0,
        )

    def test_player_workspace_reads_real_schema(self):
        from modules import player_workers

        guild_id = self.profile.allowed_guild_ids[0]
        player = (
            self.models.Player.select()
            .join(self.models.DiscordMember)
            .where(self.models.Player.guild_id == guild_id)
            .first()
        )
        if player is None:
            self.skipTest('development guild has no registered player')
        result = asyncio.run(player_workers.run_player_workspace(
            player_workers.PlayerWorkspaceRequest(
                guild_id=guild_id,
                discord_id=player.discord_member.discord_id,
                requester_discord_id=player.discord_member.discord_id,
            )
        ))
        self.assertEqual(result.player_id, player.id)
        self.assertEqual(result.discord_id, player.discord_member.discord_id)
        self.assertIsInstance(result.games, tuple)
        for row in result.games:
            self.assertIsInstance(row, player_workers.PlayerGameRow)
        self.assertIsInstance(result.squads, tuple)
        self.assertLessEqual(
            len(result.squads),
            player_workers.MAX_PROFILE_SQUADS,
        )
        self.assertGreaterEqual(result.squad_total, len(result.squads))
        for squad in result.squads:
            self.assertIsInstance(squad, player_workers.PlayerSquadSummary)
            self.assertIsInstance(squad.member_names, tuple)
            self.assertGreaterEqual(squad.games_played, 0)
        self.assertIsInstance(result.local_history, tuple)
        self.assertIsInstance(result.global_history, tuple)
        for point in (*result.local_history, *result.global_history):
            self.assertIsInstance(point, player_workers.PlayerRatingPoint)
        graph = asyncio.run(player_workers.run_player_history_graph(
            result,
            'current',
        ))
        self.assertIsInstance(graph, player_workers.PlayerHistoryGraph)
        if any(
            point.current_elo is not None
            for point in (*result.local_history, *result.global_history)
        ):
            self.assertTrue(graph.png_bytes.startswith(b'\x89PNG\r\n\x1a\n'))

    def test_player_registration_worker_commits_and_rolls_back_real_graph(self):
        from modules import player_registration_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        actor_id = self.settings.owner_id
        target_id = 8_900_000_000_000_000 + uuid.uuid4().int % 1_000_000
        rollback_target_id = target_id + 1
        canonical_name = f'P61 Canonical {suffix}'
        rollback_name = f'P61 Rollback {suffix}'

        actor = player_registration_workers.MemberSnapshot(
            discord_id=actor_id,
            discord_name='P61 Integration Actor',
            discord_nick=None,
            display_name='P61 Integration Actor',
            role_names=(),
        )

        def make_request(discord_id, name):
            target = player_registration_workers.MemberSnapshot(
                discord_id=discord_id,
                discord_name=f'P61 Target {suffix}',
                discord_nick='P61 Nick',
                display_name=f'P61 Target {suffix}',
                role_names=(),
            )
            return player_registration_workers.PlayerRegistrationRequest(
                guild_id=guild_id,
                requester_id=actor_id,
                actor=actor,
                target=target,
                canonical_name=name,
                requester_is_staff=True,
                invoked_with='integration',
            )

        def cleanup(discord_ids):
            with self.models.db.atomic():
                for discord_id in discord_ids:
                    member = self.models.DiscordMember.get_or_none(
                        discord_id=discord_id,
                    )
                    if member is not None:
                        self.models.Player.delete().where(
                            self.models.Player.discord_member == member,
                        ).execute()
                        self.models.DiscordMember.delete().where(
                            self.models.DiscordMember.discord_id == discord_id,
                        ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(suffix),
                ).execute()

        try:
            result = asyncio.run(
                player_registration_workers.run_player_registration(
                    make_request(target_id, canonical_name),
                )
            )
            self.assertEqual(result.guild_id, guild_id)
            self.assertEqual(result.target_id, target_id)
            saved = self.models.DiscordMember.get(
                self.models.DiscordMember.discord_id == target_id,
            )
            self.assertEqual(saved.polytopia_name, canonical_name)
            self.assertIsNone(saved.name_steam)
            self.assertIsNone(saved.polytopia_id)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(canonical_name),
                    self.models.GameLog.guild_id == guild_id,
                ).count(),
                1,
            )

            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P61 audit failure'),
            ):
                with self.assertRaises(peewee.OperationalError):
                    asyncio.run(
                        player_registration_workers.run_player_registration(
                            make_request(rollback_target_id, rollback_name),
                        )
                    )
            self.assertIsNone(
                self.models.DiscordMember.get_or_none(
                    discord_id=rollback_target_id,
                )
            )
            self.assertEqual(
                self.models.Player.select().join(self.models.DiscordMember).where(
                    self.models.DiscordMember.discord_id == rollback_target_id,
                ).count(),
                0,
            )
        finally:
            cleanup((target_id, rollback_target_id))

    def test_league_join_worker_uses_real_schema_and_exact_cleanup(self):
        """Exercise P8.11's legacy-compatible Player upsert boundary."""

        from modules import league_user_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex[:12]
        discord_id = 8_910_000_000_000_000 + uuid.uuid4().int % 1_000_000
        member = None
        try:
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=f'P811 {suffix}',
            )
            self.assertEqual(
                self.models.Player.select().where(
                    (self.models.Player.discord_member == member)
                    & (self.models.Player.guild_id == guild_id)
                ).count(),
                0,
            )
            result = asyncio.run(
                league_user_workers.run_join_eligibility(
                    league_user_workers.LeagueJoinRequest(
                        guild_id=guild_id,
                        requester_id=discord_id,
                        requester_name=f'P811 {suffix}',
                        requester_nick='',
                        league_scope=True,
                    )
                )
            )
            self.assertTrue(result.registered)
            self.assertTrue(result.local_player_created)
            self.assertEqual(result.guild_id, guild_id)
            self.assertEqual(
                self.models.Player.select().where(
                    (self.models.Player.discord_member == member)
                    & (self.models.Player.guild_id == guild_id)
                ).count(),
                1,
            )
            self.assertIsInstance(result.team_roles, tuple)
        finally:
            with self.models.db.atomic():
                if member is None:
                    member = self.models.DiscordMember.get_or_none(
                        self.models.DiscordMember.discord_id == discord_id
                    )
                if member is not None:
                    self.models.Player.delete().where(
                        self.models.Player.discord_member == member
                    ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.discord_id == discord_id
                ).execute()

    def test_team_creation_worker_commits_and_rolls_back_real_graph(self):
        """Exercise the new Team+GameLog graph under the unchanged gate."""

        from modules import team_creation_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        actor_id = self.settings.owner_id
        team_name = f'P85 Team {suffix}'
        rollback_name = f'P85 Rollback {suffix}'

        def make_request(name):
            return team_creation_workers.TeamCreationRequest(
                guild_id=guild_id,
                requester_id=actor_id,
                requester_is_mod=True,
                team_enabled=True,
                name=name,
                requester_description=f'**P85 Integration Actor** (`{actor_id}`)',
                native=True,
                invoked_with='integration',
            )

        try:
            result = asyncio.run(
                team_creation_workers.run_team_creation(
                    make_request(team_name),
                )
            )
            self.assertEqual(result.guild_id, guild_id)
            self.assertEqual(result.team_name, team_name)
            saved = self.models.Team.get_by_id(result.team_id)
            self.assertEqual(saved.name, team_name)
            self.assertEqual(saved.guild_id, guild_id)
            self.assertFalse(saved.is_hidden)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.guild_id == guild_id,
                    self.models.GameLog.message.contains(team_name),
                ).count(),
                1,
            )

            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P85 audit failure'),
            ):
                with self.assertRaises(peewee.OperationalError):
                    asyncio.run(
                        team_creation_workers.run_team_creation(
                            make_request(rollback_name),
                        )
                    )
            self.assertIsNone(
                self.models.Team.get_or_none(
                    (self.models.Team.guild_id == guild_id)
                    & (self.models.Team.name == rollback_name)
                )
            )
        finally:
            # The committed success is intentionally cleaned up even if a
            # later assertion fails; the audit-failure case is transaction
            # rolled back by the worker itself.
            with self.models.db.atomic():
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(suffix)
                ).execute()
                self.models.Team.delete().where(
                    (self.models.Team.guild_id == guild_id)
                    & self.models.Team.name.contains(suffix)
                ).execute()

    def test_team_archive_worker_commits_and_rolls_back_real_graph(self):
        """Exercise the native Team archive+GameLog graph under the gate."""

        from modules import team_archive_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        actor_id = self.settings.owner_id
        team_name = f'P826 Archive {suffix}'
        rollback_name = f'P826 Rollback {suffix}'
        committed_team = None
        rollback_team = None

        def make_request(team):
            return team_archive_workers.TeamArchiveRequest(
                guild_id=guild_id,
                requester_id=actor_id,
                requester_is_mod=True,
                team_enabled=True,
                league_scope=True,
                team_lookup=team.name,
                expected_team_id=team.id,
                team_role_id=900000 + team.id,
                team_role_name=team.name,
                requester_description=(
                    f'**P826 Integration Actor** (`{actor_id}`)'
                ),
                confirmed=True,
                invoked_with='integration',
            )

        try:
            committed_team = self.models.Team.create(
                name=team_name,
                guild_id=guild_id,
                is_hidden=False,
                is_archived=False,
            )
            result = asyncio.run(
                team_archive_workers.run_team_archive(
                    make_request(committed_team),
                )
            )
            self.assertEqual(result.team_id, committed_team.id)
            committed_team = self.models.Team.get_by_id(committed_team.id)
            self.assertTrue(committed_team.is_archived)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.guild_id == guild_id,
                    self.models.GameLog.message.contains(team_name),
                ).count(),
                1,
            )

            rollback_team = self.models.Team.create(
                name=rollback_name,
                guild_id=guild_id,
                is_hidden=False,
                is_archived=False,
            )
            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P826 audit failure'),
            ):
                with self.assertRaises(peewee.OperationalError):
                    asyncio.run(
                        team_archive_workers.run_team_archive(
                            make_request(rollback_team),
                        )
                    )
            rollback_team = self.models.Team.get_by_id(rollback_team.id)
            self.assertFalse(rollback_team.is_archived)
        finally:
            with self.models.db.atomic():
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(suffix)
                ).execute()
                self.models.Team.delete().where(
                    (self.models.Team.guild_id == guild_id)
                    & self.models.Team.name.contains(suffix)
                ).execute()

    def test_game_search_workspace_reads_real_schema(self):
        from modules import game_search_workers

        guild_id = self.profile.allowed_guild_ids[0]
        result = asyncio.run(game_search_workers.run_game_search(
            game_search_workers.GameSearchRequest(
                guild_id=guild_id,
                requester_discord_id=self.settings.owner_id,
                staff=True,
            )
        ))
        self.assertIsInstance(
            result,
            game_search_workers.GameSearchSnapshot,
        )
        self.assertIsInstance(result.rows, tuple)
        for row in result.rows:
            self.assertIsInstance(row, game_search_workers.GameSearchRow)
            self.assertGreater(row.game_id, 0)

    def test_game_detail_worker_reads_real_schema(self):
        from modules import game_detail_workers

        guild_id = self.profile.allowed_guild_ids[0]
        game = (
            self.models.Game.select()
            .where(self.models.Game.guild_id == guild_id)
            .order_by(self.models.Game.id.desc())
            .first()
        )
        if game is None:
            self.skipTest('development guild has no game to inspect')

        result = asyncio.run(game_detail_workers.run_game_detail(
            game_detail_workers.GameDetailRequest(
                guild_id=guild_id,
                channel_id=int(game.game_chan or 0),
                requester_discord_id=self.settings.owner_id,
                game_id=game.id,
            )
        ))
        self.assertIsInstance(
            result,
            game_detail_workers.GameDetailSnapshot,
        )
        self.assertEqual(result.game_id, game.id)
        self.assertEqual(result.guild_id, game.guild_id)
        self.assertIsInstance(result.sides, tuple)
        for side in result.sides:
            self.assertIsInstance(side, game_detail_workers.GameDetailSide)
            self.assertIsInstance(side.lineups, tuple)

    def test_team_show_worker_reads_real_schema_without_writes(self):
        """Read one known development Team through the P8.6 worker gate."""

        from modules import team_show_workers

        guild_id = self.profile.allowed_guild_ids[0]
        team = (
            self.models.Team.select()
            .where(
                (self.models.Team.guild_id == guild_id)
                & (self.models.Team.is_hidden == 0)
            )
            .order_by(self.models.Team.id)
            .first()
        )
        if team is None:
            self.skipTest('development guild has no visible team to inspect')

        request = team_show_workers.TeamShowRequest(
            guild_id=guild_id,
            requester_id=self.settings.owner_id,
            team_lookup=team.name,
            activity_mode=team_show_workers.TEAM_ACTIVITY_RECENT,
            team_enabled=bool(
                self.settings.guild_setting(guild_id, 'allow_teams')
            ),
            channel_allowed=True,
            leadership_enabled=False,
            inactive_role_name=None,
            guild_snapshot=team_show_workers.TeamShowGuildSnapshot(
                guild_id=guild_id,
                roles=(),
                members=(),
            ),
            team_elo_reset_label=str(self.settings.team_elo_reset_date),
            requester_description='integration read',
            native=True,
            invoked_with='integration',
            prefix='$',
        )
        result = asyncio.run(team_show_workers.run_team_show(request))
        self.assertEqual(result.team_id, team.id)
        self.assertEqual(result.guild_id, guild_id)
        self.assertIsInstance(result.roster_rows, tuple)
        self.assertIsInstance(result.recent_games, tuple)
        self.assertIsInstance(result.graph_bytes, bytes)

    def test_house_show_worker_reads_real_schema_without_writes(self):
        """Read one known House through the P8.7 worker gate."""

        from modules import house_show_workers

        guild_id = self.profile.allowed_guild_ids[0]
        house = self.models.House.select().order_by(self.models.House.id).first()
        if house is None:
            self.skipTest('development database has no House to inspect')
        before = (
            self.models.House.select().count(),
            self.models.Team.select().where(
                self.models.Team.guild_id == guild_id
            ).count(),
            self.models.Player.select().where(
                self.models.Player.guild_id == guild_id
            ).count(),
        )
        result = asyncio.run(
            house_show_workers.run_house_show(
                house_show_workers.HouseShowRequest(
                    guild_id=guild_id,
                    requester_id=self.settings.owner_id,
                    house_lookup=house.name,
                    require_selection=True,
                    league_scope=True,
                    channel_allowed=True,
                    inactive_role_name=None,
                    guild_snapshot=house_show_workers.HouseGuildSnapshot(
                        guild_id=guild_id,
                        members=(),
                        role_names=(),
                    ),
                )
            )
        )
        self.assertEqual(result.selected_house_id, house.id)
        self.assertTrue(any(row.house_id == house.id for row in result.houses))
        self.assertIsInstance(result.houses, tuple)
        self.assertEqual(
            before,
            (
                self.models.House.select().count(),
                self.models.Team.select().where(
                    self.models.Team.guild_id == guild_id
                ).count(),
                self.models.Player.select().where(
                    self.models.Player.guild_id == guild_id
                ).count(),
            ),
        )

    def test_house_attribute_worker_reads_and_rolls_back_real_schema(self):
        """Exercise P8.8 read and audit-failure rollback under the dev gate."""

        from modules import house_attributes_workers

        guild_id = self.profile.allowed_guild_ids[0]
        house = self.models.House.select().order_by(self.models.House.id).first()
        if house is None:
            self.skipTest('development database has no House to inspect')
        original_name = str(house.name)
        read_result = asyncio.run(
            house_attributes_workers.run_house_attribute_read(
                house_attributes_workers.HouseAttributeReadRequest(
                    guild_id=guild_id,
                    requester_id=self.settings.owner_id,
                    requester_is_mod=True,
                    league_scope=True,
                    channel_allowed=True,
                    house_lookup=original_name,
                    requester_role_names=(),
                    attribute=house_attributes_workers.HOUSE_ATTRIBUTE_NAME,
                    requester_description='P8.8 integration actor',
                )
            )
        )
        self.assertEqual(read_result.house_id, house.id)
        rollback_name = f'P88 Rollback {uuid.uuid4().hex}'
        with mock.patch.object(
            self.models.GameLog,
            'write',
            side_effect=peewee.OperationalError('P8.8 audit failure'),
        ):
            with self.assertRaises(peewee.OperationalError):
                asyncio.run(
                    house_attributes_workers.run_house_attribute_mutation(
                        house_attributes_workers.HouseAttributeMutationRequest(
                            guild_id=guild_id,
                            requester_id=self.settings.owner_id,
                            requester_is_mod=True,
                            league_scope=True,
                            channel_allowed=True,
                            house_id=house.id,
                            attribute=house_attributes_workers.HOUSE_ATTRIBUTE_NAME,
                            value=rollback_name,
                            image_operation=None,
                            staged_path=None,
                            expected_name=read_result.house_name,
                            expected_image_url=read_result.image_url,
                            expected_local_digest=read_result.local_image_digest,
                            requester_description='P8.8 integration actor',
                        )
                    )
                )
        self.assertEqual(
            self.models.House.get_by_id(house.id).name,
            original_name,
        )
        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message.contains(rollback_name)
            ).count(),
            0,
        )

    def test_house_creation_worker_commits_and_rolls_back_real_schema(self):
        """Exercise P8.9 House+audit commit and rollback under the dev gate."""

        from modules import house_attributes_workers

        guild_id = self.profile.allowed_guild_ids[0]
        house_name = f'P89 {uuid.uuid4().hex}'
        request = house_attributes_workers.HouseCreationRequest(
            guild_id=guild_id,
            requester_id=self.settings.owner_id,
            requester_is_mod=True,
            league_scope=True,
            channel_allowed=True,
            name=house_name,
            requester_description='P8.9 integration actor',
        )
        try:
            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P8.9 audit failure'),
            ):
                with self.assertRaises(peewee.OperationalError):
                    asyncio.run(
                        house_attributes_workers.run_house_creation(request)
                    )
            self.assertEqual(
                self.models.House.select().where(
                    self.models.House.name == house_name
                ).count(),
                0,
            )

            result = asyncio.run(
                house_attributes_workers.run_house_creation(request)
            )
            self.assertEqual(result.house_name, house_name)
            self.assertEqual(
                self.models.House.select().where(
                    self.models.House.id == result.house_id
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(house_name)
                ).count(),
                1,
            )
        finally:
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(house_name)
            ).execute()
            self.models.House.delete().where(
                self.models.House.name == house_name
            ).execute()

    def test_league_tokens_worker_reads_commits_and_rolls_back_real_schema(self):
        """Exercise P8.10 bounded read and House+audit transaction."""

        from modules import league_tokens_workers

        guild_id = self.profile.allowed_guild_ids[0]
        house = self.models.House.select().order_by(self.models.House.id).first()
        if house is None:
            self.skipTest('development database has no House to inspect')
        original_balance = int(house.league_tokens)
        new_balance = original_balance + 1
        if new_balance > league_tokens_workers.MAX_TOKEN_BALANCE:
            new_balance = original_balance - 1
        suffix = f'P8.10 {uuid.uuid4().hex}'

        read_result = asyncio.run(
            league_tokens_workers.run_league_tokens_read(
                league_tokens_workers.LeagueTokensReadRequest(
                    guild_id=guild_id,
                    requester_id=self.settings.owner_id,
                    requester_level=1,
                    league_scope=True,
                    house_lookup=house.name,
                )
            )
        )
        selected = next(
            row for row in read_result.houses if row.house_id == house.id
        )
        self.assertEqual(selected.balance, original_balance)

        request = league_tokens_workers.LeagueTokensMutationRequest(
            guild_id=guild_id,
            requester_id=self.settings.owner_id,
            requester_level=5,
            league_scope=True,
            house_id=house.id,
            expected_house_name=house.name,
            expected_balance=original_balance,
            new_balance=new_balance,
            note=suffix,
            requester_description='P8.10 integration actor',
        )
        try:
            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P8.10 audit failure'),
            ):
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'P8.10 audit failure',
                ):
                    asyncio.run(
                        league_tokens_workers.run_league_tokens_mutation(request)
                    )
            self.assertEqual(
                int(self.models.House.get_by_id(house.id).league_tokens),
                original_balance,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                0,
            )

            result = asyncio.run(
                league_tokens_workers.run_league_tokens_mutation(request)
            )
            self.assertEqual(result.old_balance, original_balance)
            self.assertEqual(result.new_balance, new_balance)
            self.assertEqual(
                int(self.models.House.get_by_id(house.id).league_tokens),
                new_balance,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                1,
            )
        finally:
            self.models.House.update(
                league_tokens=original_balance
            ).where(self.models.House.id == house.id).execute()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(suffix)
            ).execute()
            self.assertEqual(
                int(self.models.House.get_by_id(house.id).league_tokens),
                original_balance,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                0,
            )

    def test_development_fixture_seed_status_cleanup_round_trip(self):
        from modules import dev_fixtures

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex[:10]
        base_discord_id = (
            8_800_000_000_000_000 + uuid.uuid4().int % 1_000_000
        )
        discord_ids = (base_discord_id, base_discord_id + 1)
        ordinary_game_id = None
        created_fixture_ids = set()

        existing_fixture_ids = {
            game.id for game in self.models.Game.select().where(
                (self.models.Game.guild_id == guild_id)
                & (
                    self.models.Game.notes
                    == dev_fixtures.FIXTURE_NOTES_MARKER
                )
            )
        }
        if existing_fixture_ids:
            self.skipTest(
                'preserving an existing operator-managed beta fixture set'
            )

        first_member = self.models.DiscordMember.create(
            discord_id=discord_ids[0],
            name=f'Fixture Integration One {suffix}',
        )
        second_member = self.models.DiscordMember.create(
            discord_id=discord_ids[1],
            name=f'Fixture Integration Two {suffix}',
        )
        self.models.Player.create(
            discord_member=first_member,
            guild_id=guild_id,
            name=f'Fixture Integration One {suffix}',
        )
        self.models.Player.create(
            discord_member=second_member,
            guild_id=guild_id,
            name=f'Fixture Integration Two {suffix}',
        )
        ordinary_game = self.models.Game.create(
            guild_id=guild_id,
            name=f'Ordinary Integration Game {suffix}',
            notes='not owned by the fixture harness',
            size=[1, 1],
        )
        ordinary_game_id = ordinary_game.id
        self.models.db.close()

        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / 'manifest.json'
            try:
                seeded = dev_fixtures.seed_fixtures(
                    profile=self.profile,
                    models_module=self.models,
                    guild_id=guild_id,
                    user_ids=discord_ids,
                    manifest_path=manifest_path,
                )
                self.assertEqual(
                    {game.scenario for game in seeded.games},
                    {'ready', 'unconfirmed', 'completed'},
                )
                seeded_by_scenario = {
                    game.scenario: game for game in seeded.games
                }
                self.assertIsNone(
                    seeded_by_scenario['ready'].league_season
                )
                self.assertEqual(
                    seeded_by_scenario['completed'].league_season,
                    dev_fixtures.FIXTURE_COMPLETED_LEAGUE_SEASON,
                )
                self.assertEqual(
                    seeded_by_scenario['unconfirmed'].league_season,
                    dev_fixtures.FIXTURE_CURRENT_LEAGUE_SEASON,
                )
                self.assertEqual(
                    seeded_by_scenario['completed'].league_tier,
                    dev_fixtures.FIXTURE_LEAGUE_TIER,
                )
                self.assertTrue(manifest_path.is_file())
                seeded_ids = {game.game_id for game in seeded.games}
                created_fixture_ids.update(seeded_ids)

                repeated = dev_fixtures.seed_fixtures(
                    profile=self.profile,
                    models_module=self.models,
                    guild_id=guild_id,
                    user_ids=discord_ids,
                    manifest_path=manifest_path,
                )
                self.assertEqual(
                    {game.game_id for game in repeated.games},
                    seeded_ids,
                )

                status = dev_fixtures.fixture_status(
                    profile=self.profile,
                    models_module=self.models,
                    guild_id=guild_id,
                )
                self.assertEqual(
                    {game.game_id for game in status.games},
                    seeded_ids,
                )

                remaining = dev_fixtures.cleanup_fixtures(
                    profile=self.profile,
                    models_module=self.models,
                    guild_id=guild_id,
                    manifest_path=manifest_path,
                    confirmed=True,
                )
                self.assertEqual(remaining.games, ())
                self.assertFalse(manifest_path.exists())

                self.models.db.connect(reuse_if_open=True)
                self.assertTrue(
                    self.models.Game.select().where(
                        self.models.Game.id == ordinary_game_id
                    ).exists()
                )
            finally:
                self.models.db.connect(reuse_if_open=True)
                fixture_games = list(self.models.Game.select().where(
                    self.models.Game.id.in_(created_fixture_ids)
                ))
                for game in sorted(
                    fixture_games,
                    key=lambda item: (
                        item.completed_ts or datetime.datetime.min
                    ),
                    reverse=True,
                ):
                    game.delete_game()
                if ordinary_game_id is not None:
                    self.models.Game.delete().where(
                        self.models.Game.id == ordinary_game_id
                    ).execute()
                player_ids = [
                    player.id for player in self.models.Player.select().join(
                        self.models.DiscordMember
                    ).where(
                        self.models.DiscordMember.discord_id.in_(discord_ids)
                    )
                ]
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids)
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.discord_id.in_(discord_ids)
                ).execute()

    def test_newgame_worker_creates_complete_graph_and_rolls_back(self):
        from modules import game_workers

        marker = f'P2.1 War integration {uuid.uuid4().hex}'
        id_base = 800_000_000_000_000_000
        host_id = id_base + (uuid.uuid4().int % 10_000_000)
        opponent_id = id_base + (uuid.uuid4().int % 10_000_000)
        third_side_id = id_base + (uuid.uuid4().int % 10_000_000)
        request = game_workers.NewGameRequest(
            guild_id=self.profile.allowed_guild_ids[0],
            name=marker,
            is_ranked=False,
            is_mobile=True,
            mod_override=False,
            requester_is_staff=False,
            requester_id=host_id,
            requester_name='p21-host',
            requester_nick=None,
            requester_description=f'**p21-host** (`{host_id}`)',
            invoked_with='newgameunranked',
            escaped_game_name=marker,
            sides=(
                (
                    game_workers.NewGameParticipant(
                        discord_id=host_id,
                        discord_name='p21-host',
                        discord_nick=None,
                        display_name='P2.1 Host',
                        role_names=(),
                    ),
                ),
                (
                    game_workers.NewGameParticipant(
                        discord_id=opponent_id,
                        discord_name='p21-opponent',
                        discord_nick=None,
                        display_name='P2.1 Opponent',
                        role_names=(),
                    ),
                ),
                (
                    game_workers.NewGameParticipant(
                        discord_id=third_side_id,
                        discord_name='p23-third-side',
                        discord_nick=None,
                        display_name='P2.3 Third Side',
                        role_names=(),
                    ),
                ),
            ),
        )

        with self.rollback_scope():
            # The gated test already owns the connection/outer rollback.
            # Offline tests independently prove worker connection ownership.
            with mock.patch.object(
                self.models.db,
                'connection_context',
                return_value=nullcontext(),
            ):
                result = game_workers.create_new_game(request)

            game = self.models.Game.get_by_id(result.game_id)
            self.assertEqual(game.name, marker.title()[:35])
            self.assertEqual(game.host.discord_member.discord_id, host_id)
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game == game
                ).count(),
                3,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game
                ).count(),
                3,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

        self.assertEqual(
            self.models.Game.get_or_none(
                self.models.Game.id == result.game_id
            ),
            None,
        )
        self.assertEqual(
            self.models.DiscordMember.select().where(
                self.models.DiscordMember.discord_id.in_(
                    (host_id, opponent_id, third_side_id)
                )
            ).count(),
            0,
        )
        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count(),
            0,
        )

    def test_newgame_executor_failure_rolls_back_worker_connection(self):
        from modules import exceptions, game_workers

        marker = f'P2.1 War worker rollback {uuid.uuid4().hex}'
        id_base = 810_000_000_000_000_000
        participant_one_id = id_base + (uuid.uuid4().int % 10_000_000)
        participant_two_id = id_base + (uuid.uuid4().int % 10_000_000)
        missing_requester_id = id_base + (uuid.uuid4().int % 10_000_000)
        request = game_workers.NewGameRequest(
            guild_id=self.profile.allowed_guild_ids[0],
            name=marker,
            is_ranked=False,
            is_mobile=True,
            mod_override=False,
            requester_is_staff=False,
            requester_id=missing_requester_id,
            requester_name='missing-host',
            requester_nick=None,
            requester_description=(
                f'**missing-host** (`{missing_requester_id}`)'
            ),
            invoked_with='newgameunranked',
            escaped_game_name=marker,
            sides=(
                (
                    game_workers.NewGameParticipant(
                        discord_id=participant_one_id,
                        discord_name='p21-participant-one',
                        discord_nick=None,
                        display_name='P2.1 Participant One',
                        role_names=(),
                    ),
                ),
                (
                    game_workers.NewGameParticipant(
                        discord_id=participant_two_id,
                        discord_name='p21-participant-two',
                        discord_nick=None,
                        display_name='P2.1 Participant Two',
                        role_names=(),
                    ),
                ),
            ),
        )

        async def run_worker():
            task = asyncio.create_task(
                game_workers.run_new_game_creation(request)
            )
            while not task.done():
                await asyncio.sleep(0.05)
            return await task

        with self.assertRaisesRegex(
            exceptions.CheckFailedError,
            'registered game host',
        ):
            asyncio.run(run_worker())

        self.models.db.connect(reuse_if_open=True)
        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.name == marker.title()[:35]
            ).count(),
            0,
        )
        self.assertEqual(
            self.models.DiscordMember.select().where(
                self.models.DiscordMember.discord_id.in_(
                    (participant_one_id, participant_two_id)
                )
            ).count(),
            0,
        )
        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count(),
            0,
        )

    def test_open_game_worker_commits_and_rolls_back_complete_graph(self):
        from modules import game_open_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        discord_id = 8_600_000_000_000_000 + uuid.uuid4().int % 1_000_000
        member_name = f'P51 Integration Host {suffix}'
        marker = f'P5.1 integration {suffix}'
        team = None
        created_game_ids = set()

        try:
            if self.settings.guild_setting(guild_id, 'require_teams'):
                team = self.models.Team.create(
                    name=f'P51 Integration Team {suffix}',
                    guild_id=guild_id,
                )
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=member_name,
                polytopia_name=f'P51Mobile{suffix}',
            )
            host = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member_name,
                team=team,
            )
            request = game_open_workers.OpenGameRequest(
                guild_id=guild_id,
                requester_id=discord_id,
                requester_name=member_name,
                requester_nick=None,
                prefix='$',
                requester_role_ids=(),
                requester_role_names=(team.name,) if team else (),
                requester_level=3,
                requester_is_mod=False,
                requester_is_staff=False,
                sides=(
                    game_open_workers.OpenGameSide(1),
                    game_open_workers.OpenGameSide(1),
                ),
                expiration_hours=24,
                is_ranked=True,
                is_mobile=True,
                notes=marker,
                notes_display=marker,
                requester_description=(
                    f'**{member_name}** (`{discord_id}`)'
                ),
                invoked_with='opengame',
            )

            result = asyncio.run(
                game_open_workers.run_open_game_creation(request)
            )
            created_game_ids.add(result.game_id)
            self.assertTrue(result.is_mobile)
            game = self.models.Game.get_by_id(result.game_id)
            self.assertEqual(game.host.id, host.id)
            self.assertTrue(game.is_mobile)
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game == game
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

            failing_log = mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=RuntimeError('P5.1 audit failure'),
            )
            with failing_log, self.assertRaises(RuntimeError):
                asyncio.run(
                    game_open_workers.run_open_game_creation(request)
                )
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.notes == marker
                ).count(),
                1,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            for game_id in sorted(created_game_ids):
                game = self.models.Game.get_or_none(
                    self.models.Game.id == game_id
                )
                if game is not None:
                    game.delete_game()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(marker)
            ).execute()
            self.models.Player.delete().where(
                self.models.Player.discord_member == discord_id
            ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id == discord_id
            ).execute()
            if team is not None:
                self.models.Team.delete().where(
                    self.models.Team.id == team.id
                ).execute()

    def test_join_leave_worker_real_graph_and_audit_rollback(self):
        from modules import game_join_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        marker = f'P5.2 integration {suffix}'
        id_base = 8_900_000_000_000_000 + uuid.uuid4().int % 1_000_000
        host_discord_id = id_base
        joiner_discord_id = id_base + 1
        team = None
        game_id = None
        created_log_ids = set()

        try:
            if self.settings.guild_setting(guild_id, 'require_teams'):
                team = self.models.Team.create(
                    name=f'P52 Integration Team {suffix}',
                    guild_id=guild_id,
                )

            host_member = self.models.DiscordMember.create(
                discord_id=host_discord_id,
                name=f'P52 Host {suffix}',
                polytopia_name=f'P52Host{suffix}',
            )
            joiner_member = self.models.DiscordMember.create(
                discord_id=joiner_discord_id,
                name=f'P52 Joiner {suffix}',
                polytopia_name=f'P52Joiner{suffix}',
            )
            host_player = self.models.Player.create(
                discord_member=host_member,
                guild_id=guild_id,
                name=host_member.name,
                team=team,
            )
            joiner_player = self.models.Player.create(
                discord_member=joiner_member,
                guild_id=guild_id,
                name=joiner_member.name,
                team=team,
            )
            game = self.models.Game.create(
                guild_id=guild_id,
                host=host_player,
                expiration=datetime.datetime.now() + datetime.timedelta(hours=24),
                notes=marker,
                is_pending=True,
                is_ranked=True,
                is_mobile=True,
                size=[1, 1],
            )
            game_id = game.id
            first_side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
            )
            second_side = self.models.GameSide.create(
                game=game,
                position=2,
                sidename='Bravo',
                size=1,
            )
            self.models.Lineup.create(
                game=game,
                gameside=first_side,
                player=host_player,
            )

            role_ids = (9_999,) if team else ()
            role_names = (team.name,) if team else ()
            joiner_snapshot = game_join_workers.MemberSnapshot(
                guild_id=guild_id,
                discord_id=joiner_discord_id,
                discord_name=joiner_member.name,
                discord_nick=None,
                display_name=joiner_member.name,
                role_ids=role_ids,
                role_names=role_names,
                level=3,
                is_mod=False,
                is_staff=False,
                description=(
                    f'**{joiner_member.name}** (`{joiner_discord_id}`)'
                ),
            )
            join_request = game_join_workers.JoinRequest(
                game_id=game_id,
                guild_id=guild_id,
                prefix='$',
                member=joiner_snapshot,
                author=joiner_snapshot,
                side_arg='2',
                invoked_with='join',
                notification_member_id=joiner_discord_id,
            )

            # The legacy prefix grammar resolves a possible named side once
            # on the same bounded worker infrastructure before the join
            # transaction authoritatively resolves it again.
            self.models.db.close()
            side_snapshot = asyncio.run(
                game_join_workers.run_prefix_side_token_lookup(
                    game_join_workers.PrefixSideTokenRequest(
                        game_id=game_id,
                        guild_id=guild_id,
                        token='Bravo',
                    )
                )
            )
            self.assertTrue(side_snapshot.matches_side)
            self.models.db.connect(reuse_if_open=True)

            # Close the main-thread connection so run_join must establish and
            # close its own Peewee connection in the executor worker.
            self.models.db.close()
            join_result = asyncio.run(
                game_join_workers.run_join(join_request)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(join_result.game_id, game_id)
            self.assertEqual(
                self.models.Player.get_by_id(joiner_player.id).id,
                joiner_player.id,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game_id
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    (self.models.Lineup.game == game_id)
                    & (self.models.Lineup.gameside == second_side.id)
                    & (self.models.Lineup.player == joiner_player.id)
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game == game_id
                ).count(),
                2,
            )
            created_log_ids.update(
                row.id for row in self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                )
            )
            self.assertEqual(len(created_log_ids), 1)

            leave_request = game_join_workers.LeaveRequest(
                game_id=game_id,
                guild_id=guild_id,
                prefix='$',
                member=joiner_snapshot,
                author=joiner_snapshot,
                invoked_with='leave',
            )
            self.models.db.close()
            leave_result = asyncio.run(
                game_join_workers.run_leave(leave_request)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(leave_result.game_id, game_id)
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game_id
                ).count(),
                1,
            )
            created_log_ids.update(
                row.id for row in self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                )
            )
            self.assertEqual(len(created_log_ids), 2)

            failing_log = mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=RuntimeError('P5.2 audit failure'),
            )
            self.models.db.close()
            with failing_log, self.assertRaisesRegex(
                RuntimeError,
                'P5.2 audit failure',
            ):
                asyncio.run(game_join_workers.run_join(join_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game_id
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                2,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_id is not None:
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            if created_log_ids:
                self.models.GameLog.delete().where(
                    self.models.GameLog.id.in_(created_log_ids)
                ).execute()
            temporary_member_ids = self.models.DiscordMember.select(
                self.models.DiscordMember.id
            ).where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, joiner_discord_id)
                )
            )
            self.models.Player.delete().where(
                self.models.Player.discord_member.in_(temporary_member_ids)
            ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, joiner_discord_id)
                )
            ).execute()
            if team is not None:
                self.models.Team.delete().where(
                    self.models.Team.id == team.id
                ).execute()

            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.id == game_id
                ).count() if game_id is not None else 0,
                0,
            )
            self.assertEqual(
                self.models.DiscordMember.select().where(
                    self.models.DiscordMember.discord_id.in_(
                        (host_discord_id, joiner_discord_id)
                    )
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                0,
            )

    def test_kick_worker_real_graph_and_audit_rollback(self):
        from modules import game_join_workers, game_kick_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        id_base = 8_900_000_000_000_000 + uuid.uuid4().int % 1_000_000
        host_discord_id = id_base
        target_discord_id = id_base + 1
        game_id = None

        try:
            host_member = self.models.DiscordMember.create(
                discord_id=host_discord_id,
                name=f'P53 Host {suffix}',
                polytopia_name=f'P53Host{suffix}',
            )
            target_member = self.models.DiscordMember.create(
                discord_id=target_discord_id,
                name=f'P53 Target {suffix}',
                polytopia_name=f'P53Target{suffix}',
            )
            host_player = self.models.Player.create(
                discord_member=host_member,
                guild_id=guild_id,
                name=host_member.name,
            )
            target_player = self.models.Player.create(
                discord_member=target_member,
                guild_id=guild_id,
                name=target_member.name,
            )
            original_expiration = (
                datetime.datetime.now() + datetime.timedelta(hours=1)
            )
            game = self.models.Game.create(
                guild_id=guild_id,
                host=host_player,
                expiration=original_expiration,
                notes=f'P5.3 integration {suffix}',
                is_pending=True,
                is_ranked=True,
                is_mobile=True,
                size=[1, 1],
            )
            game_id = game.id
            first_side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
            )
            second_side = self.models.GameSide.create(
                game=game,
                position=2,
                sidename='Bravo',
                size=1,
            )
            self.models.Lineup.create(
                game=game,
                gameside=first_side,
                player=host_player,
            )
            self.models.Lineup.create(
                game=game,
                gameside=second_side,
                player=target_player,
            )

            host_snapshot = game_join_workers.MemberSnapshot(
                guild_id=guild_id,
                discord_id=host_discord_id,
                discord_name=host_member.name,
                discord_nick=None,
                display_name=host_member.name,
                role_ids=(),
                role_names=(),
                level=3,
                is_mod=False,
                is_staff=False,
                description=f'**{host_member.name}** (`{host_discord_id}`)',
            )
            target_snapshot = game_join_workers.MemberSnapshot(
                guild_id=guild_id,
                discord_id=target_discord_id,
                discord_name=target_member.name,
                discord_nick=None,
                display_name=target_member.name,
                role_ids=(),
                role_names=(),
                level=3,
                is_mod=False,
                is_staff=False,
                description=(
                    f'**{target_member.name}** (`{target_discord_id}`)'
                ),
            )
            request = game_kick_workers.KickRequest(
                game_id=game_id,
                guild_id=guild_id,
                prefix='$',
                author=host_snapshot,
                target=target_snapshot,
                invoked_with='kick',
            )

            # The caller's connection is deliberately closed.  The kick must
            # open and close its own connection in the pending-game worker.
            self.models.db.close()
            result = asyncio.run(game_kick_workers.run_kick(request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(result.game_id, game_id)
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game_id
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    (self.models.Lineup.game == game_id)
                    & (self.models.Lineup.player == target_player.id)
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                1,
            )
            committed_expiration = self.models.Game.get_by_id(
                game_id
            ).expiration
            self.assertGreater(
                committed_expiration,
                datetime.datetime.now() + datetime.timedelta(hours=23),
            )
            self.assertLess(
                committed_expiration,
                datetime.datetime.now() + datetime.timedelta(hours=25),
            )

            # Re-add the target and inject the audit failure.  The lineup,
            # log count, and already-committed expiration must all survive the
            # worker transaction rollback.
            self.models.Lineup.create(
                game=game_id,
                gameside=second_side.id,
                player=target_player.id,
            )
            expiration_before_failure = self.models.Game.get_by_id(
                game_id
            ).expiration
            log_count_before_failure = self.models.GameLog.select().where(
                self.models.GameLog.message.contains(suffix)
            ).count()
            failing_log = mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=RuntimeError('P5.3 audit failure'),
            )
            self.models.db.close()
            with failing_log, self.assertRaisesRegex(
                RuntimeError,
                'P5.3 audit failure',
            ):
                asyncio.run(game_kick_workers.run_kick(request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game_id
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                log_count_before_failure,
            )
            self.assertEqual(
                self.models.Game.get_by_id(game_id).expiration,
                expiration_before_failure,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_id is not None:
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            temporary_member_ids = self.models.DiscordMember.select(
                self.models.DiscordMember.id
            ).where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, target_discord_id)
                )
            )
            self.models.Player.delete().where(
                self.models.Player.discord_member.in_(temporary_member_ids)
            ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, target_discord_id)
                )
            ).execute()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(suffix)
            ).execute()
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.id == game_id
                ).count() if game_id is not None else 0,
                0,
            )
            self.assertEqual(
                self.models.DiscordMember.select().where(
                    self.models.DiscordMember.discord_id.in_(
                        (host_discord_id, target_discord_id)
                    )
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                0,
            )

    def test_start_worker_real_graph_and_audit_rollback(self):
        from modules import (
            game_broadcast_workers,
            game_start_channel_workers,
            game_start_workers,
        )

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        id_base = 8_950_000_000_000_000 + uuid.uuid4().int % 1_000_000
        host_discord_id = id_base
        target_discord_id = id_base + 1
        game_ids = set()
        team_ids = set()

        try:
            host_member = self.models.DiscordMember.create(
                discord_id=host_discord_id,
                name=f'P54 Host {suffix}',
                polytopia_name=f'P54Host{suffix}',
            )
            target_member = self.models.DiscordMember.create(
                discord_id=target_discord_id,
                name=f'P54 Target {suffix}',
                polytopia_name=f'P54Target{suffix}',
            )
            host_player = self.models.Player.create(
                discord_member=host_member,
                guild_id=guild_id,
                name=host_member.name,
            )
            target_player = self.models.Player.create(
                discord_member=target_member,
                guild_id=guild_id,
                name=target_member.name,
            )
            team_one = self.models.Team.create(
                name=f'P54 Team One {suffix}',
                guild_id=guild_id,
            )
            team_two = self.models.Team.create(
                name=f'P54 Team Two {suffix}',
                guild_id=guild_id,
            )
            team_ids.update((team_one.id, team_two.id))

            def make_pending_game(note):
                game = self.models.Game.create(
                    guild_id=guild_id,
                    host=host_player,
                    expiration=(
                        datetime.datetime.now() + datetime.timedelta(days=1)
                    ),
                    notes=note,
                    is_pending=True,
                    is_ranked=True,
                    is_mobile=True,
                    size=[1, 1],
                )
                first_side = self.models.GameSide.create(
                    game=game,
                    position=1,
                    sidename='Alpha',
                    size=1,
                )
                second_side = self.models.GameSide.create(
                    game=game,
                    position=2,
                    sidename='Bravo',
                    size=1,
                )
                self.models.Lineup.create(
                    game=game,
                    gameside=first_side,
                    player=host_player,
                )
                self.models.Lineup.create(
                    game=game,
                    gameside=second_side,
                    player=target_player,
                )
                game_ids.add(game.id)
                return game

            host_snapshot = game_start_workers.StartMemberSnapshot(
                guild_id=guild_id,
                discord_id=host_discord_id,
                discord_name=host_member.name,
                discord_nick=None,
                display_name=host_member.name,
                role_ids=(),
                role_names=(team_one.name,),
                level=3,
                is_mod=False,
                is_staff=False,
                description=(
                    f'**{host_member.name}** ({host_discord_id})'
                ),
                side_position=0,
                lineup_id=None,
                player_id=None,
                player_name=host_member.name,
            )
            target_snapshot = game_start_workers.StartMemberSnapshot(
                guild_id=guild_id,
                discord_id=target_discord_id,
                discord_name=target_member.name,
                discord_nick=None,
                display_name=target_member.name,
                role_ids=(),
                role_names=(team_two.name,),
                level=3,
                is_mod=False,
                is_staff=False,
                description=(
                    f'**{target_member.name}** ({target_discord_id})'
                ),
                side_position=2,
                lineup_id=None,
                player_id=None,
                player_name=target_player.name,
            )

            first_game = make_pending_game(f'P54 first {suffix}')
            broadcast = self.models.TeamServerBroadcastMessage.create(
                game=first_game,
                channel_id=id_base + 10,
                message_id=id_base + 11,
            )
            first_preflight = game_start_workers.preflight_start_game(
                game_start_workers.StartPreflightRequest(
                    game_id=first_game.id,
                    guild_id=guild_id,
                    name=f'Fields of Fire {suffix}',
                    prefix='$',
                    requester=host_snapshot,
                    require_teams=False,
                    invoked_with='start',
                )
            )
            first_request = game_start_workers.StartRequest(
                game_id=first_game.id,
                guild_id=guild_id,
                name=f'Fields of Fire {suffix}',
                prefix='$',
                requester=host_snapshot,
                participants=(
                    game_start_workers.StartMemberSnapshot(
                        **{
                            **host_snapshot.__dict__,
                            'side_position': first_preflight.participants[0].side_position,
                            'lineup_id': first_preflight.participants[0].lineup_id,
                            'player_id': first_preflight.participants[0].player_id,
                            'player_name': first_preflight.participants[0].player_name,
                        }
                    ),
                    game_start_workers.StartMemberSnapshot(
                        **{
                            **target_snapshot.__dict__,
                            'side_position': first_preflight.participants[1].side_position,
                            'lineup_id': first_preflight.participants[1].lineup_id,
                            'player_id': first_preflight.participants[1].player_id,
                            'player_name': first_preflight.participants[1].player_name,
                        }
                    ),
                ),
                preflight=first_preflight,
                require_teams=False,
                invoked_with='start',
            )

            self.models.db.close()
            first_result = asyncio.run(
                game_start_workers.run_start(first_request)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(first_result.game_id, first_game.id)
            self.assertEqual(
                first_result.broadcast_targets,
                (
                    game_broadcast_workers.ExternalBroadcastTarget(
                        row_id=broadcast.id,
                        game_id=first_game.id,
                        guild_id=guild_id,
                        channel_id=broadcast.channel_id,
                        message_id=broadcast.message_id,
                    ),
                ),
            )
            self.assertIsNotNone(first_result.channel_plan)
            self.assertEqual(first_result.channel_plan.game.id, first_game.id)
            self.assertEqual(first_result.channel_plan.side_targets, ())
            self.assertTrue(first_result.is_ranked)
            self.assertEqual(first_result.side_sizes, (1, 1))
            self.assertIsNone(first_result.league_season)
            committed_first_side = self.models.GameSide.get(
                (self.models.GameSide.game == first_game.id)
                & (self.models.GameSide.position == 1)
            )
            self.assertEqual(
                first_result.first_side_team_hidden,
                bool(committed_first_side.team.is_hidden),
            )
            self.assertFalse(first_result.uncaught_season_game)
            self.assertIn(
                f'Side **{host_player.name[:30]}**',
                first_result.channel_plan.roster_names,
            )

            first_side = self.models.GameSide.get(
                (self.models.GameSide.game == first_game.id)
                & (self.models.GameSide.position == 1)
            )
            channel_id = id_base + 20
            channel_guild_id = guild_id + 123
            self.models.db.close()
            persisted_channel = asyncio.run(
                game_start_channel_workers.run_persist_started_channel(
                    game_start_channel_workers.PersistStartedChannelRequest(
                        game_id=int(first_game.id),
                        guild_id=int(guild_id),
                        channel_id=int(channel_id),
                        channel_guild_id=int(channel_guild_id),
                        kind='side',
                        side_id=int(first_side.id),
                    )
                )
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertFalse(persisted_channel.already_persisted)
            persisted_side = self.models.GameSide.get_by_id(first_side.id)
            self.assertEqual(int(persisted_side.team_chan), channel_id)
            self.assertEqual(
                int(persisted_side.team_chan_external_server),
                channel_guild_id,
            )
            self.models.db.close()
            repeated_channel = asyncio.run(
                game_start_channel_workers.run_persist_started_channel(
                    game_start_channel_workers.PersistStartedChannelRequest(
                        game_id=int(first_game.id),
                        guild_id=int(guild_id),
                        channel_id=int(channel_id),
                        channel_guild_id=int(channel_guild_id),
                        kind='side',
                        side_id=int(first_side.id),
                    )
                )
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(repeated_channel.already_persisted)
            self.models.db.close()
            with self.assertRaises(
                game_start_channel_workers.StartedChannelConflictError
            ):
                asyncio.run(
                    game_start_channel_workers.run_persist_started_channel(
                        game_start_channel_workers.PersistStartedChannelRequest(
                            game_id=int(first_game.id),
                            guild_id=int(guild_id),
                            channel_id=int(channel_id + 1),
                            channel_guild_id=int(guild_id),
                            kind='side',
                            side_id=int(first_side.id),
                        )
                    )
                )
            self.models.db.connect(reuse_if_open=True)
            started_game = self.models.Game.get_by_id(first_game.id)
            self.assertFalse(started_game.is_pending)
            self.assertEqual(started_game.name, f'Fields Of Fire {suffix}'.title()[:35])
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(suffix)
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.GameSide.select().where(
                    (self.models.GameSide.game == first_game.id)
                    & self.models.GameSide.team.is_null(False)
                ).count(),
                2,
            )

            self.models.db.close()
            discovered = asyncio.run(
                game_broadcast_workers.run_discover_started_broadcasts(
                    game_broadcast_workers.BroadcastDiscoveryRequest(
                        guild_id=guild_id,
                    )
                )
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertIn(
                first_result.broadcast_targets[0],
                discovered.targets,
            )

            with mock.patch.object(
                self.models.TeamServerBroadcastMessage,
                'delete_instance',
                side_effect=RuntimeError('P5.15 finalization failure'),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    RuntimeError,
                    'P5.15 finalization failure',
                ):
                    asyncio.run(
                        game_broadcast_workers
                        .run_finalize_started_broadcast(
                            first_result.broadcast_targets[0]
                        )
                    )
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNotNone(
                self.models.TeamServerBroadcastMessage.get_or_none(
                    id=broadcast.id
                )
            )

            self.models.db.close()
            finalized = asyncio.run(
                game_broadcast_workers.run_finalize_started_broadcast(
                    first_result.broadcast_targets[0]
                )
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                finalized.status,
                game_broadcast_workers.FINALIZED,
            )
            self.assertIsNone(
                self.models.TeamServerBroadcastMessage.get_or_none(
                    id=broadcast.id
                )
            )

            rollback_game = make_pending_game(f'P54 rollback {suffix}')
            rollback_preflight = game_start_workers.preflight_start_game(
                game_start_workers.StartPreflightRequest(
                    game_id=rollback_game.id,
                    guild_id=guild_id,
                    name=f'Fields of Fire {suffix}',
                    prefix='$',
                    requester=host_snapshot,
                    require_teams=False,
                    invoked_with='start',
                )
            )
            rollback_participants = tuple(
                game_start_workers.StartMemberSnapshot(
                    **{
                        **(
                            host_snapshot
                            if participant.discord_id == host_discord_id
                            else target_snapshot
                        ).__dict__,
                        'side_position': participant.side_position,
                        'lineup_id': participant.lineup_id,
                        'player_id': participant.player_id,
                        'player_name': participant.player_name,
                    }
                )
                for participant in rollback_preflight.participants
            )
            rollback_request = game_start_workers.StartRequest(
                game_id=rollback_game.id,
                guild_id=guild_id,
                name=f'Fields of Fire {suffix}',
                prefix='$',
                requester=host_snapshot,
                participants=rollback_participants,
                preflight=rollback_preflight,
                require_teams=False,
                invoked_with='start',
            )
            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=RuntimeError('P5.4 audit failure'),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(RuntimeError, 'P5.4 audit failure'):
                    asyncio.run(game_start_workers.run_start(rollback_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(
                self.models.Game.get_by_id(rollback_game.id).is_pending
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == rollback_game.id
                ).count(),
                2,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            for game_id in sorted(game_ids):
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            temporary_members = self.models.DiscordMember.select(
                self.models.DiscordMember.id
            ).where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, target_discord_id)
                )
            )
            self.models.Player.delete().where(
                self.models.Player.discord_member.in_(temporary_members)
            ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, target_discord_id)
                )
            ).execute()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(suffix)
            ).execute()
            if team_ids:
                self.models.Team.delete().where(
                    self.models.Team.id.in_(team_ids)
                ).execute()

    def test_nova_graduation_worker_reads_real_schema_without_writes(self):
        from modules import nova_graduation_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        suffix = uuid.uuid4().hex
        discord_id = 9_150_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        game_ids = []
        member_id = None
        try:
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=f'P519B Nova {suffix}',
                polytopia_name=f'P519BNova{suffix}',
                elo_moonrise=1234,
            )
            member_id = int(member.id)
            player = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member.name,
            )

            def make_game(*, completed):
                game = self.models.Game.create(
                    guild_id=guild_id,
                    name=f'P519B {suffix}',
                    notes=f'P519B {suffix}',
                    is_pending=False,
                    is_completed=completed,
                    is_confirmed=completed,
                    is_ranked=True,
                    is_mobile=True,
                    completed_ts=(
                        datetime.datetime.now() if completed else None
                    ),
                    size=[2, 2],
                )
                first = self.models.GameSide.create(
                    game=game,
                    position=1,
                    sidename='Alpha',
                    size=2,
                )
                self.models.GameSide.create(
                    game=game,
                    position=2,
                    sidename='Bravo',
                    size=2,
                )
                self.models.Lineup.create(
                    game=game,
                    gameside=first,
                    player=player,
                )
                if completed:
                    game.winner = first
                    game.save()
                game_ids.append(int(game.id))
                return game

            completed_game = make_game(completed=True)
            incomplete_game = make_game(completed=False)
            config_count = self.models.Configuration.select().where(
                self.models.Configuration.guild_id == guild_id
            ).count()
            log_count = self.models.GameLog.select().count()

            nova_request = workers.NovaGraduationRequest(
                game_id=int(incomplete_game.id),
                guild_id=guild_id,
                allowed_guild_ids=(guild_id,),
                participants=(workers.NovaParticipantSnapshot(
                    discord_id=discord_id,
                    member_name=member.name,
                    mention=f'<@{discord_id}>',
                    has_nova_role=True,
                    has_grad_role=False,
                ),),
            )
            self.models.db.close()
            loaded = asyncio.run(
                workers.run_load_nova_graduation(nova_request)
            )
            self.models.db.connect(reuse_if_open=True)

            self.assertEqual(len(loaded.candidates), 1)
            self.assertEqual(loaded.candidates[0].discord_id, discord_id)
            self.assertEqual(
                loaded.candidates[0].qualifying_game_ids,
                (int(incomplete_game.id), int(completed_game.id)),
            )
            self.assertEqual(loaded.candidates[0].global_elo, 1234)
            self.assertEqual(
                self.models.Configuration.select().where(
                    self.models.Configuration.guild_id == guild_id
                ).count(),
                config_count,
            )
            self.assertEqual(self.models.GameLog.select().count(), log_count)
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_ids:
                self.models.Game.update(winner=None).where(
                    self.models.Game.id.in_(game_ids)
                ).execute()
                self.models.Lineup.delete().where(
                    self.models.Lineup.game.in_(game_ids)
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game.in_(game_ids)
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id.in_(game_ids)
                ).execute()
            if member_id is not None:
                self.models.Player.delete().where(
                    self.models.Player.discord_member == member_id
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id == member_id
                ).execute()
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.id.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )

    def test_pending_delete_worker_commits_and_rolls_back_real_graph_and_audit(self):
        from modules import game_deletion_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        marker = f'P56 pending-delete {suffix}'
        id_base = 9_050_000_000_000_000 + uuid.uuid4().int % 1_000_000
        host_discord_id = id_base
        target_discord_id = id_base + 1
        game_ids = set()

        def make_pending_game(label):
            game = self.models.Game.create(
                guild_id=guild_id,
                host=host_player,
                expiration=(
                    datetime.datetime.now() + datetime.timedelta(days=1)
                ),
                name=label,
                notes=label,
                is_pending=True,
                is_ranked=True,
                is_mobile=True,
                size=[1, 1],
            )
            game_ids.add(game.id)
            first_side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
            )
            second_side = self.models.GameSide.create(
                game=game,
                position=2,
                sidename='Bravo',
                size=1,
            )
            self.models.Lineup.create(
                game=game,
                gameside=first_side,
                player=host_player,
            )
            self.models.Lineup.create(
                game=game,
                gameside=second_side,
                player=target_player,
            )
            return game

        def request_for(game):
            return game_deletion_workers.DeletionRequest(
                game_id=game.id,
                guild_id=guild_id,
                requester_id=host_discord_id,
                requester_name=host_member.name,
                requester_description=f'{marker} host',
                requester_is_staff=False,
                requester_is_mod=False,
                prefix='$',
                invoked_with='delete',
            )

        try:
            host_member = self.models.DiscordMember.create(
                discord_id=host_discord_id,
                name=f'P56 Host {suffix}',
                polytopia_name=f'P56Host{suffix}',
            )
            target_member = self.models.DiscordMember.create(
                discord_id=target_discord_id,
                name=f'P56 Target {suffix}',
                polytopia_name=f'P56Target{suffix}',
            )
            host_player = self.models.Player.create(
                discord_member=host_member,
                guild_id=guild_id,
                name=host_member.name,
            )
            target_player = self.models.Player.create(
                discord_member=target_member,
                guild_id=guild_id,
                name=target_member.name,
            )

            commit_game = make_pending_game(f'{marker} commit')
            self.models.db.close()
            commit_result = asyncio.run(
                game_deletion_workers.run_pending_game_deletion(
                    request_for(commit_game)
                )
            )
            self.models.db.connect(reuse_if_open=True)

            self.assertEqual(commit_result.game_id, commit_game.id)
            self.assertFalse(commit_result.recalculated)
            self.assertIsNone(
                self.models.Game.get_or_none(id=commit_game.id)
            )
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game == commit_game.id
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == commit_game.id
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

            rollback_game = make_pending_game(f'{marker} rollback')
            rollback_lineup_ids = tuple(
                lineup.id
                for lineup in self.models.Lineup.select().where(
                    self.models.Lineup.game == rollback_game.id
                ).order_by(self.models.Lineup.id)
            )
            original_delete_instance = self.models.Lineup.delete_instance
            deleted_lineups = []

            def delete_one_then_fail(lineup, *args, **kwargs):
                deleted_lineups.append(lineup.id)
                result = original_delete_instance(lineup, *args, **kwargs)
                if len(deleted_lineups) == 1:
                    raise peewee.OperationalError(
                        'P5.6 injected fault after partial graph mutation'
                    )
                return result

            with mock.patch.object(
                self.models.Lineup,
                'delete_instance',
                new=delete_one_then_fail,
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'partial graph mutation',
                ):
                    asyncio.run(
                        game_deletion_workers.run_pending_game_deletion(
                            request_for(rollback_game)
                        )
                    )
            self.models.db.connect(reuse_if_open=True)

            self.assertEqual(deleted_lineups, [rollback_lineup_ids[0]])
            self.assertIsNotNone(
                self.models.Game.get_or_none(id=rollback_game.id)
            )
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game == rollback_game.id
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == rollback_game.id
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            for game_id in sorted(game_ids):
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            temporary_member_ids = tuple(
                member_id
                for (member_id,) in self.models.DiscordMember.select(
                    self.models.DiscordMember.id
                ).where(
                    self.models.DiscordMember.discord_id.in_(
                        (host_discord_id, target_discord_id)
                    )
                ).tuples()
            )
            self.models.Player.delete().where(
                self.models.Player.discord_member.in_(temporary_member_ids)
            ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, target_discord_id)
                )
            ).execute()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(marker)
            ).execute()
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.id.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )
            self.assertEqual(
                self.models.DiscordMember.select().where(
                    self.models.DiscordMember.discord_id.in_(
                        (host_discord_id, target_discord_id)
                    )
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.Player.select().where(
                    self.models.Player.discord_member.in_(temporary_member_ids)
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                0,
            )

    def test_external_broadcast_creation_worker_real_schema(self):
        from modules import game_broadcast_creation_workers as workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        discord_id = 8_516_000_000_000_000 + uuid.uuid4().int % 1_000_000
        external_server_id = discord_id + 1
        channel_id = discord_id + 2
        message_id = discord_id + 3
        game_id = None
        team_id = None

        try:
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=f'P516 Host {suffix}',
                polytopia_name=f'P516Host{suffix}',
            )
            player = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member.name,
            )
            team = self.models.Team.create(
                name=f'P516 Team {suffix}',
                guild_id=guild_id,
                external_server=external_server_id,
            )
            team_id = team.id
            game = self.models.Game.create(
                guild_id=guild_id,
                host=player,
                expiration=(
                    datetime.datetime.now() + datetime.timedelta(days=1)
                ),
                notes=f'P516 {suffix}',
                is_pending=True,
                is_ranked=True,
                is_mobile=True,
                size=[1, 1],
            )
            game_id = game.id
            self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
                required_role_id=discord_id + 10,
            )
            self.models.GameSide.create(
                game=game,
                position=2,
                sidename='Bravo',
                size=1,
            )

            self.models.db.close()
            plan_result = asyncio.run(workers.run_build_broadcast_plan(
                workers.BroadcastPlanRequest(
                    game_id=game_id,
                    guild_id=guild_id,
                    jump_url='https://discord.test/p516',
                    role_locks=(workers.BroadcastRoleSnapshot(
                        discord_id + 10,
                        team.name,
                    ),),
                )
            ))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(plan_result.status, workers.READY)
            self.assertEqual(len(plan_result.destinations), 1)
            self.assertEqual(
                plan_result.destinations[0].external_server_id,
                external_server_id,
            )

            target = workers.BroadcastTargetRequest(
                game_id=game_id,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
            )
            self.models.db.close()
            preflight = asyncio.run(
                workers.run_preflight_broadcast_target(target)
            )
            persisted = asyncio.run(
                workers.run_persist_broadcast_target(target)
            )
            duplicate = asyncio.run(
                workers.run_preflight_broadcast_target(target)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(preflight.status, workers.READY)
            self.assertEqual(persisted.status, workers.TRACKED)
            self.assertEqual(duplicate.status, workers.DUPLICATE)
            self.assertEqual(
                self.models.TeamServerBroadcastMessage.select().where(
                    (self.models.TeamServerBroadcastMessage.game == game_id)
                    & (
                        self.models.TeamServerBroadcastMessage.channel_id
                        == channel_id
                    )
                    & (
                        self.models.TeamServerBroadcastMessage.message_id
                        == message_id
                    )
                ).count(),
                1,
            )

            game = self.models.Game.get_by_id(game_id)
            game.is_pending = False
            game.save()
            stale_target = workers.BroadcastTargetRequest(
                game_id=game_id,
                guild_id=guild_id,
                channel_id=channel_id + 1,
                message_id=message_id + 1,
            )
            self.models.db.close()
            stale = asyncio.run(
                workers.run_persist_broadcast_target(stale_target)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(stale.status, workers.STALE)
            self.assertEqual(
                self.models.TeamServerBroadcastMessage.select().where(
                    self.models.TeamServerBroadcastMessage.channel_id
                    == stale_target.channel_id
                ).count(),
                0,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_id is not None:
                self.models.TeamServerBroadcastMessage.delete().where(
                    self.models.TeamServerBroadcastMessage.game == game_id
                ).execute()
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            if team_id is not None:
                self.models.Team.delete().where(
                    self.models.Team.id == team_id
                ).execute()
            player_ids = tuple(
                row[0]
                for row in self.models.Player.select(
                    self.models.Player.id
                ).join(self.models.DiscordMember).where(
                    self.models.DiscordMember.discord_id == discord_id
                ).tuples()
            )
            if player_ids:
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids)
                ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id == discord_id
            ).execute()

    def test_expired_game_purge_discovers_commits_and_rolls_back_real_graph(self):
        from modules import game_expiration_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        marker = f'P510 expired-purge {suffix}'
        id_base = 9_510_000_000_000_000 + uuid.uuid4().int % 1_000_000
        host_discord_id = id_base
        target_discord_id = id_base + 1
        now = datetime.datetime.now()
        game_ids = set()

        def make_game(label, *, expiration, full):
            game = self.models.Game.create(
                guild_id=guild_id,
                host=host_player,
                expiration=expiration,
                name=label,
                notes=label,
                is_pending=True,
                is_ranked=True,
                is_mobile=True,
                size=[1, 1],
            )
            game_ids.add(game.id)
            first_side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
            )
            second_side = self.models.GameSide.create(
                game=game,
                position=2,
                sidename='Bravo',
                size=1,
            )
            self.models.Lineup.create(
                game=game,
                gameside=first_side,
                player=host_player,
            )
            if full:
                self.models.Lineup.create(
                    game=game,
                    gameside=second_side,
                    player=target_player,
                )
            return game

        try:
            host_member = self.models.DiscordMember.create(
                discord_id=host_discord_id,
                name=f'P510 Host {suffix}',
                polytopia_name=f'P510Host{suffix}',
            )
            target_member = self.models.DiscordMember.create(
                discord_id=target_discord_id,
                name=f'P510 Target {suffix}',
                polytopia_name=f'P510Target{suffix}',
            )
            host_player = self.models.Player.create(
                discord_member=host_member,
                guild_id=guild_id,
                name=host_member.name,
            )
            target_player = self.models.Player.create(
                discord_member=target_member,
                guild_id=guild_id,
                name=target_member.name,
            )
            open_game = make_game(
                f'{marker} open',
                expiration=now - datetime.timedelta(hours=1),
                full=False,
            )
            grace_game = make_game(
                f'{marker} grace',
                expiration=now - datetime.timedelta(days=2),
                full=True,
            )
            rollback_game = make_game(
                f'{marker} rollback',
                expiration=now - datetime.timedelta(days=4),
                full=True,
            )
            broadcast = self.models.TeamServerBroadcastMessage.create(
                game=open_game,
                channel_id=9_510_001,
                message_id=9_510_002,
            )

            self.models.db.close()
            discovered = asyncio.run(
                game_expiration_workers.run_discover_expired_game_ids(
                    game_expiration_workers.ExpiredGameDiscoveryRequest(
                        guild_id=guild_id,
                        as_of=now,
                    )
                )
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertIn(open_game.id, discovered.game_ids)
            self.assertIn(rollback_game.id, discovered.game_ids)
            self.assertNotIn(grace_game.id, discovered.game_ids)

            self.models.db.close()
            committed = asyncio.run(
                game_expiration_workers.run_purge_expired_game(
                    game_expiration_workers.ExpiredGamePurgeRequest(
                        game_id=open_game.id,
                        guild_id=guild_id,
                        as_of=now,
                        announcement_channel_id=9_510_003,
                    )
                )
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(committed.status, game_expiration_workers.PURGED)
            self.assertEqual(
                committed.effect_plan.broadcast_targets,
                (
                    game_expiration_workers.game_deletion_workers
                    .DeletionBroadcastTarget(
                        broadcast.channel_id,
                        broadcast.message_id,
                    ),
                ),
            )
            self.assertIsNone(self.models.Game.get_or_none(id=open_game.id))
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(str(open_game.id))
                    & self.models.GameLog.message.contains(
                        'Reconciliation targets:'
                    )
                    & (self.models.GameLog.is_protected == True)
                ).count(),
                1,
            )

            original_delete_instance = self.models.Lineup.delete_instance
            deleted_lineups = []

            def delete_one_then_fail(lineup, *args, **kwargs):
                deleted_lineups.append(lineup.id)
                result = original_delete_instance(lineup, *args, **kwargs)
                if len(deleted_lineups) == 1:
                    raise peewee.OperationalError(
                        'P5.10 injected purge rollback'
                    )
                return result

            with mock.patch.object(
                self.models.Lineup,
                'delete_instance',
                new=delete_one_then_fail,
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'purge rollback',
                ):
                    asyncio.run(
                        game_expiration_workers.run_purge_expired_game(
                            game_expiration_workers.ExpiredGamePurgeRequest(
                                game_id=rollback_game.id,
                                guild_id=guild_id,
                                as_of=now,
                                announcement_channel_id=9_510_003,
                            )
                        )
                    )
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(deleted_lineups)
            self.assertIsNotNone(
                self.models.Game.get_or_none(id=rollback_game.id)
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == rollback_game.id
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(str(rollback_game.id))
                    & self.models.GameLog.message.contains(
                        'Reconciliation targets:'
                    )
                ).count(),
                0,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            for game_id in sorted(game_ids):
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            temporary_member_ids = tuple(
                member_id
                for (member_id,) in self.models.DiscordMember.select(
                    self.models.DiscordMember.id
                ).where(
                    self.models.DiscordMember.discord_id.in_(
                        (host_discord_id, target_discord_id)
                    )
                ).tuples()
            )
            self.models.Player.delete().where(
                self.models.Player.discord_member.in_(temporary_member_ids)
            ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(
                    (host_discord_id, target_discord_id)
                )
            ).execute()
            for game_id in sorted(game_ids):
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.startswith(f'__{game_id}__')
                ).execute()
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.id.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )
            self.assertEqual(
                self.models.DiscordMember.select().where(
                    self.models.DiscordMember.discord_id.in_(
                        (host_discord_id, target_discord_id)
                    )
                ).count(),
                0,
            )
            for game_id in sorted(game_ids):
                self.assertEqual(
                    self.models.GameLog.select().where(
                        self.models.GameLog.message.startswith(
                            f'__{game_id}__'
                        )
                    ).count(),
                    0,
                )

    def test_old_incomplete_purge_warns_commits_and_rolls_back_real_graph(self):
        from modules import incomplete_game_purge_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        marker = f'P514 incomplete-purge {suffix}'
        id_base = 9_514_000_000_000_000 + uuid.uuid4().int % 1_000_000
        today = datetime.date.today()
        game_ids = set()
        purge_runner = asyncio.Runner()

        def make_game(label, *, age_days, pending=False, season=None):
            created = self.models.Game.create(
                guild_id=guild_id,
                host=host_player,
                name=label,
                notes=label,
                date=today - datetime.timedelta(days=age_days),
                is_pending=pending,
                is_completed=False,
                is_confirmed=False,
                is_ranked=True,
                is_mobile=True,
                league_season=season,
                league_tier=1 if season else None,
                size=[1, 1],
                game_chan=id_base + 100 + len(game_ids),
            )
            game_ids.add(created.id)
            first_side = self.models.GameSide.create(
                game=created,
                position=1,
                sidename='Alpha',
                size=1,
            )
            second_side = self.models.GameSide.create(
                game=created,
                position=2,
                sidename='Bravo',
                size=1,
            )
            self.models.Lineup.create(
                game=created,
                gameside=first_side,
                player=host_player,
            )
            self.models.Lineup.create(
                game=created,
                gameside=second_side,
                player=target_player,
            )
            return created

        try:
            host_member = self.models.DiscordMember.create(
                discord_id=id_base,
                name=f'P514 Host {suffix}',
                polytopia_name=f'P514Host{suffix}',
            )
            target_member = self.models.DiscordMember.create(
                discord_id=id_base + 1,
                name=f'P514 Target {suffix}',
                polytopia_name=f'P514Target{suffix}',
            )
            host_player = self.models.Player.create(
                discord_member=host_member,
                guild_id=guild_id,
                name=host_member.name,
            )
            target_player = self.models.Player.create(
                discord_member=target_member,
                guild_id=guild_id,
                name=target_member.name,
            )
            warning_game = make_game(f'{marker} warning', age_days=58)
            purge_game = make_game(f'{marker} purge', age_days=61)
            rollback_game = make_game(f'{marker} rollback', age_days=62)
            pending_game = make_game(
                f'{marker} pending', age_days=90, pending=True,
            )
            season_game = make_game(
                f'{marker} season', age_days=90, season=99,
            )

            self.models.db.close()
            discovered = asyncio.run(
                incomplete_game_purge_workers.run_discover_incomplete_games(
                    incomplete_game_purge_workers
                    .IncompleteGameDiscoveryRequest(guild_id, today)
                )
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertIn(warning_game.id, discovered.warning_game_ids)
            self.assertIn(purge_game.id, discovered.purge_game_ids)
            self.assertIn(rollback_game.id, discovered.purge_game_ids)
            self.assertNotIn(pending_game.id, discovered.purge_game_ids)
            self.assertNotIn(season_game.id, discovered.purge_game_ids)

            self.models.db.close()
            warning_plan = asyncio.run(
                incomplete_game_purge_workers.run_load_warning_plan(
                    incomplete_game_purge_workers.IncompleteGamePurgeRequest(
                        warning_game.id, guild_id, today,
                    )
                )
            )
            self.assertEqual(len(warning_plan.targets), 1)
            warning_target = warning_plan.targets[0]
            self.assertEqual(warning_target.channel_id, warning_game.game_chan)
            recorded = asyncio.run(
                incomplete_game_purge_workers.run_record_warning_delivery(
                    incomplete_game_purge_workers.WarningDeliveryRequest(
                        warning_game.id,
                        guild_id,
                        warning_target.guild_id,
                        warning_target.channel_id,
                        today,
                    )
                )
            )
            self.assertEqual(
                recorded.status,
                incomplete_game_purge_workers.WARNING_RECORDED,
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.startswith(
                        f'__{warning_game.id}__'
                    )
                    & self.models.GameLog.message.contains(
                        incomplete_game_purge_workers.PURGE_WARNING_MARKER
                    )
                    & (self.models.GameLog.is_protected == True)
                ).count(),
                1,
            )

            with mock.patch.object(
                self.settings,
                'bot',
                SimpleNamespace(locked_game_records=set()),
            ):
                self.models.db.close()
                committed = purge_runner.run(
                    incomplete_game_purge_workers.run_purge_incomplete_game(
                        incomplete_game_purge_workers
                        .IncompleteGamePurgeRequest(
                            purge_game.id, guild_id, today,
                        )
                    )
                )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                committed.status,
                incomplete_game_purge_workers.PURGED,
            )
            self.assertEqual(
                committed.effect_plan.channel_targets,
                (
                    incomplete_game_purge_workers.game_deletion_workers
                    .DeletionChannelTarget(guild_id, purge_game.game_chan),
                ),
            )
            self.assertIsNone(self.models.Game.get_or_none(id=purge_game.id))
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.startswith(
                        f'__{purge_game.id}__'
                    )
                    & self.models.GameLog.message.contains(
                        'Reconciliation targets:'
                    )
                    & (self.models.GameLog.is_protected == True)
                ).count(),
                1,
            )

            def delete_one_lineup_then_fail(loaded):
                loaded.lineup.get().delete_instance()
                raise peewee.OperationalError('P5.14 injected purge rollback')

            with mock.patch.object(
                self.settings,
                'bot',
                SimpleNamespace(locked_game_records=set()),
            ), mock.patch.object(
                self.models.Game,
                'delete_game',
                new=delete_one_lineup_then_fail,
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'P5.14 injected purge rollback',
                ):
                    purge_runner.run(
                        incomplete_game_purge_workers
                        .run_purge_incomplete_game(
                            incomplete_game_purge_workers
                            .IncompleteGamePurgeRequest(
                                rollback_game.id, guild_id, today,
                            )
                        )
                    )
            purge_runner.run(asyncio.sleep(0))
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNotNone(
                self.models.Game.get_or_none(id=rollback_game.id)
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == rollback_game.id
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.startswith(
                        f'__{rollback_game.id}__'
                    )
                    & self.models.GameLog.message.contains(
                        'Reconciliation targets:'
                    )
                ).count(),
                0,
            )
        finally:
            purge_runner.close()
            self.models.db.connect(reuse_if_open=True)
            for game_id in sorted(game_ids):
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.startswith(f'__{game_id}__')
                ).execute()
            temporary_member_ids = tuple(
                row[0] for row in self.models.DiscordMember.select(
                    self.models.DiscordMember.id
                ).where(
                    self.models.DiscordMember.discord_id.in_(
                        (id_base, id_base + 1)
                    )
                ).tuples()
            )
            if temporary_member_ids:
                self.models.Player.delete().where(
                    self.models.Player.discord_member.in_(temporary_member_ids)
                ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(
                    (id_base, id_base + 1)
                )
            ).execute()
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.id.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                0,
            )

    def test_bot_extensions_load_with_background_tasks_disabled(self):
        import bot as bot_module

        extension_names = (
            'modules.games',
            'modules.customhelp',
            'modules.matchmaking',
            'modules.administration',
            'modules.misc',
            'modules.league',
            'modules.api_cog',
            'modules.bullet',
            'modules.antiscam',
        )

        async def load_extensions():
            bot_module.configure_runtime_arguments(['--skip_tasks'])
            instance = bot_module.MyBot()
            try:
                for extension_name in extension_names:
                    if extension_name == 'modules.bullet':
                        spreadsheet_config = mock.mock_open(
                            read_data='{"spreadsheet_key": "offline-test"}'
                        )
                        with mock.patch('builtins.open', spreadsheet_config):
                            await instance.load_extension(extension_name)
                    else:
                        await instance.load_extension(extension_name)
                self.assertEqual(
                    set(instance.extensions),
                    set(extension_names),
                )
                self.assertFalse(self.settings.run_tasks)

                matchmaking_cog = instance.get_cog('matchmaking')
                league_cog = instance.get_cog('league')
                self.assertFalse(
                    matchmaking_cog.task_purge_expired_games.is_running()
                )
                self.assertFalse(
                    league_cog.task_send_polychamps_invite.is_running()
                )
                self.assertFalse(
                    league_cog.task_draft_reminders.is_running()
                )
            finally:
                await instance.close()
                self.settings.bot = None

        asyncio.run(load_extensions())

    def test_h8_startup_ban_reconciliation_real_schema(self):
        """Exercise exact atomic ban replacement on a worker connection."""

        from modules import startup_ban_workers as workers

        suffix = uuid.uuid4().hex[:10]
        id_base = 9_800_000_000_000_000 + uuid.uuid4().int % 1_000_000
        original_banned_ids = tuple(
            row.id
            for row in self.models.DiscordMember.select(
                self.models.DiscordMember.id
            ).where(self.models.DiscordMember.is_banned == True)
        )
        created_ids = []
        try:
            discord_target = self.models.DiscordMember.create(
                discord_id=id_base,
                name=f'H8 Discord {suffix}',
                polytopia_id=f'H8-discord-{suffix}',
                is_banned=False,
            )
            poly_target = self.models.DiscordMember.create(
                discord_id=id_base + 1,
                name=f'H8 Poly {suffix}',
                polytopia_id=f'H8-poly-{suffix}',
                is_banned=False,
            )
            reset_target = self.models.DiscordMember.create(
                discord_id=id_base + 2,
                name=f'H8 Reset {suffix}',
                polytopia_id=f'H8-reset-{suffix}',
                is_banned=True,
            )
            created_ids.extend((
                int(discord_target.id),
                int(poly_target.id),
                int(reset_target.id),
            ))
            request = workers.StartupBanReconciliationRequest(
                discord_ids=(int(discord_target.discord_id),),
                polytopia_ids=(str(poly_target.polytopia_id),),
            )

            original_update = self.models.DiscordMember.update
            update_calls = 0

            def failing_update(*args, **kwargs):
                nonlocal update_calls
                update_calls += 1
                if update_calls == 3:
                    raise peewee.OperationalError(
                        'H8 injected ban reconciliation failure'
                    )
                return original_update(*args, **kwargs)

            self.models.db.close()
            with mock.patch.object(
                self.models.DiscordMember,
                'update',
                side_effect=failing_update,
            ):
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected ban reconciliation failure',
                ):
                    asyncio.run(
                        workers.run_startup_ban_reconciliation(request)
                    )
            self.assertTrue(self.models.db.is_closed())
            self.models.db.connect(reuse_if_open=True)
            self.assertFalse(
                self.models.DiscordMember.get_by_id(discord_target.id).is_banned
            )
            self.assertFalse(
                self.models.DiscordMember.get_by_id(poly_target.id).is_banned
            )
            self.assertTrue(
                self.models.DiscordMember.get_by_id(reset_target.id).is_banned
            )

            self.models.db.close()
            result = asyncio.run(
                workers.run_startup_ban_reconciliation(request)
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertGreaterEqual(result.reset_rows, 3)
            self.assertEqual(result.discord_rows, 1)
            self.assertEqual(result.polytopia_rows, 1)
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(
                self.models.DiscordMember.get_by_id(discord_target.id).is_banned
            )
            self.assertTrue(
                self.models.DiscordMember.get_by_id(poly_target.id).is_banned
            )
            self.assertFalse(
                self.models.DiscordMember.get_by_id(reset_target.id).is_banned
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            self.models.DiscordMember.update(is_banned=False).execute()
            if original_banned_ids:
                self.models.DiscordMember.update(is_banned=True).where(
                    self.models.DiscordMember.id.in_(original_banned_ids)
                ).execute()
            if created_ids:
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(tuple(created_ids))
                ).execute()

    def test_h2_start_authorization_rejects_persisted_bans(self):
        """Exercise account and guild-player bans on the start worker."""

        from modules import game_start_workers as workers

        suffix = uuid.uuid4().hex[:10]
        discord_id = 9_810_000_000_000_000 + uuid.uuid4().int % 1_000_000
        member = None
        player = None
        try:
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=f'H2 Start {suffix}',
                polytopia_id=f'H2-start-{suffix}',
                is_banned=True,
            )
            player = self.models.Player.create(
                discord_member=member,
                guild_id=478571892832206869,
                name=f'H2 Start {suffix}',
                is_banned=False,
            )
            requester = workers.StartMemberSnapshot(
                guild_id=478571892832206869,
                discord_id=discord_id,
                discord_name=f'H2 Start {suffix}',
                discord_nick=None,
                display_name=f'H2 Start {suffix}',
                role_ids=(),
                role_names=(),
                level=5,
                is_mod=True,
                is_staff=True,
                description=f'H2 Start {suffix} ({discord_id})',
                side_position=0,
                lineup_id=None,
                player_id=int(player.id),
                player_name=f'H2 Start {suffix}',
            )
            request = workers.StartPreflightRequest(
                game_id=9_810_000_000,
                guild_id=478571892832206869,
                name='Fields of Fire',
                prefix='$',
                requester=requester,
                require_teams=False,
                invoked_with='/game start',
            )
            fake_game = SimpleNamespace(
                guild_id=478571892832206869,
                is_hosted_by=lambda _discord_id: (True, None),
                is_created_by=lambda _discord_id: True,
                creating_player=lambda: None,
            )

            self.models.db.close()
            with mock.patch.object(
                workers,
                '_load_game',
                return_value=fake_game,
            ), self.assertRaisesRegex(
                workers.GameStartValidationError,
                'ELO Banned',
            ):
                asyncio.run(workers.run_start_preflight(request))
            self.assertTrue(self.models.db.is_closed())

            self.models.db.connect(reuse_if_open=True)
            self.models.DiscordMember.update(is_banned=False).where(
                self.models.DiscordMember.id == member.id
            ).execute()
            self.models.Player.update(is_banned=True).where(
                self.models.Player.id == player.id
            ).execute()
            self.models.db.close()
            with mock.patch.object(
                workers,
                '_load_game',
                return_value=fake_game,
            ), self.assertRaisesRegex(
                workers.GameStartValidationError,
                'ELO Banned',
            ):
                asyncio.run(workers.run_start_preflight(request))
            self.assertTrue(self.models.db.is_closed())
        finally:
            self.models.db.connect(reuse_if_open=True)
            if player is not None:
                self.models.Player.delete_by_id(player.id)
            if member is not None:
                self.models.DiscordMember.delete_by_id(member.id)

    def test_api_application_and_database_route_request(self):
        from modules import api

        discord_id = 8_800_000_000_000_000 + uuid.uuid4().int % 1_000_000
        with self.rollback_scope():
            self.models.DiscordMember.create(
                discord_id=discord_id,
                name='Phase 6 API User',
                polytopia_name='Offline API User',
            )

            async def offline_scopes():
                return ['users:read', 'games:read']

            api.server.dependency_overrides[api.get_scopes] = offline_scopes

            async def request_user():
                messages = []
                request_sent = False

                async def receive():
                    nonlocal request_sent
                    if request_sent:
                        return {'type': 'http.disconnect'}
                    request_sent = True
                    return {
                        'type': 'http.request',
                        'body': b'',
                        'more_body': False,
                    }

                async def send(message):
                    messages.append(message)

                path = f'/users/{discord_id}'
                await api.server(
                    {
                        'type': 'http',
                        'asgi': {'version': '3.0'},
                        'http_version': '1.1',
                        'method': 'GET',
                        'scheme': 'http',
                        'path': path,
                        'raw_path': path.encode('ascii'),
                        'query_string': b'',
                        'headers': [(b'host', b'offline.test')],
                        'client': ('127.0.0.1', 1),
                        'server': ('offline.test', 80),
                        'root_path': '',
                    },
                    receive,
                    send,
                )
                status = next(
                    message['status'] for message in messages
                    if message['type'] == 'http.response.start'
                )
                body = b''.join(
                    message.get('body', b'') for message in messages
                    if message['type'] == 'http.response.body'
                )
                return status, json.loads(body)

            try:
                status, response = asyncio.run(request_user())
            finally:
                api.server.dependency_overrides.clear()

            self.assertEqual(status, 200)
            self.assertEqual(response['discord_id'], discord_id)
            self.assertEqual(response['mobile_name'], 'Offline API User')
            self.assertEqual(response['games'], {})

    def test_database_backed_draft_graph_embed_and_card_rendering(self):
        import discord
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import pyplot as plt
        import numpy as np
        import pandas as pd
        from PIL import Image
        from scipy import signal

        from modules import image_storage, imgen

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex[:10]
        team_name = f'Phase 6 Team {suffix}'
        base_discord_id = (
            8_700_000_000_000_000 + uuid.uuid4().int % 1_000_000
        )

        with self.rollback_scope():
            team = self.models.Team.create(
                name=team_name,
                guild_id=guild_id,
                emoji='🧪',
                image_url='https://offline.test/team.png',
                league_tier=1,
            )
            first_member = self.models.DiscordMember.create(
                discord_id=base_discord_id,
                name='Phase 6 One',
                polytopia_name='PhaseSixOne',
            )
            second_member = self.models.DiscordMember.create(
                discord_id=base_discord_id + 1,
                name='Phase 6 Two',
                polytopia_name='PhaseSixTwo',
            )
            first_player = self.models.Player.create(
                discord_member=first_member,
                guild_id=guild_id,
                name='Phase 6 One',
                team=team,
            )
            second_player = self.models.Player.create(
                discord_member=second_member,
                guild_id=guild_id,
                name='Phase 6 Two',
                team=team,
            )
            game = self.models.Game.create(
                guild_id=guild_id,
                host=first_player,
                name='Phase 6 Draft',
                notes='transactional integration fixture',
                is_pending=True,
                size=[1, 1],
                expiration=datetime.datetime.now()
                + datetime.timedelta(hours=24),
            )
            first_side = self.models.GameSide.create(
                game=game,
                team=team,
                size=1,
                position=1,
                sidename='Alpha',
            )
            second_side = self.models.GameSide.create(
                game=game,
                size=1,
                position=2,
                sidename='Beta',
            )
            self.models.Lineup.create(
                game=game,
                gameside=first_side,
                player=first_player,
            )
            self.models.Lineup.create(
                game=game,
                gameside=second_side,
                player=second_player,
            )

            draft_order = game.draft_order()
            self.assertEqual(len(draft_order), 2)
            self.assertEqual(
                {pick['player'].id for pick in draft_order},
                {first_player.id, second_player.id},
            )

            embed, content = game.embed(prefix='$')
            self.assertIsInstance(embed, discord.Embed)
            self.assertIn(f'Open Game {game.id}', embed.title)
            self.assertIn('This match is now full', content)
            self.assertIn(f'`$start {game.id} Name of Game`', content)

            history_start = datetime.datetime(2026, 1, 1, 12, 0)
            for day, elo in ((0, 1000), (2, 1025), (4, 1015)):
                history_game = self.models.Game.create(
                    guild_id=guild_id,
                    name=f'History {day} {suffix}',
                    completed_ts=history_start + datetime.timedelta(days=day),
                    is_completed=True,
                    is_confirmed=True,
                    size=[2, 2],
                )
                self.models.GameSide.create(
                    game=history_game,
                    team=team,
                    size=2,
                    position=1,
                    team_elo_after_game=elo,
                )

            history_query = (
                self.models.GameSide
                .select(
                    self.models.Game.completed_ts,
                    self.models.GameSide.team_elo_after_game.alias('elo'),
                )
                .join(self.models.Game)
                .where(
                    (self.models.GameSide.team == team)
                    & self.models.GameSide.team_elo_after_game.is_null(False)
                )
                .order_by(self.models.Game.completed_ts)
            )
            history = pd.DataFrame(history_query.dicts())
            resampled = (
                history
                .set_index('completed_ts')
                .resample('D')
                .mean()
                .interpolate()
                .reset_index()
            )
            smoothed = signal.savgol_filter(
                resampled['elo'].values,
                window_length=3,
                polyorder=2,
            )
            self.assertTrue(np.isfinite(smoothed).all())

            figure, axis = plt.subplots()
            try:
                axis.plot(resampled['completed_ts'], smoothed)
                graph = BytesIO()
                figure.savefig(graph, format='png')
                self.assertTrue(
                    graph.getvalue().startswith(b'\x89PNG\r\n\x1a\n')
                )
            finally:
                plt.close(figure)

            source_images = [
                Image.new('RGBA', (100, 100), '#00ff00'),
                Image.new('RGBA', (100, 100), '#0000ff'),
            ]
            with mock.patch.object(
                    imgen, 'fetch_image', side_effect=source_images):
                promotion = imgen.arrow_card(
                    'PROMOTION',
                    team.name,
                    'left',
                    'right',
                    [('u', '#00ff00')],
                )
            try:
                self.assertGreater(promotion.fp.getbuffer().nbytes, 0)
            finally:
                promotion.close()

            source_images = [
                Image.new('RGBA', (100, 100), '#00ff00'),
                Image.new('RGBA', (100, 100), '#0000ff'),
            ]
            with mock.patch.object(
                    imgen, 'fetch_image', side_effect=source_images):
                demotion = imgen.arrow_card(
                    'DEMOTION',
                    team.name,
                    'left',
                    'right',
                    [('r', '#ff0000'), ('l', '#00ff00')],
                )
            try:
                self.assertGreater(demotion.fp.getbuffer().nbytes, 0)
            finally:
                demotion.close()

            fake_member = SimpleNamespace(
                id=first_member.discord_id,
                name=first_member.name,
                guild=SimpleNamespace(id=guild_id),
                display_avatar=SimpleNamespace(
                    replace=lambda **_kwargs: 'offline-avatar'
                ),
            )
            fake_role = SimpleNamespace(
                name=team.name,
                colour=discord.Colour.blue(),
                color=discord.Colour.blue(),
            )
            source_images = [
                Image.new('RGBA', (100, 100), '#00ff00'),
                Image.new('RGBA', (100, 100), '#0000ff'),
            ]
            with mock.patch.object(
                    image_storage, 'resolve_image',
                    return_value=team.image_url):
                with mock.patch.object(
                        imgen, 'fetch_image', side_effect=source_images):
                    draft_card = imgen.player_draft_card(
                        fake_member,
                        fake_role,
                    )
            try:
                self.assertIn(team.name, draft_card.filename)
                self.assertGreater(draft_card.fp.getbuffer().nbytes, 0)
            finally:
                draft_card.close()

        self.assertEqual(
            self.models.Team.select().where(
                self.models.Team.name == team_name
            ).count(),
            0,
        )

    def test_game_log_worker_reads_real_schema_without_writes(self):
        """Exercise P4.4's bounded read under the unchanged identity gate."""

        from modules import beta_readiness, game_log_workers

        guild_id = beta_readiness.BETA_GUILD_ID
        before_count = self.models.GameLog.select().count()
        request = game_log_workers.GameLogRequest(
            guild_id=guild_id,
            requester_id=int(self.settings.owner_id),
            requester_is_staff=True,
            requester_is_owner=True,
            key=game_log_workers.GameLogKey(scope='guild'),
        )
        result = game_log_workers.read_game_logs(request)

        self.assertEqual(result.key.scope, 'guild')
        self.assertLessEqual(len(result.rows), game_log_workers.MAX_LOG_ROWS)
        self.assertTrue(all(
            row.guild_id in {0, guild_id} for row in result.rows
        ))
        returned_ids = tuple(row.log_id for row in result.rows)
        if returned_ids:
            protected_count = self.models.GameLog.select().where(
                self.models.GameLog.id.in_(returned_ids),
                self.models.GameLog.is_protected == 1,
            ).count()
            self.assertEqual(protected_count, 0)
        self.assertEqual(self.models.GameLog.select().count(), before_count)

    def test_league_season_worker_reads_real_schema_without_writes(self):
        """Exercise P8.12's one-query snapshot under the identity gate."""

        from modules import beta_readiness, league_season_workers

        guild_id = beta_readiness.BETA_GUILD_ID
        before_counts = (
            self.models.Game.select().count(),
            self.models.GameSide.select().count(),
            self.models.Team.select().count(),
            self.models.GameLog.select().count(),
        )
        request = league_season_workers.LeagueSeasonRequest(
            guild_id=guild_id,
            requester_id=int(self.settings.owner_id),
            season=None,
            league_scope=True,
            channel_allowed=True,
            tier_labels=tuple(
                (int(number), str(name))
                for number, name in self.settings.league_tiers
            ),
        )
        result = league_season_workers.load_league_season(request)

        self.assertEqual(result.guild_id, guild_id)
        self.assertIsNone(result.season)
        self.assertLessEqual(
            sum(len(tier.teams) for tier in result.tiers),
            league_season_workers.MAX_SEASON_ROWS,
        )
        self.assertTrue(all(tier.teams for tier in result.tiers))
        self.assertEqual(
            (
                self.models.Game.select().count(),
                self.models.GameSide.select().count(),
                self.models.Team.select().count(),
                self.models.GameLog.select().count(),
            ),
            before_counts,
        )

    def test_free_agent_post_state_commits_rolls_back_and_cleans_up(self):
        """Exercise P8.13 Configuration+audit atomicity under the dev gate."""

        from modules import league_free_agents_workers

        guild_id = 8_000_000_000_000_000_000 + (uuid.uuid4().int % 100_000_000)
        actor_id = int(self.settings.owner_id)
        try:
            initial = league_free_agents_workers.load_draft_state(guild_id)
            self.assertIsNone(initial.announcement_message_id)
            self.assertEqual(
                self.models.Configuration.select().where(
                    self.models.Configuration.guild_id == guild_id
                ).count(),
                0,
            )

            committed = league_free_agents_workers.persist_draft_state(
                league_free_agents_workers.DraftPersistRequest(
                    guild_id=guild_id,
                    requester_id=actor_id,
                    requester_name='P8.13 integration actor',
                    expected_message_id=None,
                    expected_channel_id=None,
                    announcement_message_id=700,
                    announcement_channel_id=400,
                    added_message='Owned integration message',
                    opened_at='2026-08-08T12:00:00+00:00',
                )
            )
            self.assertEqual(committed.announcement_message_id, 700)
            config = self.models.Configuration.get(
                self.models.Configuration.guild_id == guild_id
            ).polychamps_draft
            self.assertEqual(config['announcement_message'], 700)
            self.assertEqual(config['draft_message'], 'Owned integration message')
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.guild_id == guild_id
                ).count(),
                1,
            )

            toggled = league_free_agents_workers.transition_draft_state(
                league_free_agents_workers.DraftTransitionRequest(
                    guild_id=guild_id,
                    requester_id=actor_id,
                    requester_name='P8.13b integration actor',
                    expected_message_id=700,
                    expected_channel_id=400,
                    operation='toggle',
                )
            )
            self.assertTrue(toggled.previous_open)
            self.assertFalse(toggled.draft_open)
            self.assertFalse(
                self.models.Configuration.get(
                    self.models.Configuration.guild_id == guild_id
                ).polychamps_draft['draft_open']
            )
            league_free_agents_workers.write_signup_audit(
                league_free_agents_workers.SignupAuditRequest(
                    guild_id=guild_id,
                    requester_id=actor_id,
                    requester_name='P8.13b integration actor',
                    expected_message_id=700,
                    expected_channel_id=400,
                    action='leave',
                    role_name='Free Agent',
                )
            )
            with self.assertRaises(
                league_free_agents_workers.FreeAgentPostConflictError
            ):
                league_free_agents_workers.write_signup_audit(
                    league_free_agents_workers.SignupAuditRequest(
                        guild_id=guild_id,
                        requester_id=actor_id,
                        requester_name='P8.13b integration actor',
                        expected_message_id=700,
                        expected_channel_id=400,
                        action='join',
                        role_name='Free Agent',
                    )
                )

            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=RuntimeError('forced audit rollback'),
            ):
                with self.assertRaises(RuntimeError):
                    league_free_agents_workers.persist_draft_state(
                        league_free_agents_workers.DraftPersistRequest(
                            guild_id=guild_id,
                            requester_id=actor_id,
                            requester_name='P8.13 integration actor',
                            expected_message_id=700,
                            expected_channel_id=400,
                            announcement_message_id=701,
                            announcement_channel_id=401,
                            added_message='Must roll back',
                            opened_at='2026-08-08T12:01:00+00:00',
                        )
                    )
            config = self.models.Configuration.get(
                self.models.Configuration.guild_id == guild_id
            ).polychamps_draft
            self.assertEqual(config['announcement_message'], 700)
            self.assertEqual(config['announcement_channel'], 400)
            self.assertFalse(config['draft_open'])
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.guild_id == guild_id
                ).count(),
                3,
            )

            concluded = league_free_agents_workers.transition_draft_state(
                league_free_agents_workers.DraftTransitionRequest(
                    guild_id=guild_id,
                    requester_id=actor_id,
                    requester_name='P8.13b integration actor',
                    expected_message_id=700,
                    expected_channel_id=400,
                    operation='conclude',
                )
            )
            self.assertEqual(concluded.operation, 'conclude')
            self.assertEqual(
                self.models.Configuration.get(
                    self.models.Configuration.guild_id == guild_id
                ).polychamps_draft,
                self.models.Configuration.draft_config_defaults(),
            )
        finally:
            self.models.GameLog.delete().where(
                self.models.GameLog.guild_id == guild_id
            ).execute()
            self.models.Configuration.delete().where(
                self.models.Configuration.guild_id == guild_id
            ).execute()

        self.assertEqual(
            self.models.GameLog.select().where(
                self.models.GameLog.guild_id == guild_id
            ).count(),
            0,
        )
        self.assertEqual(
            self.models.Configuration.select().where(
                self.models.Configuration.guild_id == guild_id
            ).count(),
            0,
        )

    def test_league_roster_card_resolves_team_image_without_writes(self):
        """Exercise the P8.14 worker-local team-image read under the dev gate."""

        from modules import league_roster_cards_workers as workers

        team = (
            self.models.Team.select()
            .where(self.models.Team.is_hidden == 0)
            .order_by(self.models.Team.id)
            .first()
        )
        if team is None:
            self.skipTest('development database has no visible team to inspect')
        before = (
            self.models.Team.select().count(),
            self.models.GameLog.select().count(),
        )
        rendered = SimpleNamespace(fp=BytesIO(b'owned-png'), close=mock.Mock())
        request = workers.RosterCardRequest(
            guild_id=int(team.guild_id),
            mode='promote',
            top_text='PROMOTION',
            bottom_text=str(team.name),
            left=workers.ImageSource('url', 'https://example.invalid/player.png'),
            right=workers.ImageSource('team', str(team.name)),
            role_colours=(workers.RoleColourSnapshot(str(team.name), '#123456'),),
        )
        with mock.patch.object(
            workers.image_storage,
            'resolve_image',
            return_value='https://offline.test/team.png',
        ), mock.patch.object(
            workers.imgen, 'arrow_card', return_value=rendered
        ):
            result = asyncio.run(workers.run_roster_card(request))
        self.assertEqual(result.image_bytes, b'owned-png')
        self.assertEqual(
            (
                self.models.Team.select().count(),
                self.models.GameLog.select().count(),
            ),
            before,
        )

    def test_league_draft_card_reads_player_and_team_without_writes(self):
        """Exercise the P8.15 worker-local draft-card reads under the dev gate."""

        from modules import league_draft_cards_workers as workers

        player = (
            self.models.Player.select(self.models.Player)
            .join(self.models.DiscordMember)
            .order_by(self.models.Player.id)
            .first()
        )
        if player is None:
            self.skipTest('development database has no registered player to inspect')
        team = (
            self.models.Team.select()
            .where(
                (self.models.Team.guild_id == player.guild_id)
                & (self.models.Team.is_hidden == 0)
            )
            .order_by(self.models.Team.id)
            .first()
        )
        if team is None:
            self.skipTest('player guild has no visible Team to inspect')
        before = (
            self.models.Player.select().count(),
            self.models.Team.select().count(),
            self.models.GameLog.select().count(),
        )
        rendered = SimpleNamespace(fp=BytesIO(b'draft-png'), close=mock.Mock())
        request = workers.DraftCardRequest(
            guild_id=int(player.guild_id),
            player_discord_id=int(player.discord_member.discord_id),
            player_name=str(player.discord_member.name),
            player_avatar_url='https://offline.test/player.png',
            team_name=str(team.name),
            role_colours=(workers.RoleColourSnapshot(str(team.name), '#123456'),),
        )
        with mock.patch.object(
            workers.image_storage, 'resolve_image', return_value='/tmp/team.png'
        ), mock.patch.object(
            workers.imgen,
            'player_draft_card_from_sources',
            return_value=rendered,
        ):
            result = asyncio.run(workers.run_draft_card(request))
        self.assertEqual(result.image_bytes, b'draft-png')
        self.assertEqual(
            (
                self.models.Player.select().count(),
                self.models.Team.select().count(),
                self.models.GameLog.select().count(),
            ),
            before,
        )

    def test_league_trade_price_reads_real_schema_without_writes(self):
        """Exercise P8.16's exact Player and three-season read under the gate."""

        from modules import league_trade_price_workers as workers

        lineup = (
            self.models.Lineup.select(self.models.Lineup)
            .join(self.models.Game)
            .where(
                (self.models.Game.is_completed == 1)
                & (self.models.Game.is_confirmed == 1)
                & self.models.Game.league_season.is_null(False)
            )
            .order_by(self.models.Game.league_season.desc())
            .first()
        )
        if lineup is None:
            self.skipTest('development database has no confirmed league player')
        player = lineup.player
        ending_season = int(lineup.game.league_season)
        before = (
            self.models.Player.select().count(),
            self.models.DiscordMember.select().count(),
            self.models.GameLog.select().count(),
        )
        result = asyncio.run(
            workers.run_trade_price(
                workers.TradePriceRequest(
                    guild_id=int(player.guild_id),
                    player_discord_id=int(player.discord_member.discord_id),
                    player_display_name=str(player.name),
                    ending_season=ending_season,
                    leadership_adjustment=False,
                )
            )
        )
        self.assertEqual(result.ending_season, ending_season)
        self.assertEqual(len(result.seasons), 3)
        self.assertGreaterEqual(result.price, 0)
        self.assertEqual(
            (
                self.models.Player.select().count(),
                self.models.DiscordMember.select().count(),
                self.models.GameLog.select().count(),
            ),
            before,
        )

    def test_league_export_reads_real_schema_without_writes(self):
        """Exercise P8.17's fixed league export scope under the dev gate."""

        import gzip
        from modules import league_export_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        before = (
            self.models.Game.select().count(),
            self.models.GameLog.select().count(),
            self.models.Player.select().count(),
        )
        request = workers.LeagueExportRequest(
            guild_id=guild_id,
            requester_id=1,
            requester_is_staff=True,
            league_scope=True,
            include_logs=False,
            attachment_limit=workers.DEFAULT_ATTACHMENT_LIMIT,
        )
        try:
            result = asyncio.run(workers.run_league_export(request))
        except workers.LeagueExportEmptyError:
            result = None
        if result is not None:
            self.assertGreater(result.game_count, 0)
            csv_text = gzip.decompress(result.payload).decode('utf-8')
            self.assertTrue(csv_text.startswith('game_id,server,season,'))
        self.assertEqual(
            (
                self.models.Game.select().count(),
                self.models.GameLog.select().count(),
                self.models.Player.select().count(),
            ),
            before,
        )

    def test_league_inactivity_reads_and_audits_real_schema_safely(self):
        """Exercise P8.18 selection and exact audit cleanup under the gate."""

        from modules import league_inactivity_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        cutoff = datetime.date.today() - datetime.timedelta(
            days=workers.ACTIVITY_DAYS
        )
        active_lineup = (
            self.models.Lineup
            .select(self.models.Lineup)
            .join(self.models.Game)
            .where(
                (self.models.Game.guild_id == guild_id)
                & (
                    (self.models.Game.date > cutoff)
                    | (self.models.Game.is_completed == False)
                )
            )
            .first()
        )
        if active_lineup is None:
            self.skipTest('development database has no recent/incomplete lineup')
        active_discord_id = int(
            active_lineup.player.discord_member.discord_id
        )
        synthetic_id = 9_000_000_000_000_001
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        old_join = now - 120 * 86400
        before = (
            self.models.Player.select().count(),
            self.models.GameLog.select().count(),
            self.models.Lineup.select().count(),
        )
        request = workers.InactivityPreviewRequest(
            guild_id=guild_id,
            requester_id=1,
            requester_is_mod=True,
            league_scope=True,
            now_timestamp=now,
            inactive_role_id=99,
            inactive_role_name='Inactive',
            protected_role_names=('Mod',),
            missing_protected_role_names=(),
            members=(
                workers.InactivityMemberSnapshot(
                    member_id=active_discord_id,
                    display_name='Active fixture member',
                    joined_timestamp=old_join,
                    role_ids=(),
                    role_names=(),
                    is_bot=False,
                    is_owner=False,
                ),
                workers.InactivityMemberSnapshot(
                    member_id=synthetic_id,
                    display_name='Synthetic inactive candidate',
                    joined_timestamp=old_join,
                    role_ids=(),
                    role_names=(),
                    is_bot=False,
                    is_owner=False,
                ),
            ),
        )
        result = asyncio.run(workers.run_inactivity_preview(request))
        self.assertNotIn(active_discord_id, result.candidate_ids)
        self.assertIn(synthetic_id, result.candidate_ids)
        self.assertEqual(
            (
                self.models.Player.select().count(),
                self.models.GameLog.select().count(),
                self.models.Lineup.select().count(),
            ),
            before,
        )

        audit_log_id = asyncio.run(workers.record_inactive_role_change(
            workers.InactiveRoleAuditRequest(
                guild_id=guild_id,
                member_id=active_discord_id,
                role_name='Inactive',
                applied=True,
            )
        ))
        self.assertIsNotNone(audit_log_id)
        try:
            audit = self.models.GameLog.get_by_id(audit_log_id)
            self.assertEqual(int(audit.guild_id), guild_id)
            self.assertIn('Inactive', audit.message)
        finally:
            self.models.GameLog.delete_by_id(audit_log_id)
        self.assertEqual(
            (
                self.models.Player.select().count(),
                self.models.GameLog.select().count(),
                self.models.Lineup.select().count(),
            ),
            before,
        )

    def test_member_removal_worker_commits_and_rolls_back_real_graph(self):
        """Exercise P8.19's departure cleanup on a worker-owned connection."""

        from modules import member_removal_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        suffix = uuid.uuid4().hex
        marker = f'P8.19 member removal {suffix}'
        discord_id = 9_080_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        game_ids = []

        def make_game(*, pending):
            game = self.models.Game.create(
                guild_id=guild_id,
                host=player,
                expiration=(
                    datetime.datetime.now() + datetime.timedelta(days=1)
                ),
                name=f'{marker} {"pending" if pending else "incomplete"}',
                notes=marker,
                is_pending=pending,
                is_completed=False,
                is_ranked=False,
                is_mobile=True,
                size=[1],
            )
            game_ids.append(int(game.id))
            side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
            )
            lineup = self.models.Lineup.create(
                game=game,
                gameside=side,
                player=player,
            )
            return game, lineup

        try:
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=f'P819 Member {suffix}',
                polytopia_name=f'P819{suffix}',
            )
            player = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member.name,
            )
            pending_game, pending_lineup = make_game(pending=True)
            incomplete_game, incomplete_lineup = make_game(pending=False)
            request = workers.MemberRemovalRequest(
                guild_id=guild_id,
                member_id=discord_id,
                member_description=marker,
            )

            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=peewee.OperationalError(
                    'P8.19 injected audit failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected audit failure',
                ):
                    asyncio.run(workers.run_member_removal(request))
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNotNone(
                self.models.Lineup.get_or_none(id=pending_lineup.id)
            )
            self.assertIsNotNone(
                self.models.Lineup.get_or_none(id=incomplete_lineup.id)
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                0,
            )

            self.models.db.close()
            result = asyncio.run(workers.run_member_removal(request))
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(result.registered)
            self.assertEqual(result.pending_game_ids, (pending_game.id,))
            self.assertEqual(
                result.incomplete_game_ids,
                (incomplete_game.id,),
            )
            self.assertEqual(result.deleted_pending_count, 1)
            self.assertIsNone(
                self.models.Lineup.get_or_none(id=pending_lineup.id)
            )
            self.assertIsNotNone(
                self.models.Lineup.get_or_none(id=incomplete_lineup.id)
            )
            audit = self.models.GameLog.get(
                self.models.GameLog.message.contains(marker)
            )
            self.assertEqual(int(audit.guild_id), guild_id)
            self.assertIn(str(pending_game.id), audit.message)
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_ids:
                self.models.Lineup.delete().where(
                    self.models.Lineup.game.in_(game_ids)
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game.in_(game_ids)
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id.in_(game_ids)
                ).execute()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(marker)
            ).execute()
            player_ids = tuple(
                row[0]
                for row in self.models.Player.select(
                    self.models.Player.id
                ).join(self.models.DiscordMember).where(
                    self.models.DiscordMember.discord_id == discord_id
                ).tuples()
            )
            if player_ids:
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids)
                ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id == discord_id
            ).execute()
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.id.in_(game_ids)
                ).count() if game_ids else 0,
                0,
            )

    def test_member_join_worker_upserts_loads_and_claims_real_side_channel(self):
        """Exercise the rejoin write/read/reconciliation graph off-loop."""

        from modules import member_join_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        suffix = uuid.uuid4().hex
        marker = f'P8.20 member join {suffix}'
        target_discord_id = 9_081_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        ally_discord_id = 9_082_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        game_id = None
        target_member_id = None
        ally_member_id = None
        try:
            target_member = self.models.DiscordMember.create(
                discord_id=target_discord_id,
                name=f'P820 Target {suffix}',
                polytopia_name=f'P820T{suffix}',
            )
            target_member_id = int(target_member.id)
            ally_member = self.models.DiscordMember.create(
                discord_id=ally_discord_id,
                name=f'P820 Ally {suffix}',
                polytopia_name=f'P820A{suffix}',
            )
            ally_member_id = int(ally_member.id)
            ally = self.models.Player.create(
                discord_member=ally_member,
                guild_id=guild_id,
                name=ally_member.name,
            )
            game = self.models.Game.create(
                guild_id=guild_id,
                host=ally,
                expiration=datetime.datetime.now() + datetime.timedelta(days=1),
                name=marker,
                notes=marker,
                is_pending=False,
                is_completed=False,
                is_ranked=False,
                is_mobile=True,
                size=[2],
            )
            game_id = int(game.id)
            side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=2,
            )
            self.models.Lineup.create(
                game=game,
                gameside=side,
                player=ally,
            )
            join_request = workers.MemberJoinRequest(
                guild_id=guild_id,
                member_id=target_discord_id,
                discord_name=target_member.name,
                discord_nick='Returned',
            )

            with mock.patch.object(
                workers,
                '_missing_side_channels',
                side_effect=peewee.OperationalError(
                    'P8.20 injected snapshot failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected snapshot failure',
                ):
                    asyncio.run(workers.run_member_join(join_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNone(
                self.models.Player.get_or_none(
                    (self.models.Player.discord_member == target_member_id)
                    & (self.models.Player.guild_id == guild_id)
                )
            )

            self.models.db.close()
            upserted = asyncio.run(workers.run_member_join(join_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(upserted.registered)
            self.assertTrue(upserted.local_player_created)
            self.assertEqual(upserted.missing_side_channels, ())
            target_player = self.models.Player.get(
                (self.models.Player.discord_member == target_member_id)
                & (self.models.Player.guild_id == guild_id)
            )
            self.models.Lineup.create(
                game=game,
                gameside=side,
                player=target_player,
            )

            self.models.db.close()
            loaded = asyncio.run(workers.run_member_join(join_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(loaded.registered)
            self.assertFalse(loaded.local_player_created)
            self.assertEqual(len(loaded.missing_side_channels), 1)
            snapshot = loaded.missing_side_channels[0]
            self.assertEqual(snapshot.game.id, game_id)
            self.assertEqual(snapshot.gameside_id, side.id)
            self.assertEqual(
                tuple(player.discord_member.discord_id for player in snapshot.players),
                (ally_discord_id, target_discord_id),
            )

            external_guild_id = guild_id + 123
            self.models.db.close()
            asyncio.run(workers.run_persist_side_channel(
                workers.PersistSideChannelRequest(
                    game_id=game_id,
                    gameside_id=int(side.id),
                    channel_id=9_999_001,
                    channel_guild_id=external_guild_id,
                )
            ))
            self.models.db.connect(reuse_if_open=True)
            side = self.models.GameSide.get_by_id(side.id)
            self.assertEqual(int(side.team_chan), 9_999_001)
            self.assertEqual(
                int(side.team_chan_external_server),
                external_guild_id,
            )
            self.models.db.close()
            with self.assertRaises(workers.MemberJoinConflictError):
                asyncio.run(workers.run_persist_side_channel(
                    workers.PersistSideChannelRequest(
                        game_id=game_id,
                        gameside_id=int(side.id),
                        channel_id=9_999_002,
                        channel_guild_id=guild_id,
                    )
                ))
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_id is not None:
                self.models.Lineup.delete().where(
                    self.models.Lineup.game == game_id
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            member_ids = tuple(
                value for value in (target_member_id, ally_member_id)
                if value is not None
            )
            if member_ids:
                self.models.Player.delete().where(
                    self.models.Player.discord_member.in_(member_ids)
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(member_ids)
                ).execute()

    def test_channel_reference_worker_rolls_back_and_clears_real_graph(self):
        """Exercise atomic deleted-channel cleanup on a worker connection."""

        from modules import channel_reference_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        suffix = uuid.uuid4().hex
        marker = f'P8.21 channel cleanup {suffix}'
        channel_id = 9_083_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        external_guild_id = guild_id + 123
        game_id = None
        try:
            game = self.models.Game.create(
                guild_id=guild_id,
                name=marker,
                notes=marker,
                is_pending=False,
                is_completed=False,
                is_ranked=False,
                is_mobile=True,
                game_chan=channel_id,
                size=[2],
            )
            game_id = int(game.id)
            side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=2,
                team_chan=channel_id,
                team_chan_external_server=external_guild_id,
            )
            cleanup_request = workers.ChannelDeleteRequest(
                channel_id=channel_id,
                guild_id=guild_id,
                channel_name=f'p821-{suffix}',
            )

            with mock.patch.object(
                workers,
                '_clear_game_references',
                side_effect=peewee.OperationalError(
                    'P8.21 injected full-game update failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected full-game update failure',
                ):
                    asyncio.run(
                        workers.run_channel_reference_cleanup(cleanup_request)
                    )
            self.models.db.connect(reuse_if_open=True)
            game = self.models.Game.get_by_id(game_id)
            side = self.models.GameSide.get_by_id(side.id)
            self.assertEqual(int(game.game_chan), channel_id)
            self.assertEqual(int(side.team_chan), channel_id)
            self.assertEqual(
                int(side.team_chan_external_server),
                external_guild_id,
            )

            self.models.db.close()
            result = asyncio.run(
                workers.run_channel_reference_cleanup(cleanup_request)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(result.gameside_ids, (int(side.id),))
            self.assertEqual(result.side_game_ids, (game_id,))
            self.assertEqual(result.game_ids, (game_id,))
            game = self.models.Game.get_by_id(game_id)
            side = self.models.GameSide.get_by_id(side.id)
            self.assertIsNone(game.game_chan)
            self.assertIsNone(side.team_chan)
            self.assertEqual(
                int(side.team_chan_external_server),
                external_guild_id,
            )

            self.models.db.close()
            repeated = asyncio.run(
                workers.run_channel_reference_cleanup(cleanup_request)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(repeated.cleared_side_count, 0)
            self.assertEqual(repeated.cleared_game_count, 0)
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_id is not None:
                self.models.GameSide.delete().where(
                    self.models.GameSide.game == game_id
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id == game_id
                ).execute()
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                0,
            )

    def test_member_identity_workers_commit_and_roll_back_real_graphs(self):
        """Exercise P8.22 identity/moderation transactions on PostgreSQL."""

        from modules import member_identity_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        other_guild_id = guild_id + 987
        suffix = uuid.uuid4().hex[:10]
        marker = f'P8.22 identity {suffix}'
        discord_id = 9_084_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        old_name = f'P822Old{suffix}'
        new_name = f'P822New{suffix}'
        member_id = None
        player_ids = []
        try:
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=old_name,
            )
            member_id = int(member.id)
            local_player = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                nick='Local Nick',
                name=f'{old_name} (Local Nick)',
            )
            other_player = self.models.Player.create(
                discord_member=member,
                guild_id=other_guild_id,
                nick=None,
                name=old_name,
            )
            player_ids.extend((int(local_player.id), int(other_player.id)))
            username_request = workers.UsernameUpdateRequest(
                discord_id=discord_id,
                before_name=old_name,
                after_name=new_name,
                stored_name=new_name,
                member_description=f'**{marker}** (`{discord_id}`)',
            )

            with mock.patch.object(
                workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError(
                    'P8.22 injected username audit failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'username audit failure',
                ):
                    asyncio.run(workers.run_username_update(username_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.DiscordMember.get_by_id(member_id).name,
                old_name,
            )
            self.assertEqual(
                self.models.Player.get_by_id(local_player.id).name,
                f'{old_name} (Local Nick)',
            )

            self.models.db.close()
            username_result = asyncio.run(
                workers.run_username_update(username_request)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(username_result.registered)
            self.assertEqual(
                username_result.updated_player_ids,
                tuple(sorted(player_ids)),
            )
            self.assertEqual(
                self.models.DiscordMember.get_by_id(member_id).name,
                new_name,
            )
            self.assertEqual(
                self.models.Player.get_by_id(local_player.id).name,
                f'{new_name} (Local Nick)',
            )
            self.assertEqual(
                self.models.Player.get_by_id(other_player.id).name,
                new_name,
            )

            nickname_request = workers.NicknameUpdateRequest(
                guild_id=guild_id,
                member_id=discord_id,
                before_nick='Local Nick',
                after_name=new_name,
                after_nick='Changed Nick',
                member_description=f'**{marker}** (`{discord_id}`)',
            )
            with mock.patch.object(
                workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError(
                    'P8.22 injected nickname audit failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'nickname audit failure',
                ):
                    asyncio.run(workers.run_nickname_update(nickname_request))
            self.models.db.connect(reuse_if_open=True)
            local_player = self.models.Player.get_by_id(local_player.id)
            self.assertEqual(local_player.nick, 'Local Nick')
            self.assertEqual(local_player.name, f'{new_name} (Local Nick)')

            self.models.db.close()
            nickname_result = asyncio.run(
                workers.run_nickname_update(nickname_request)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(nickname_result.registered)
            local_player = self.models.Player.get_by_id(local_player.id)
            self.assertEqual(local_player.nick, 'Changed Nick')
            self.assertEqual(local_player.name, f'{new_name} (Changed Nick)')

            ban_request = workers.EloBanUpdateRequest(
                guild_id=guild_id,
                member_id=discord_id,
                is_banned=True,
                member_description=f'**{marker}** (`{discord_id}`)',
            )
            with mock.patch.object(
                workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError(
                    'P8.22 injected ELO-ban audit failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'ELO-ban audit failure',
                ):
                    asyncio.run(workers.run_elo_ban_update(ban_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertFalse(
                self.models.Player.get_by_id(local_player.id).is_banned
            )

            self.models.db.close()
            ban_result = asyncio.run(workers.run_elo_ban_update(ban_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(ban_result.registered)
            self.assertTrue(
                self.models.Player.get_by_id(local_player.id).is_banned
            )
            self.assertEqual(
                self.models.Player.get_by_id(other_player.id).is_banned,
                False,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                3,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(marker)
            ).execute()
            if player_ids:
                self.models.Player.delete().where(
                    self.models.Player.id.in_(tuple(player_ids))
                ).execute()
            if member_id is not None:
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id == member_id
                ).execute()

    def test_league_channel_cache_reads_real_schema_without_writes(self):
        """Exercise the bounded P8.23 cache loader on PostgreSQL."""

        from modules import league_channel_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        eligible_team_ids = tuple(
            int(team_id)
            for (team_id,) in (
                self.models.Team
                .select(self.models.Team.id)
                .where(
                    (self.models.Team.guild_id == guild_id)
                    & (self.models.Team.is_hidden == False)
                )
                .order_by(self.models.Team.id)
                .tuples()
            )
        )
        expected = ()
        if eligible_team_ids:
            expected = tuple(
                int(channel_id)
                for (channel_id,) in (
                    self.models.GameSide
                    .select(self.models.GameSide.team_chan)
                    .join(self.models.Game)
                    .where(
                        (self.models.GameSide.team_chan.is_null(False))
                        & (self.models.GameSide.game.guild_id == guild_id)
                        & (self.models.GameSide.game.is_confirmed == False)
                        & (self.models.GameSide.team.in_(eligible_team_ids))
                    )
                    .order_by(self.models.GameSide.id)
                    .limit(workers.MAX_LEAGUE_TEAM_CHANNELS + 1)
                    .tuples()
                )
            )

        self.models.db.close()
        result = asyncio.run(workers.run_load_league_team_channels(
            workers.LeagueChannelCacheRequest(guild_id=guild_id)
        ))
        self.models.db.connect(reuse_if_open=True)
        self.assertEqual(result.guild_id, guild_id)
        self.assertEqual(result.channel_ids, expected)

    def test_league_role_reconciliation_commits_and_rolls_back_real_graph(self):
        """Exercise P8.24's worker-owned team-role persistence graph."""

        from modules import league_role_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        suffix = uuid.uuid4().hex[:12]
        marker = f'P8.24 role reconciliation {suffix}'
        discord_id = 9_085_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        house = None
        team = None
        member = None
        player = None

        def transition(*, before=(), after=()):
            return workers.LeagueRoleUpdateRequest(
                guild_id=guild_id,
                member_id=discord_id,
                member_description=f'**{marker}** (`{discord_id}`)',
                before_role_names=tuple(before),
                after_role_names=tuple(after),
            )

        try:
            house = self.models.House.create(
                name=f'P824 House {suffix}',
                emoji='',
            )
            team = self.models.Team.create(
                guild_id=guild_id,
                name=f'P824 Team {suffix}',
                house=house,
                league_tier=2,
                is_hidden=False,
                is_archived=False,
            )
            member = self.models.DiscordMember.create(
                discord_id=discord_id,
                name=f'P824 Member {suffix}',
            )
            player = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member.name,
            )
            self.models.PlayerHousePreference.create(
                player=player,
                house=house,
            )
            assignment = transition(after=(team.name,))

            with mock.patch.object(
                workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError(
                    'P8.24 injected assignment audit failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'assignment audit failure',
                ):
                    asyncio.run(workers.run_league_team_role_update(assignment))
            self.models.db.connect(reuse_if_open=True)
            player = self.models.Player.get_by_id(player.id)
            self.assertIsNone(player.team_id)
            self.assertEqual(
                self.models.PlayerHousePreference.select().where(
                    self.models.PlayerHousePreference.player == player
                ).count(),
                1,
            )

            self.models.db.close()
            assigned = asyncio.run(
                workers.run_league_team_role_update(assignment)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(assigned.registered)
            self.assertEqual(assigned.team_id, int(team.id))
            self.assertEqual(assigned.house_name, house.name)
            player = self.models.Player.get_by_id(player.id)
            self.assertEqual(int(player.team_id), int(team.id))
            self.assertEqual(
                self.models.PlayerHousePreference.select().where(
                    self.models.PlayerHousePreference.player == player
                ).count(),
                0,
            )

            # Preferences chosen after assignment survive becoming teamless.
            self.models.PlayerHousePreference.create(
                player=player,
                house=house,
            )
            removal = transition(before=(team.name,))
            with mock.patch.object(
                workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError(
                    'P8.24 injected removal audit failure'
                ),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'removal audit failure',
                ):
                    asyncio.run(workers.run_league_team_role_update(removal))
            self.models.db.connect(reuse_if_open=True)
            player = self.models.Player.get_by_id(player.id)
            self.assertEqual(int(player.team_id), int(team.id))

            self.models.db.close()
            removed = asyncio.run(
                workers.run_league_team_role_update(removal)
            )
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(removed.registered)
            self.assertIsNone(removed.team_id)
            player = self.models.Player.get_by_id(player.id)
            self.assertIsNone(player.team_id)
            self.assertEqual(
                self.models.PlayerHousePreference.select().where(
                    self.models.PlayerHousePreference.player == player
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker),
                    self.models.GameLog.guild_id == guild_id,
                ).count(),
                2,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(marker)
            ).execute()
            if player is not None:
                self.models.PlayerHousePreference.delete().where(
                    self.models.PlayerHousePreference.player == player.id
                ).execute()
                self.models.Player.delete().where(
                    self.models.Player.id == player.id
                ).execute()
            if member is not None:
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id == member.id
                ).execute()
            if team is not None:
                self.models.Team.delete().where(
                    self.models.Team.id == team.id
                ).execute()
            if house is not None:
                self.models.House.delete().where(
                    self.models.House.id == house.id
                ).execute()

    def test_game_reaction_lookup_reads_real_schema_without_writes(self):
        """Exercise P5.9's bounded reaction-routing snapshot."""

        from modules import game_reaction_workers as workers

        game = (
            self.models.Game
            .select(
                self.models.Game.id,
                self.models.Game.guild_id,
                self.models.Game.is_pending,
            )
            .order_by(self.models.Game.id)
            .first()
        )
        if game is None:
            self.skipTest('development database has no game to inspect')
        expected_external = tuple(
            int(external_id)
            for (external_id,) in (
                self.models.Team
                .select(self.models.Team.external_server)
                .where(
                    (self.models.Team.guild_id == int(game.guild_id))
                    & (self.models.Team.external_server > 0)
                )
                .distinct()
                .order_by(self.models.Team.external_server)
                .limit(workers.MAX_EXTERNAL_SERVERS + 1)
                .tuples()
            )
        )
        self.assertLessEqual(
            len(expected_external),
            workers.MAX_EXTERNAL_SERVERS,
        )

        before_logs = self.models.GameLog.select().count()
        self.models.db.close()
        result = asyncio.run(workers.run_load_reaction_game(
            workers.ReactionGameRequest(game_id=int(game.id))
        ))
        self.models.db.connect(reuse_if_open=True)
        self.assertTrue(result.exists)
        self.assertEqual(result.game_id, int(game.id))

        self.assertEqual(result.guild_id, int(game.guild_id))
        self.assertEqual(result.is_pending, bool(game.is_pending))
        self.assertEqual(result.external_server_ids, expected_external)
        self.assertEqual(self.models.GameLog.select().count(), before_logs)

        self.models.db.close()
        missing = asyncio.run(workers.run_load_reaction_game(
            workers.ReactionGameRequest(game_id=9_999_999_999)
        ))
        self.models.db.connect(reuse_if_open=True)
        self.assertFalse(missing.exists)
        self.assertIsNone(missing.guild_id)
        self.assertEqual(missing.external_server_ids, ())

    def test_staff_help_context_reads_real_game_without_writes(self):
        """Resolve bounded production routing context from one existing game."""

        from modules import staff_help_workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        game = (
            self.models.Game
            .select()
            .where(self.models.Game.guild_id == guild_id)
            .order_by(self.models.Game.id)
            .first()
        )
        if game is None:
            self.skipTest('No retained development game is available.')
        before = self.models.Game.select().count()

        result = staff_help_workers.find_related_game(
            channel_id=0,
            game_id=int(game.id),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.game_id, int(game.id))
        self.assertEqual(result.guild_id, guild_id)
        self.assertEqual(self.models.Game.select().count(), before)

    def test_game_reminder_worker_reads_real_schema_without_writes(self):
        """Exercise P5.11 reminder snapshots without fixture mutation."""

        from modules import game_reminder_workers

        candidates = self.models.Game.search_pending(
            status_filter=1,
            ranked_filter=1,
            limit=1,
        )
        candidate = candidates[0] if candidates else None
        if candidate is None:
            self.skipTest('no existing full ranked pending game is available')

        before_logs = self.models.GameLog.select().count()
        self.models.db.close()
        result = asyncio.run(
            game_reminder_workers.run_load_game_reminders(
                game_reminder_workers.GameReminderRequest(
                    as_of=(
                        datetime.datetime.now()
                        + datetime.timedelta(days=365)
                    ),
                )
            )
        )
        self.models.db.connect(reuse_if_open=True)
        after_logs = self.models.GameLog.select().count()

        represented = {
            item.game_id for item in result.items
        } | set(result.skipped_game_ids)
        self.assertIn(candidate.id, represented)
        for reminder in result.items:
            self.assertTrue(reminder.snapshot.is_pending)
            self.assertTrue(reminder.snapshot.is_ranked)
            self.assertEqual(reminder.game_id, reminder.snapshot.game_id)
            self.assertGreater(reminder.creator_discord_id, 0)
        self.assertEqual(after_logs, before_logs)

    def test_configured_lobby_worker_commits_and_rolls_back_real_graph(self):
        """Exercise P5.12 idempotency and atomic rollback on PostgreSQL."""

        from modules import game_lobby_workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex
        marker = f'P5.12 vacant lobby {suffix}'
        rollback_marker = f'P5.12 rollback {suffix}'
        created_game_ids = set()

        def request(notes):
            return game_lobby_workers.EnsureLobbyRequest(
                guild_id=guild_id,
                size=(1, 1),
                size_display='1v1',
                is_ranked=False,
                remake_partial=False,
                notes=notes,
                notes_log_display=f'*{notes}*',
                expiration_at=(
                    datetime.datetime.now() + datetime.timedelta(hours=30)
                ),
                role_locks=(
                    game_lobby_workers.LobbySideLock(None, None),
                    game_lobby_workers.LobbySideLock(None, None),
                ),
            )

        try:
            result = asyncio.run(
                game_lobby_workers.run_ensure_configured_lobby(request(marker))
            )
            created_game_ids.add(result.game_id)
            self.assertEqual(result.status, game_lobby_workers.CREATED)
            self.models.db.connect(reuse_if_open=True)
            game = self.models.Game.get_by_id(result.game_id)
            self.assertIsNone(game.host)
            self.assertTrue(game.is_pending)
            self.assertFalse(game.is_ranked)
            self.assertEqual(game.size, [1, 1])
            self.assertEqual(
                self.models.GameSide.select().where(
                    self.models.GameSide.game == game
                ).count(),
                2,
            )
            self.assertEqual(
                self.models.Lineup.select().where(
                    self.models.Lineup.game == game
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

            second = asyncio.run(
                game_lobby_workers.run_ensure_configured_lobby(request(marker))
            )
            self.assertEqual(second.status, game_lobby_workers.EXISTING)
            self.assertEqual(second.game_id, result.game_id)
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.notes == marker
                ).count(),
                1,
            )

            original_create = self.models.GameSide.create
            side_calls = 0

            def fail_second_side(**kwargs):
                nonlocal side_calls
                side_calls += 1
                if side_calls == 2:
                    raise RuntimeError('P5.12 side failure')
                return original_create(**kwargs)

            with mock.patch.object(
                self.models.GameSide,
                'create',
                side_effect=fail_second_side,
            ), self.assertRaisesRegex(RuntimeError, 'P5.12 side failure'):
                asyncio.run(
                    game_lobby_workers.run_ensure_configured_lobby(
                        request(rollback_marker)
                    )
                )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.Game.select().where(
                    self.models.Game.notes == rollback_marker
                ).count(),
                0,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(rollback_marker)
                ).count(),
                0,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            for game_id in sorted(created_game_ids):
                game = self.models.Game.get_or_none(
                    self.models.Game.id == game_id
                )
                if game is not None:
                    game.delete_game()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(marker)
                | self.models.GameLog.message.contains(rollback_marker)
            ).execute()

    def test_game_list_broadcast_worker_reads_real_schema_without_writes(self):
        """Exercise P5.13 frozen broadcast rows without database writes."""

        from modules import game_list_broadcast_workers

        guild_id = self.profile.allowed_guild_ids[0]
        before_logs = self.models.GameLog.select().count()
        self.models.db.close()
        result = asyncio.run(
            game_list_broadcast_workers.run_load_game_list_broadcast(
                game_list_broadcast_workers.GameListBroadcastRequest(
                    guild_id=guild_id,
                    ranked_filter=2,
                    as_of=datetime.datetime.now(),
                )
            )
        )
        self.models.db.connect(reuse_if_open=True)
        after_logs = self.models.GameLog.select().count()

        self.assertLessEqual(
            len(result.rows),
            game_list_broadcast_workers.MAX_BROADCAST_GAMES,
        )
        self.assertEqual(result.guild_id, guild_id)
        for item in result.rows:
            self.assertGreater(item.game_id, 0)
            self.assertGreaterEqual(item.players, 0)
            self.assertGreater(item.capacity, 0)
            self.assertLessEqual(item.players, item.capacity)
        self.assertEqual(after_logs, before_logs)

    def test_inactive_kick_preview_and_audit_use_real_schema_safely(self):
        """Exercise P8.19 selection and post-Discord audit under the gate."""

        from modules import league_inactive_kick_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        suffix = uuid.uuid4().hex
        marker = f'P8.19 kick {suffix}'
        id_base = 9_081_000_000_000_000 + (uuid.uuid4().int % 1_000_000)
        unregistered_id = id_base
        eligible_id = id_base + 1
        blocked_id = id_base + 2
        game_ids = []
        team_id = None
        discord_ids = (eligible_id, blocked_id)

        def snapshot(member_id, role_name=None):
            roles = [
                workers.KickRoleSnapshot(1, '@everyone', False),
                workers.KickRoleSnapshot(99, 'Inactive', False),
            ]
            if role_name:
                roles.append(workers.KickRoleSnapshot(100, role_name, False))
            return workers.KickMemberSnapshot(
                member_id=member_id,
                display_name=f'{marker} {member_id}',
                joined_timestamp=(
                    datetime.datetime.now(datetime.timezone.utc).timestamp()
                    - 100 * 86400
                ),
                roles=tuple(roles),
                is_bot=False,
                is_owner=False,
            )

        try:
            eligible_member = self.models.DiscordMember.create(
                discord_id=eligible_id,
                name=f'P819 Eligible {suffix}',
                polytopia_name=f'P819Eligible{suffix}',
            )
            blocked_member = self.models.DiscordMember.create(
                discord_id=blocked_id,
                name=f'P819 Blocked {suffix}',
                polytopia_name=f'P819Blocked{suffix}',
            )
            eligible_player = self.models.Player.create(
                discord_member=eligible_member,
                guild_id=guild_id,
                name=eligible_member.name,
            )
            blocked_player = self.models.Player.create(
                discord_member=blocked_member,
                guild_id=guild_id,
                name=blocked_member.name,
            )
            team = self.models.Team.create(
                guild_id=guild_id,
                name=f'P819 Team {suffix}',
                is_hidden=False,
                is_archived=False,
            )
            team_id = int(team.id)
            game = self.models.Game.create(
                guild_id=guild_id,
                host=blocked_player,
                name=f'{marker} incomplete',
                notes=marker,
                is_pending=False,
                is_completed=False,
                is_ranked=False,
                is_mobile=True,
                size=[1],
            )
            game_ids.append(int(game.id))
            side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
            )
            self.models.Lineup.create(
                game=game,
                gameside=side,
                player=blocked_player,
            )

            preview_request = workers.InactiveKickPreviewRequest(
                guild_id=guild_id,
                requester_id=1,
                requester_is_mod=True,
                league_scope=True,
                now_timestamp=datetime.datetime.now(
                    datetime.timezone.utc
                ).timestamp(),
                inactive_role_id=99,
                inactive_role_name='Inactive',
                starter_role_names=('Newbie',),
                protected_role_names=('Mod',),
                members=(
                    snapshot(unregistered_id),
                    snapshot(eligible_id, team.name),
                    snapshot(blocked_id),
                ),
            )
            self.models.db.close()
            loaded = asyncio.run(workers.run_preview(preview_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                set(loaded.candidate_ids),
                {unregistered_id, eligible_id},
            )
            loaded_rows = {row.member_id: row for row in loaded.decisions}
            self.assertTrue(loaded_rows[eligible_id].has_team_role)
            self.assertIn('pending or incomplete', loaded_rows[blocked_id].reason)

            audit_request = workers.InactiveKickAuditRequest(
                guild_id=guild_id,
                actor_id=1,
                actor_description=f'{marker} actor',
                rows=(
                    workers.KickAuditRow(unregistered_id, 'Unregistered'),
                    workers.KickAuditRow(eligible_id, 'Eligible'),
                ),
            )
            self.models.db.close()
            audit = asyncio.run(workers.record_kicks(audit_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(len(audit.log_ids), 2)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                2,
            )

            before_failure = self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count()
            original_write = self.models.GameLog.write
            calls = 0

            def write_then_fail(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise peewee.OperationalError('P8.19 injected audit failure')
                return original_write(*args, **kwargs)

            with mock.patch.object(
                self.models.GameLog,
                'write',
                side_effect=write_then_fail,
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected audit failure',
                ):
                    asyncio.run(workers.record_kicks(audit_request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                before_failure,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_ids:
                self.models.Lineup.delete().where(
                    self.models.Lineup.game.in_(game_ids)
                ).execute()
                self.models.GameSide.delete().where(
                    self.models.GameSide.game.in_(game_ids)
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id.in_(game_ids)
                ).execute()
            self.models.GameLog.delete().where(
                self.models.GameLog.message.contains(marker)
            ).execute()
            player_ids = tuple(
                row[0]
                for row in self.models.Player.select(
                    self.models.Player.id
                ).join(self.models.DiscordMember).where(
                    self.models.DiscordMember.discord_id.in_(discord_ids)
                ).tuples()
            )
            if player_ids:
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids)
                ).execute()
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(discord_ids)
            ).execute()
            if team_id is not None:
                self.models.Team.delete().where(
                    self.models.Team.id == team_id
                ).execute()
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                0,
            )

    def test_league_invitation_workers_read_commit_and_roll_back_real_schema(self):
        """Exercise P8.27's bounded scan and idempotent sent-date write."""

        from modules import league_invitation_workers

        suffix = uuid.uuid4().hex
        first_discord_id = 8_826_000_000_000_000 + uuid.uuid4().int % 1_000_000
        second_discord_id = first_discord_id + 1
        first = second = None
        era_start, era_end = self.models.moonrise_or_air_date_range()

        try:
            first = self.models.DiscordMember.create(
                discord_id=first_discord_id,
                name=f'P827 Scan {suffix}',
                elo_max=1100,
                elo_max_moonrise=1200,
                polytopia_name='P827 Scan',
                date_polychamps_invite_sent=None,
            )
            request = league_invitation_workers.LeagueInvitationEligibilityRequest(
                as_of=datetime.datetime.now(),
                polychampions_guild_id=self.profile.allowed_guild_ids[0],
                global_guild_ids=tuple(
                    self.settings.servers_included_in_global_lb()
                ),
                era_start=era_start,
                era_end=era_end,
                after_member_id=first.id - 1,
                limit=10,
            )
            batch = asyncio.run(
                league_invitation_workers.run_load_invitation_eligibility(
                    request
                )
            )
            row = next(
                row for row in batch.evaluations if row.member_id == first.id
            )
            self.assertEqual(row.discord_id, first_discord_id)
            self.assertEqual(row.wins, 0)
            self.assertEqual(row.losses, 0)
            self.assertEqual(row.recent_games, 0)
            self.assertFalse(row.eligible)
            self.assertEqual(row.reason, 'insufficient_wins')

            delivery = league_invitation_workers.LeagueInvitationDeliveryRequest(
                member_id=first.id,
                discord_id=first_discord_id,
                sent_on=datetime.date.today(),
            )
            committed = asyncio.run(
                league_invitation_workers.run_record_invitation_delivery(
                    delivery
                )
            )
            self.assertTrue(committed.recorded)
            repeated = asyncio.run(
                league_invitation_workers.run_record_invitation_delivery(
                    delivery
                )
            )
            self.assertFalse(repeated.recorded)

            second = self.models.DiscordMember.create(
                discord_id=second_discord_id,
                name=f'P827 Rollback {suffix}',
                elo_max=1100,
                elo_max_moonrise=1200,
                polytopia_name='P827 Rollback',
                date_polychamps_invite_sent=None,
            )
            rollback_request = (
                league_invitation_workers.LeagueInvitationDeliveryRequest(
                    member_id=second.id,
                    discord_id=second_discord_id,
                    sent_on=datetime.date.today(),
                )
            )
            original_update = league_invitation_workers._update_delivery

            def update_then_fail(value):
                original_update(value)
                raise peewee.OperationalError('P827 forced rollback')

            with mock.patch.object(
                league_invitation_workers,
                '_update_delivery',
                side_effect=update_then_fail,
            ):
                with self.assertRaises(peewee.OperationalError):
                    asyncio.run(
                        league_invitation_workers.run_record_invitation_delivery(
                            rollback_request
                        )
                    )
            second = self.models.DiscordMember.get_by_id(second.id)
            self.assertIsNone(second.date_polychamps_invite_sent)
        finally:
            self.models.DiscordMember.delete().where(
                self.models.DiscordMember.discord_id.in_(
                    (first_discord_id, second_discord_id)
                )
            ).execute()

    def test_operator_tribe_emoji_commits_and_rolls_back_real_schema(self):
        """Exercise P9.3's owner-only worker against real development rows."""

        from modules import operator_tribe_workers

        tribe = (
            self.models.Tribe.select()
            .order_by(self.models.Tribe.id)
            .first()
        )
        self.assertIsNotNone(tribe)
        tribe_id = int(tribe.id)
        original_emoji = str(tribe.emoji or '')
        first_emoji = '🧪' if original_emoji != '🧪' else '⚔️'
        second_emoji = '🛰️' if first_emoji != '🛰️' else '🧭'
        marker = f'P9.3-{uuid.uuid4().hex}'
        guild_id = int(self.profile.allowed_guild_ids[0])
        request = operator_tribe_workers.OperatorTribeMutationRequest(
            guild_id=guild_id,
            requester_id=int(self.settings.owner_id),
            requester_description=marker,
            tribe_lookup=str(tribe.name),
            emoji=first_emoji,
        )

        try:
            self.models.db.close()
            result = asyncio.run(operator_tribe_workers.run_mutation(request))
            self.assertTrue(self.models.db.is_closed())
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(result.changed)
            self.assertEqual(
                self.models.Tribe.get_by_id(tribe_id).emoji,
                first_emoji,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

            failed_request = (
                operator_tribe_workers.OperatorTribeMutationRequest(
                    guild_id=guild_id,
                    requester_id=int(self.settings.owner_id),
                    requester_description=marker,
                    tribe_lookup=str(tribe.name),
                    emoji=second_emoji,
                )
            )
            with mock.patch.object(
                operator_tribe_workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P9.3 forced rollback'),
            ):
                self.models.db.close()
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'forced rollback',
                ):
                    asyncio.run(
                        operator_tribe_workers.run_mutation(failed_request)
                    )
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.Tribe.get_by_id(tribe_id).emoji,
                first_emoji,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                self.models.Tribe.update(emoji=original_emoji).where(
                    self.models.Tribe.id == tribe_id
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(marker)
                ).execute()

    def test_p97a_confirmation_audit_commits_and_rolls_back_atomically(self):
        """Exercise the confirmation/audit boundary on the real schema."""

        from modules import elo_workers

        guild_id = self.settings.server_ids['polychampions']
        suffix = uuid.uuid4().hex[:10]
        marker = f'P9.7a-{suffix}'
        id_base = 8_700_000_000_000_000_000 + (
            uuid.uuid4().int % 100_000_000
        )
        member_ids = []
        player_ids = []
        game_id = None

        try:
            for index, label in enumerate(('Alpha', 'Bravo')):
                member = self.models.DiscordMember.create(
                    discord_id=id_base + index,
                    name=f'{marker}-{label}',
                    polytopia_name=f'{marker}{label}',
                )
                player = self.models.Player.create(
                    discord_member=member,
                    guild_id=guild_id,
                    name=member.name,
                )
                member_ids.append(member.id)
                player_ids.append(player.id)

            game = self.models.Game.create(
                guild_id=guild_id,
                host=player_ids[0],
                notes=marker,
                is_pending=False,
                is_completed=True,
                is_confirmed=False,
                is_ranked=False,
                is_mobile=True,
                size=[1, 1],
            )
            game_id = game.id
            first_side = self.models.GameSide.create(
                game=game,
                position=1,
                sidename='Alpha',
                size=1,
            )
            second_side = self.models.GameSide.create(
                game=game,
                position=2,
                sidename='Bravo',
                size=1,
            )
            self.models.Lineup.create(
                game=game,
                gameside=first_side,
                player=player_ids[0],
            )
            self.models.Lineup.create(
                game=game,
                gameside=second_side,
                player=player_ids[1],
            )
            game.winner = first_side
            game.save()

            requester = f'Integration staff {marker}'
            self.models.db.close()
            result = elo_workers.confirm_game(
                game_id,
                guild_id,
                requester,
            )
            self.models.db.connect(reuse_if_open=True)

            committed = self.models.Game.get_by_id(game_id)
            self.assertEqual(
                result.winner_name,
                self.models.Player.get_by_id(player_ids[0]).name,
            )
            self.assertIsNotNone(result.publication)
            self.assertEqual(result.publication.game.game_id, game_id)
            self.assertTrue(result.publication.game.is_confirmed)
            self.assertEqual(
                result.publication.roster_mentions,
                tuple(f'<@{id_base + index}>' for index in range(2)),
            )
            self.assertTrue(committed.is_confirmed)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

            self.models.Game.update(
                is_confirmed=False,
                completed_ts=None,
            ).where(self.models.Game.id == game_id).execute()
            log_count = self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count()
            self.models.db.close()
            with mock.patch.object(
                elo_workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError(
                    'P9.7a forced audit rollback'
                ),
            ), self.assertRaisesRegex(
                peewee.OperationalError,
                'forced audit rollback',
            ):
                elo_workers.confirm_game(
                    game_id,
                    guild_id,
                    requester,
                )
            self.models.db.connect(reuse_if_open=True)

            rolled_back = self.models.Game.get_by_id(game_id)
            self.assertFalse(rolled_back.is_confirmed)
            self.assertIsNone(rolled_back.completed_ts)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                log_count,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                if game_id is not None:
                    self.models.Game.update(winner=None).where(
                        self.models.Game.id == game_id
                    ).execute()
                    self.models.Lineup.delete().where(
                        self.models.Lineup.game == game_id
                    ).execute()
                    self.models.GameSide.delete().where(
                        self.models.GameSide.game == game_id
                    ).execute()
                    self.models.Game.delete().where(
                        self.models.Game.id == game_id
                    ).execute()
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids or (-1,))
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(member_ids or (-1,))
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(marker)
                ).execute()
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                0,
            )

    def test_p97c_ordinary_win_unwin_snapshots_use_real_schema(self):
        """Exercise ordinary result snapshots on development PostgreSQL."""

        from modules import elo_workers

        guild_id = self.settings.server_ids['polychampions']
        suffix = uuid.uuid4().hex[:10]
        marker = f'P9.7c-{suffix}'
        id_base = 8_600_000_000_000_000_000 + (
            uuid.uuid4().int % 100_000_000
        )
        member_ids = []
        player_ids = []
        game_id = None

        try:
            for index, label in enumerate(('Alpha', 'Bravo')):
                member = self.models.DiscordMember.create(
                    discord_id=id_base + index,
                    name=f'{marker}-{label}',
                    polytopia_name=f'{marker}{label}',
                )
                player = self.models.Player.create(
                    discord_member=member,
                    guild_id=guild_id,
                    name=member.name,
                )
                member_ids.append(member.id)
                player_ids.append(player.id)

            game = self.models.Game.create(
                guild_id=guild_id,
                host=player_ids[0],
                notes=marker,
                is_pending=False,
                is_completed=False,
                is_confirmed=False,
                is_ranked=False,
                is_mobile=True,
                size=[1, 1],
            )
            game_id = game.id
            sides = []
            for position, (label, player_id) in enumerate(
                zip(('Alpha', 'Bravo'), player_ids),
                start=1,
            ):
                side = self.models.GameSide.create(
                    game=game,
                    position=position,
                    sidename=label,
                    size=1,
                )
                sides.append(side)
                self.models.Lineup.create(
                    game=game,
                    gameside=side,
                    player=player_id,
                )

            self.models.db.close()
            win_result = elo_workers.record_win(
                game_id,
                guild_id,
                sides[0].id,
                id_base,
                marker,
                False,
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertIsNotNone(win_result.publication)
            self.assertFalse(win_result.publication.game.is_confirmed)
            self.assertEqual(
                win_result.publication.roster_mentions,
                tuple(f'<@{id_base + index}>' for index in range(2)),
            )

            unwin_result = elo_workers.unwin_game(
                game_id,
                guild_id,
                id_base,
                marker,
                True,
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertIsNotNone(unwin_result.publication)
            self.assertFalse(unwin_result.publication.game.is_completed)
            self.assertIsNone(unwin_result.publication.game.winner_side_id)

            self.models.db.connect(reuse_if_open=True)
            reset = self.models.Game.get_by_id(game_id)
            self.assertFalse(reset.is_completed)
            self.assertFalse(reset.is_confirmed)
            self.assertIsNone(reset.winner)
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                if game_id is not None:
                    self.models.Game.update(winner=None).where(
                        self.models.Game.id == game_id
                    ).execute()
                    self.models.Lineup.delete().where(
                        self.models.Lineup.game == game_id
                    ).execute()
                    self.models.GameSide.delete().where(
                        self.models.GameSide.game == game_id
                    ).execute()
                    self.models.Game.delete().where(
                        self.models.Game.id == game_id
                    ).execute()
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids or (-1,))
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(member_ids or (-1,))
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(marker)
                ).execute()

    def _create_p925_elo_players(self, *, guild_id, marker):
        id_base = 8_700_000_000_000_000_000 + (
            uuid.uuid4().int % 100_000_000
        )
        players = []
        for index, label in enumerate(('Alpha', 'Bravo')):
            member = self.models.DiscordMember.create(
                discord_id=id_base + index,
                name=f'{marker}-{label}',
                polytopia_name=f'{marker}{label}',
            )
            players.append(self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member.name,
            ))
        return tuple(players)

    def _create_p925_ranked_game(
            self, *, guild_id, marker, players, completed_ts):
        game = self.models.Game.create(
            guild_id=guild_id,
            host=players[0],
            name=marker,
            notes=marker,
            date=datetime.date(2026, 8, 11),
            completed_ts=completed_ts,
            is_pending=False,
            is_completed=False,
            is_confirmed=False,
            is_ranked=True,
            is_mobile=True,
            size=[1, 1],
        )
        sides = []
        for position, player in enumerate(players, start=1):
            side = self.models.GameSide.create(
                game=game,
                position=position,
                sidename=f'{marker}-{position}',
                size=1,
            )
            sides.append(side)
            self.models.Lineup.create(
                game=game,
                gameside=side,
                player=player,
            )
        return game, tuple(sides)

    def _create_p117_ranked_2v2_graph(
            self, *, guild_id, marker, completed_ts):
        """Create the smallest graph that takes every ranked ELO branch."""

        id_base = 8_720_000_000_000_000_000 + (
            uuid.uuid4().int % 100_000_000
        )
        players = []
        for index, label in enumerate(('Alpha One', 'Alpha Two',
                                       'Bravo One', 'Bravo Two')):
            member = self.models.DiscordMember.create(
                discord_id=id_base + index,
                name=f'{marker}-{label}',
                polytopia_name=f'{marker}{index}',
            )
            players.append(self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member.name,
            ))

        teams = tuple(
            self.models.Team.create(
                guild_id=guild_id,
                name=f'{marker}-team-{label}',
            )
            for label in ('alpha', 'bravo')
        )
        squads = tuple(
            self.models.Squad.create(
                guild_id=guild_id,
                name=f'{marker}-squad-{label}',
            )
            for label in ('alpha', 'bravo')
        )
        sides = []
        for position, (team, squad, side_players) in enumerate(
            zip(teams, squads, (players[:2], players[2:])), start=1
        ):
            for player in side_players:
                self.models.SquadMember.create(player=player, squad=squad)
            sides.append((team, squad, tuple(side_players), position))

        game = self.models.Game.create(
            guild_id=guild_id,
            host=players[0],
            name=marker,
            notes=marker,
            date=datetime.date(2026, 8, 11),
            completed_ts=completed_ts,
            is_pending=False,
            is_completed=False,
            is_confirmed=False,
            is_ranked=True,
            is_mobile=True,
            size=[2, 2],
        )
        game_sides = []
        for team, squad, side_players, position in sides:
            side = self.models.GameSide.create(
                game=game,
                team=team,
                squad=squad,
                position=position,
                sidename=f'{marker}-{position}',
                size=2,
            )
            game_sides.append(side)
            for player in side_players:
                self.models.Lineup.create(
                    game=game,
                    gameside=side,
                    player=player,
                )
        return game, tuple(game_sides), tuple(players), teams, squads

    def _p117_elo_graph_snapshot(self, *, games, players, teams, squads):
        """Read every persisted rating/snapshot branch in stable order."""

        player_rows = tuple(
            self.models.Player.get_by_id(player.id) for player in players
        )
        member_rows = tuple(
            self.models.DiscordMember.get_by_id(player.discord_member_id)
            for player in players
        )
        game_side_rows = tuple(
            self.models.GameSide.select()
            .where(self.models.GameSide.game.in_(
                tuple(game.id for game in games)
            ))
            .order_by(self.models.GameSide.game, self.models.GameSide.position)
        )
        lineup_rows = tuple(
            self.models.Lineup.select()
            .where(self.models.Lineup.game.in_(
                tuple(game.id for game in games)
            ))
            .order_by(self.models.Lineup.game, self.models.Lineup.id)
        )
        return {
            'players': tuple(
                (row.elo_moonrise, row.elo_max_moonrise,
                 row.elo_alltime, row.elo_max_alltime)
                for row in player_rows
            ),
            'members': tuple(
                (row.elo_moonrise, row.elo_max_moonrise,
                 row.elo_alltime, row.elo_max_alltime)
                for row in member_rows
            ),
            'teams': tuple(
                (self.models.Team.get_by_id(team.id).elo,
                 self.models.Team.get_by_id(team.id).elo_alltime)
                for team in teams
            ),
            'squads': tuple(
                self.models.Squad.get_by_id(squad.id).elo
                for squad in squads
            ),
            'sides': tuple(
                (row.elo_change_team, row.team_elo_after_game,
                 row.elo_change_team_alltime, row.team_elo_after_game_alltime,
                 row.elo_change_squad)
                for row in game_side_rows
            ),
            'lineups': tuple(
                (row.elo_change_player_moonrise,
                 row.elo_after_game_moonrise,
                 row.elo_change_player_alltime,
                 row.elo_after_game_alltime,
                 row.elo_change_discordmember_moonrise,
                 row.elo_after_game_global_moonrise,
                 row.elo_change_discordmember_alltime,
                 row.elo_after_game_global_alltime)
                for row in lineup_rows
            ),
        }

    def test_p925_ranked_win_and_reverse_use_exact_legacy_deltas(self):
        """Characterize a real ranked graph and its retained reversal rules."""

        guild_id = self.profile.allowed_guild_ids[0]
        marker = f'P9.25-ranked-{uuid.uuid4().hex[:10]}'
        completed_ts = datetime.datetime(2500, 1, 1) + datetime.timedelta(
            seconds=uuid.uuid4().int % 20_000_000
        )
        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.completed_ts >= completed_ts
            ).count(),
            0,
        )

        with self.rollback_scope():
            players = self._create_p925_elo_players(
                guild_id=guild_id,
                marker=marker,
            )
            game, sides = self._create_p925_ranked_game(
                guild_id=guild_id,
                marker=marker,
                players=players,
                completed_ts=completed_ts,
            )
            full_game = self.models.Game.load_full_game(game.id)
            winning_side = self.models.GameSide.get_by_id(sides[0].id)
            full_game.declare_winner(winning_side=winning_side, confirm=True)

            refreshed_players = tuple(
                self.models.Player.get_by_id(player.id) for player in players
            )
            self.assertEqual(
                tuple(player.elo_moonrise for player in refreshed_players),
                (1044, 980),
            )
            self.assertEqual(
                tuple(player.elo_alltime for player in refreshed_players),
                (1044, 980),
            )
            lineups = tuple(
                self.models.Lineup.select()
                .where(self.models.Lineup.game == game.id)
                .order_by(self.models.Lineup.id)
            )
            self.assertEqual(
                tuple(row.elo_change_player_moonrise for row in lineups),
                (44, -20),
            )
            self.assertEqual(
                tuple(row.elo_after_game_moonrise for row in lineups),
                (1044, 980),
            )

            global_enabled = (
                guild_id in self.settings.servers_included_in_global_lb()
            )
            members = tuple(
                self.models.DiscordMember.get_by_id(
                    player.discord_member_id
                )
                for player in players
            )
            self.assertEqual(
                tuple(member.elo_moonrise for member in members),
                (1044, 980) if global_enabled else (1000, 1000),
            )

            self.models.Game.load_full_game(game.id).reverse_elo_changes()
            reversed_players = tuple(
                self.models.Player.get_by_id(player.id) for player in players
            )
            self.assertEqual(
                tuple(player.elo_moonrise for player in reversed_players),
                (1000, 1000),
            )
            # Reversal restores current ratings and clears per-game snapshots,
            # while intentionally retaining the historical maximum reached.
            self.assertEqual(
                tuple(player.elo_max_moonrise for player in reversed_players),
                (1044, 1000),
            )
            reversed_lineups = tuple(
                self.models.Lineup.select()
                .where(self.models.Lineup.game == game.id)
                .order_by(self.models.Lineup.id)
            )
            self.assertEqual(
                tuple(row.elo_change_player_moonrise
                      for row in reversed_lineups),
                (0, 0),
            )
            self.assertEqual(
                tuple(row.elo_after_game_moonrise
                      for row in reversed_lineups),
                (None, None),
            )

        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.notes == marker
            ).count(),
            0,
        )

    def test_p925_recalculation_replays_ranked_graph_deterministically(self):
        """Two ranked results reproduce the same ratings and snapshots."""

        guild_id = self.profile.allowed_guild_ids[0]
        marker = f'P9.25-replay-{uuid.uuid4().hex[:10]}'
        completed_ts = datetime.datetime(2501, 1, 1) + datetime.timedelta(
            seconds=uuid.uuid4().int % 20_000_000
        )
        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.completed_ts >= completed_ts
            ).count(),
            0,
        )

        with self.rollback_scope():
            players = self._create_p925_elo_players(
                guild_id=guild_id,
                marker=marker,
            )
            first_game, first_sides = self._create_p925_ranked_game(
                guild_id=guild_id,
                marker=f'{marker}-one',
                players=players,
                completed_ts=completed_ts,
            )
            second_game, second_sides = self._create_p925_ranked_game(
                guild_id=guild_id,
                marker=f'{marker}-two',
                players=players,
                completed_ts=completed_ts + datetime.timedelta(seconds=1),
            )
            self.models.Game.load_full_game(first_game.id).declare_winner(
                winning_side=self.models.GameSide.get_by_id(
                    first_sides[0].id
                ),
                confirm=True,
            )
            self.models.Game.load_full_game(second_game.id).declare_winner(
                winning_side=self.models.GameSide.get_by_id(
                    second_sides[1].id
                ),
                confirm=True,
            )

            def graph_snapshot():
                player_rows = tuple(
                    self.models.Player.select()
                    .where(self.models.Player.id.in_(
                        tuple(player.id for player in players)
                    ))
                    .order_by(self.models.Player.id)
                )
                lineup_rows = tuple(
                    self.models.Lineup.select()
                    .where(self.models.Lineup.game.in_((
                        first_game.id,
                        second_game.id,
                    )))
                    .order_by(
                        self.models.Lineup.game,
                        self.models.Lineup.id,
                    )
                )
                game_rows = tuple(
                    self.models.Game.select()
                    .where(self.models.Game.id.in_((
                        first_game.id,
                        second_game.id,
                    )))
                    .order_by(self.models.Game.completed_ts)
                )
                return (
                    tuple(
                        (row.elo_moonrise, row.elo_max_moonrise,
                         row.elo_alltime, row.elo_max_alltime)
                        for row in player_rows
                    ),
                    tuple(
                        (row.elo_change_player_moonrise,
                         row.elo_after_game_moonrise,
                         row.elo_change_player_alltime,
                         row.elo_after_game_alltime)
                        for row in lineup_rows
                    ),
                    tuple(
                        (row.is_completed, row.is_confirmed,
                         row.winner_id, row.completed_ts)
                        for row in game_rows
                    ),
                )

            before_replay = graph_snapshot()
            self.assertEqual(
                tuple(row[0] for row in before_replay[0]),
                (1010, 1050),
            )
            self.models.Game.recalculate_elo_since(completed_ts)
            self.assertEqual(graph_snapshot(), before_replay)

        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.notes.contains(marker)
            ).count(),
            0,
        )

    def test_p117_ranked_2v2_graph_persists_and_reverses_all_rating_branches(self):
        """A ranked 2v2 stores and reverses player, member, team, and squad ELO."""

        guild_id = self.profile.allowed_guild_ids[0]
        marker = f'P11.7-ranked-{uuid.uuid4().hex[:10]}'
        completed_ts = datetime.datetime(2502, 1, 1) + datetime.timedelta(
            seconds=uuid.uuid4().int % 20_000_000
        )
        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.completed_ts >= completed_ts
            ).count(),
            0,
        )

        with self.rollback_scope():
            game, sides, players, teams, squads = self._create_p117_ranked_2v2_graph(
                guild_id=guild_id,
                marker=marker,
                completed_ts=completed_ts,
            )
            self.models.Game.load_full_game(game.id).declare_winner(
                winning_side=self.models.GameSide.get_by_id(sides[0].id),
                confirm=True,
            )

            global_enabled = guild_id in self.settings.servers_included_in_global_lb()
            # New 1000-ELO records use the provisional 75-point factor. The
            # retained low-ELO boost adds int(38 * .4) == 15, so equal-side
            # winner/loss deltas are +53 and -23 rather than symmetric.
            winner_global = (53, 1053) if global_enabled else (0, None)
            loser_global = (-23, 977) if global_enabled else (0, None)
            graph = self._p117_elo_graph_snapshot(
                games=(game,), players=players, teams=teams, squads=squads,
            )
            self.assertEqual(
                graph['players'],
                ((1053, 1053, 1053, 1053),) * 2
                + ((977, 1000, 977, 1000),) * 2,
            )
            self.assertEqual(
                graph['members'],
                ((1053, 1053, 1053, 1053),) * 2
                + ((977, 1000, 977, 1000),) * 2
                if global_enabled else ((1000, 1000, 1000, 1000),) * 4,
            )
            self.assertEqual(graph['teams'], ((1016, 1016), (984, 984)))
            self.assertEqual(graph['squads'], (1025, 975))
            self.assertEqual(
                graph['sides'],
                ((16, 1016, 16, 1016, 25), (-16, 984, -16, 984, -25)),
            )
            self.assertEqual(
                graph['lineups'],
                ((53, 1053, 53, 1053, winner_global[0], winner_global[1],
                  winner_global[0], winner_global[1]),) * 2
                + ((-23, 977, -23, 977, loser_global[0], loser_global[1],
                    loser_global[0], loser_global[1]),) * 2,
            )

            self.models.Game.load_full_game(game.id).reverse_elo_changes()
            reversed_graph = self._p117_elo_graph_snapshot(
                games=(game,), players=players, teams=teams, squads=squads,
            )
            self.assertEqual(
                reversed_graph['players'],
                ((1000, 1053, 1000, 1053),) * 2
                + ((1000, 1000, 1000, 1000),) * 2,
            )
            self.assertEqual(
                reversed_graph['members'],
                ((1000, 1053, 1000, 1053),) * 2
                + ((1000, 1000, 1000, 1000),) * 2
                if global_enabled else ((1000, 1000, 1000, 1000),) * 4,
            )
            self.assertEqual(reversed_graph['teams'], ((1000, 1000),) * 2)
            self.assertEqual(reversed_graph['squads'], (1000, 1000))
            self.assertEqual(
                reversed_graph['sides'],
                ((0, None, 0, None, 0),) * 2,
            )
            self.assertEqual(
                reversed_graph['lineups'],
                ((0, None, 0, None, 0, None, 0, None),) * 4,
            )

        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.notes == marker
            ).count(),
            0,
        )

    def test_p117_recalculation_replays_ranked_2v2_graph_deterministically(self):
        """Replay preserves the 2v2 Team/Squad graph as well as player snapshots."""

        guild_id = self.profile.allowed_guild_ids[0]
        marker = f'P11.7-replay-{uuid.uuid4().hex[:10]}'
        completed_ts = datetime.datetime(2503, 1, 1) + datetime.timedelta(
            seconds=uuid.uuid4().int % 20_000_000
        )
        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.completed_ts >= completed_ts
            ).count(),
            0,
        )

        with self.rollback_scope():
            first_game, first_sides, players, teams, squads = (
                self._create_p117_ranked_2v2_graph(
                    guild_id=guild_id,
                    marker=f'{marker}-one',
                    completed_ts=completed_ts,
                )
            )
            second_game = self.models.Game.create(
                guild_id=guild_id,
                host=players[0],
                name=f'{marker}-two',
                notes=marker,
                date=datetime.date(2026, 8, 11),
                completed_ts=completed_ts + datetime.timedelta(seconds=1),
                is_pending=False,
                is_completed=False,
                is_confirmed=False,
                is_ranked=True,
                is_mobile=True,
                size=[2, 2],
            )
            second_sides = []
            for first_side in first_sides:
                side = self.models.GameSide.create(
                    game=second_game,
                    team=first_side.team,
                    squad=first_side.squad,
                    position=first_side.position,
                    sidename=f'{marker}-two-{first_side.position}',
                    size=2,
                )
                second_sides.append(side)
                for first_lineup in first_side.lineup:
                    self.models.Lineup.create(
                        game=second_game,
                        gameside=side,
                        player=first_lineup.player,
                    )

            self.models.Game.load_full_game(first_game.id).declare_winner(
                winning_side=self.models.GameSide.get_by_id(first_sides[0].id),
                confirm=True,
            )
            self.models.Game.load_full_game(second_game.id).declare_winner(
                winning_side=self.models.GameSide.get_by_id(second_sides[1].id),
                confirm=True,
            )

            global_enabled = guild_id in self.settings.servers_included_in_global_lb()
            # The first game applies the 1000-ELO +15/-15 boost to the
            # provisional +/-38 bases.  The second uses the retained
            # low-ELO boost at 1053 and 977: -46 + 13 == -33, and
            # +46 + 20 == +66.
            expected_member_rows = (
                ((1020, 1053, 1020, 1053),) * 2
                + ((1043, 1043, 1043, 1043),) * 2
                if global_enabled else ((1000, 1000, 1000, 1000),) * 4
            )
            expected_graph = {
                'players': ((1020, 1053, 1020, 1053),) * 2
                + ((1043, 1043, 1043, 1043),) * 2,
                'members': expected_member_rows,
                'teams': ((999, 999), (1001, 1001)),
                'squads': (996, 1004),
                'sides': (
                    (16, 1016, 16, 1016, 25),
                    (-16, 984, -16, 984, -25),
                    (-17, 999, -17, 999, -29),
                    (17, 1001, 17, 1001, 29),
                ),
                'lineups': (
                    (53, 1053, 53, 1053, 53, 1053, 53, 1053)
                    if global_enabled else (53, 1053, 53, 1053, 0, None, 0, None),
                ) * 2 + (
                    (-23, 977, -23, 977, -23, 977, -23, 977)
                    if global_enabled else (-23, 977, -23, 977, 0, None, 0, None),
                ) * 2 + (
                    (-33, 1020, -33, 1020, -33, 1020, -33, 1020)
                    if global_enabled else (-33, 1020, -33, 1020, 0, None, 0, None),
                ) * 2 + (
                    (66, 1043, 66, 1043, 66, 1043, 66, 1043)
                    if global_enabled else (66, 1043, 66, 1043, 0, None, 0, None),
                ) * 2,
            }
            before_replay = self._p117_elo_graph_snapshot(
                games=(first_game, second_game),
                players=players,
                teams=teams,
                squads=squads,
            )
            self.assertEqual(before_replay, expected_graph)
            self.models.Game.recalculate_elo_since(completed_ts)
            self.assertEqual(
                self._p117_elo_graph_snapshot(
                    games=(first_game, second_game),
                    players=players,
                    teams=teams,
                    squads=squads,
                ),
                expected_graph,
            )

        self.assertEqual(
            self.models.Game.select().where(
                self.models.Game.notes.contains(marker)
            ).count(),
            0,
        )

    def test_p97f_champion_plan_and_audit_use_real_schema(self):
        """Exercise champion discovery and audit on development PostgreSQL."""

        from modules import champion_role_workers

        guild_id = self.settings.server_ids['polychampions']
        marker = f'P9.7f-{uuid.uuid4().hex[:10]}'
        request = champion_role_workers.ChampionRoleRequest(
            guild_ids=(guild_id,),
            date_cutoff=self.settings.date_cutoff,
        )

        try:
            self.models.db.close()
            plan = asyncio.run(
                champion_role_workers.run_load_champion_role_plan(request)
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(len(plan.guilds), 1)
            self.assertEqual(plan.guilds[0].guild_id, guild_id)
            self.assertTrue(
                plan.global_champion_discord_id is None
                or isinstance(plan.global_champion_discord_id, int)
            )
            self.assertTrue(
                plan.guilds[0].local_champion_discord_id is None
                or isinstance(
                    plan.guilds[0].local_champion_discord_id,
                    int,
                )
            )

            result = asyncio.run(
                champion_role_workers.run_record_champion_role_audit(
                    champion_role_workers.ChampionAuditRequest(
                        guild_id=guild_id,
                        messages=(marker,),
                    )
                )
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(result.message, marker)
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message == marker
                ).count(),
                1,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            self.models.GameLog.delete().where(
                self.models.GameLog.message == marker
            ).execute()

    def test_p97g_completed_channel_discovery_and_reconciliation_real_schema(self):
        """Exercise completed-channel planning and exact reference clearing."""

        from modules import completed_game_channel_purge_workers as workers

        guild_id = self.settings.server_ids['polychampions']
        suffix = uuid.uuid4().hex[:10]
        marker = f'P9.7g-{suffix}'
        id_base = 9_700_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        now = datetime.datetime.now()
        game_ids = []

        def make_game(label, *, age, season=None, notes=None):
            game = self.models.Game.create(
                guild_id=guild_id,
                name=f'{marker}-{label}',
                notes=notes or marker,
                is_pending=False,
                is_completed=True,
                is_confirmed=True,
                is_ranked=False,
                is_mobile=True,
                completed_ts=now - datetime.timedelta(days=age),
                league_season=season,
                league_tier=1 if season else None,
                size=[2],
                game_chan=id_base + len(game_ids) * 10,
            )
            game_ids.append(int(game.id))
            return game

        try:
            eligible = make_game('eligible', age=2)
            side = self.models.GameSide.create(
                game=eligible,
                position=1,
                sidename='Alpha',
                size=2,
                team_chan=id_base + 1,
                team_chan_external_server=guild_id + 1,
            )
            too_recent = make_game('recent', age=0.25)
            too_old = make_game('old', age=15)
            season = make_game('season', age=2, season=99)
            nova = make_game(
                'nova',
                age=2,
                notes=f'{marker} Nova Red Nova Blue',
            )

            self.models.db.close()
            discovered = asyncio.run(
                workers.run_discover_completed_game_channels(
                    workers.CompletedPurgeDiscoveryRequest((guild_id,), now)
                )
            )
            self.assertTrue(self.models.db.is_closed())
            discovered_ids = tuple(plan.game_id for plan in discovered.plans)
            self.assertIn(int(eligible.id), discovered_ids)
            self.assertNotIn(int(too_recent.id), discovered_ids)
            self.assertNotIn(int(too_old.id), discovered_ids)
            self.assertNotIn(int(season.id), discovered_ids)
            self.assertNotIn(int(nova.id), discovered_ids)
            eligible_plan = next(
                plan for plan in discovered.plans
                if plan.game_id == int(eligible.id)
            )
            self.assertEqual(
                tuple((target.kind, target.channel_id)
                      for target in eligible_plan.targets),
                (
                    (workers.SIDE_TARGET, id_base + 1),
                    (workers.GAME_TARGET, id_base),
                ),
            )
            central_target = next(
                target for target in eligible_plan.targets
                if target.kind == workers.GAME_TARGET
            )
            side_target = next(
                target for target in eligible_plan.targets
                if target.kind == workers.SIDE_TARGET
            )

            with mock.patch.object(
                workers.models.Game,
                'save',
                side_effect=peewee.OperationalError(
                    'P9.7g injected reconcile failure'
                ),
            ):
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected reconcile failure',
                ):
                    asyncio.run(workers.run_reconcile_deleted_channel(
                        workers.CompletedChannelReconcileRequest(
                            int(eligible.id), guild_id, central_target,
                        )
                    ))
            self.assertTrue(self.models.db.is_closed())
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                int(self.models.Game.get_by_id(eligible.id).game_chan),
                id_base,
            )

            self.models.db.close()
            central_result = asyncio.run(
                workers.run_reconcile_deleted_channel(
                    workers.CompletedChannelReconcileRequest(
                        int(eligible.id), guild_id, central_target,
                    )
                )
            )
            side_result = asyncio.run(
                workers.run_reconcile_deleted_channel(
                    workers.CompletedChannelReconcileRequest(
                        int(eligible.id), guild_id, side_target,
                    )
                )
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(central_result.status, workers.RECONCILED)
            self.assertEqual(side_result.status, workers.RECONCILED)
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNone(
                self.models.Game.get_by_id(eligible.id).game_chan
            )
            self.assertIsNone(
                self.models.GameSide.get_by_id(side.id).team_chan
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            if game_ids:
                self.models.GameSide.delete().where(
                    self.models.GameSide.game.in_(tuple(game_ids))
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id.in_(tuple(game_ids))
                ).execute()

    def test_p99_manual_channel_preview_and_reconciliation_real_schema(self):
        """Exercise exact owner preview, protection, audit, and rollback."""

        from modules import operator_channel_purge_workers as workers

        guild_id = self.profile.allowed_guild_ids[0]
        suffix = uuid.uuid4().hex[:10]
        marker = f'P9.9-{suffix}'
        id_base = 9_900_000_000_000_000 + (
            uuid.uuid4().int % 1_000_000
        )
        now = datetime.datetime.now(datetime.UTC)
        old = now - datetime.timedelta(days=31)
        game_ids = []

        def make_game(label, *, channel_id, completed=False, season=None):
            game = self.models.Game.create(
                guild_id=guild_id,
                name=f'{marker}-{label}',
                notes=marker,
                is_pending=False,
                is_completed=completed,
                is_confirmed=completed,
                is_ranked=False,
                is_mobile=True,
                completed_ts=(
                    now.replace(tzinfo=None) - datetime.timedelta(days=2)
                    if completed else None
                ),
                league_season=season,
                league_tier=1 if season else None,
                size=[2],
                game_chan=channel_id,
            )
            game_ids.append(int(game.id))
            return game

        def channel(channel_id, *, recent=False):
            return workers.ChannelSnapshot(
                channel_id=channel_id,
                name=f'p99-{channel_id}',
                category_id=50,
                category_name='Games',
                last_message_id=channel_id,
                last_activity_at=(now if recent else old),
                manageable=True,
                archive_protected=False,
            )

        try:
            eligible = make_game('eligible', channel_id=id_base)
            side = self.models.GameSide.create(
                game=eligible,
                position=1,
                sidename='Alpha',
                size=2,
                team_chan=id_base + 1,
            )
            completed = make_game(
                'completed', channel_id=id_base + 10, completed=True,
            )
            season = make_game(
                'season', channel_id=id_base + 20, season=99,
            )
            recent = make_game('recent', channel_id=id_base + 30)
            missing = make_game('missing', channel_id=id_base + 40)
            channels = (
                channel(id_base),
                channel(id_base + 1),
                channel(id_base + 2),
                channel(id_base + 10),
                channel(id_base + 20),
                channel(id_base + 30, recent=True),
            )

            def preview_request(mode):
                return workers.ManualPurgePreviewRequest(
                    guild_id=guild_id,
                    requester_id=int(self.settings.owner_id),
                    mode=mode,
                    as_of=now,
                    guild_channel_count=430,
                    configured_category_ids=(50,),
                    channels=channels,
                )

            self.models.db.close()
            stale = asyncio.run(workers.run_load_manual_purge_preview(
                preview_request(workers.STALE)
            ))
            orphan = asyncio.run(workers.run_load_manual_purge_preview(
                preview_request(workers.ORPHAN)
            ))
            capacity = asyncio.run(workers.run_load_manual_purge_preview(
                preview_request(workers.CAPACITY)
            ))
            missing_preview = asyncio.run(
                workers.run_load_manual_purge_preview(
                    preview_request(workers.MISSING)
                )
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(
                {row.channel_id for row in stale.candidates},
                {id_base, id_base + 1},
            )
            self.assertEqual(
                {row.channel_id for row in orphan.candidates},
                {id_base + 2},
            )
            self.assertEqual(
                {row.channel_id for row in capacity.candidates},
                {id_base, id_base + 30},
            )
            self.assertEqual(
                {row.channel_id for row in missing_preview.candidates},
                {id_base + 40},
            )
            self.assertNotIn(
                int(completed.game_chan),
                {row.channel_id for row in stale.candidates},
            )
            self.assertNotIn(
                int(season.game_chan),
                {row.channel_id for row in stale.candidates},
            )
            self.assertNotIn(
                int(recent.game_chan),
                {row.channel_id for row in stale.candidates},
            )

            target = next(
                row for row in stale.candidates if row.channel_id == id_base
            )
            authorized = asyncio.run(
                workers.run_authorize_manual_purge_candidate(
                    workers.ManualPurgeAuthorizationRequest(
                        guild_id,
                        int(self.settings.owner_id),
                        target,
                        now,
                    )
                )
            )
            self.assertTrue(authorized)

            request = workers.ManualPurgeReconcileRequest(
                guild_id,
                int(self.settings.owner_id),
                f'P9.9 gate (`{self.settings.owner_id}`)',
                target,
            )
            with mock.patch.object(
                workers.models.Game,
                'save',
                side_effect=peewee.OperationalError(
                    'P9.9 injected reconcile failure'
                ),
            ):
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'injected reconcile failure',
                ):
                    asyncio.run(workers.run_reconcile_manual_purge(request))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                int(self.models.Game.get_by_id(eligible.id).game_chan),
                id_base,
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.startswith(
                        f'__{eligible.id}__'
                    )
                ).count(),
                0,
            )

            self.models.db.close()
            result = asyncio.run(
                workers.run_reconcile_manual_purge(request)
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(result.status, workers.RECONCILED)
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNone(
                self.models.Game.get_by_id(eligible.id).game_chan
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.startswith(
                        f'__{eligible.id}__'
                    )
                    & (self.models.GameLog.is_protected == True)
                ).count(),
                1,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            for game_id in game_ids:
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.startswith(f'__{game_id}__')
                ).execute()
            if game_ids:
                self.models.GameSide.delete().where(
                    self.models.GameSide.game.in_(tuple(game_ids))
                ).execute()
                self.models.Game.delete().where(
                    self.models.Game.id.in_(tuple(game_ids))
                ).execute()

    def test_p97e_auto_confirmation_revalidates_real_schema(self):
        """Exercise discovery and stale revalidation on development DB."""

        from modules import auto_confirmation_workers, elo_workers

        guild_id = self.settings.server_ids['polychampions']
        suffix = uuid.uuid4().hex[:10]
        marker = f'P9.7e-{suffix}'
        id_base = 8_500_000_000_000_000_000 + (
            uuid.uuid4().int % 100_000_000
        )
        member_ids = []
        player_ids = []
        game_id = None

        try:
            for index, label in enumerate(('Alpha', 'Bravo')):
                member = self.models.DiscordMember.create(
                    discord_id=id_base + index,
                    name=f'{marker}-{label}',
                    polytopia_name=f'{marker}{label}',
                )
                player = self.models.Player.create(
                    discord_member=member,
                    guild_id=guild_id,
                    name=member.name,
                )
                member_ids.append(member.id)
                player_ids.append(player.id)

            game = self.models.Game.create(
                guild_id=guild_id,
                host=player_ids[0],
                notes=marker,
                is_pending=False,
                is_completed=True,
                is_confirmed=False,
                is_ranked=False,
                is_mobile=True,
                size=[1, 1],
                win_claimed_ts=datetime.datetime(2000, 1, 1),
            )
            game_id = game.id
            sides = []
            for position, player_id in enumerate(player_ids, start=1):
                side = self.models.GameSide.create(
                    game=game,
                    position=position,
                    sidename=f'Side {position}',
                    size=1,
                )
                sides.append(side)
                self.models.Lineup.create(
                    game=game,
                    gameside=side,
                    player=player_id,
                )
            game.winner = sides[0]
            game.save()

            policy = auto_confirmation_workers.AutoConfirmationPolicy(
                as_of=datetime.datetime.now()
            )
            self.models.db.close()
            batch = asyncio.run(
                auto_confirmation_workers.run_discover_auto_confirmations(
                    auto_confirmation_workers
                    .AutoConfirmationDiscoveryRequest(
                        guild_id=guild_id,
                        policy=policy,
                    )
                )
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertIn(
                game_id,
                tuple(candidate.game_id for candidate in batch.candidates),
            )

            self.models.db.connect(reuse_if_open=True)
            self.models.Game.update(
                win_claimed_ts=policy.as_of,
            ).where(self.models.Game.id == game_id).execute()
            log_count = self.models.GameLog.select().where(
                self.models.GameLog.message.contains(marker)
            ).count()
            self.models.db.close()
            with self.assertRaises(elo_workers.AutoConfirmationIneligible):
                elo_workers.confirm_game(
                    game_id,
                    guild_id,
                    marker,
                    auto_policy=policy,
                )

            self.models.db.connect(reuse_if_open=True)
            stale = self.models.Game.get_by_id(game_id)
            self.assertFalse(stale.is_confirmed)
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                log_count,
            )
            self.models.GameSide.update(win_confirmed=True).where(
                self.models.GameSide.game == game_id
            ).execute()
            self.models.db.close()
            result = elo_workers.confirm_game(
                game_id,
                guild_id,
                marker,
                auto_policy=policy,
            )

            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(
                result.auto_confirmation.reason,
                'Due to partial confirmations.',
            )
            self.assertEqual(result.auto_confirmation.confirmed_count, 2)
            self.models.db.connect(reuse_if_open=True)
            self.assertTrue(self.models.Game.get_by_id(game_id).is_confirmed)
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                if game_id is not None:
                    self.models.Game.update(winner=None).where(
                        self.models.Game.id == game_id
                    ).execute()
                    self.models.Lineup.delete().where(
                        self.models.Lineup.game == game_id
                    ).execute()
                    self.models.GameSide.delete().where(
                        self.models.GameSide.game == game_id
                    ).execute()
                    self.models.Game.delete().where(
                        self.models.Game.id == game_id
                    ).execute()
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids or (-1,))
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(member_ids or (-1,))
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(marker)
                ).execute()

    def test_p97d_rank_unstart_snapshots_use_real_schema(self):
        """Exercise correction snapshots on development PostgreSQL."""

        from modules import game_workers

        guild_id = self.settings.server_ids['polychampions']
        suffix = uuid.uuid4().hex[:10]
        marker = f'P9.7d-{suffix}'
        id_base = 8_700_000_000_000_000_000 + (
            uuid.uuid4().int % 100_000_000
        )
        member_ids = []
        player_ids = []
        game_id = None

        try:
            for index, label in enumerate(('Alpha', 'Bravo')):
                member = self.models.DiscordMember.create(
                    discord_id=id_base + index,
                    name=f'{marker}-{label}',
                    polytopia_name=f'{marker}{label}',
                )
                player = self.models.Player.create(
                    discord_member=member,
                    guild_id=guild_id,
                    name=member.name,
                )
                member_ids.append(member.id)
                player_ids.append(player.id)

            game = self.models.Game.create(
                guild_id=guild_id,
                host=player_ids[0],
                name=marker,
                notes=marker,
                is_pending=False,
                is_completed=False,
                is_confirmed=False,
                is_ranked=False,
                is_mobile=True,
                size=[1, 1],
            )
            game_id = game.id
            for position, player_id in enumerate(player_ids, start=1):
                side = self.models.GameSide.create(
                    game=game,
                    position=position,
                    sidename=f'Side {position}',
                    size=1,
                )
                self.models.Lineup.create(
                    game=game,
                    gameside=side,
                    player=player_id,
                )

            self.models.db.close()
            ranked = game_workers.set_game_ranked_state(
                game_id,
                guild_id,
                True,
                marker,
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertTrue(ranked.is_ranked)
            self.assertIsNotNone(ranked.publication)
            self.assertTrue(ranked.publication.game.is_ranked)
            self.assertEqual(
                ranked.publication.roster_mentions,
                tuple(f'<@{id_base + index}>' for index in range(2)),
            )

            unstarted = game_workers.unstart_game(
                game_id,
                guild_id,
                marker,
                '$unstart',
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertIsNotNone(unstarted.publication)
            self.assertTrue(unstarted.publication.game.is_pending)
            self.assertEqual(
                unstarted.mentions,
                tuple(f'<@{id_base + index}>' for index in range(2)),
            )

            self.models.db.connect(reuse_if_open=True)
            restored = self.models.Game.get_by_id(game_id)
            self.assertTrue(restored.is_pending)
            self.assertTrue(restored.is_ranked)
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                if game_id is not None:
                    self.models.Lineup.delete().where(
                        self.models.Lineup.game == game_id
                    ).execute()
                    self.models.GameSide.delete().where(
                        self.models.GameSide.game == game_id
                    ).execute()
                    self.models.Game.delete().where(
                        self.models.Game.id == game_id
                    ).execute()
                self.models.Player.delete().where(
                    self.models.Player.id.in_(player_ids or (-1,))
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(member_ids or (-1,))
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(marker)
                ).execute()

    def test_operator_player_migration_commits_dependencies_and_rolls_back(self):
        """Exercise P9.4's complete graph on real development PostgreSQL."""

        from modules import operator_player_migration_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        marker = f'P9.4-{uuid.uuid4().hex}'
        numeric = int('8' + uuid.uuid4().hex[:17], 16) % 8_000_000_000_000_000_000
        base_discord_id = max(numeric, 1_000_000_000_000_000_000)
        created_game_ids = []
        created_member_ids = []
        created_squad_ids = []
        created_house_ids = []
        created_auction_ids = []

        def make_graph(offset):
            source_id = base_discord_id + offset
            destination_id = base_discord_id + offset + 1
            source = self.models.DiscordMember.create(
                discord_id=source_id,
                name=f'{marker}-source-{offset}',
                polytopia_name=f'{marker}-canonical',
            )
            destination = self.models.DiscordMember.create(
                discord_id=destination_id,
                name=f'{marker}-destination-{offset}',
                timezone_offset_minutes=60,
            )
            created_member_ids.extend((source.id, destination.id))
            source_player = self.models.Player.create(
                discord_member=source,
                guild_id=guild_id,
                name=source.name,
            )
            destination_player = self.models.Player.create(
                discord_member=destination,
                guild_id=guild_id,
                name=destination.name,
                nick='Target Nick',
            )
            other_guild_player = self.models.Player.create(
                discord_member=destination,
                guild_id=guild_id + 1,
                name=destination.name,
            )
            game = self.models.Game.create(
                name=f'{marker} game {offset}',
                guild_id=guild_id,
                host=destination_player,
                size=[1, 1],
                is_completed=False,
                is_pending=False,
            )
            created_game_ids.append(game.id)
            side = self.models.GameSide.create(
                game=game,
                size=1,
                position=1,
            )
            lineup = self.models.Lineup.create(
                game=game,
                gameside=side,
                player=destination_player,
            )
            squad = self.models.Squad.create(
                guild_id=guild_id,
                name=f'{marker}-squad-{offset}',
            )
            created_squad_ids.append(squad.id)
            self.models.SquadMember.create(player=source_player, squad=squad)
            self.models.SquadMember.create(player=destination_player, squad=squad)
            house = self.models.House.create(name=f'{marker}-house-{offset}')
            created_house_ids.append(house.id)
            self.models.PlayerHousePreference.create(player=source_player, house=house)
            self.models.PlayerHousePreference.create(player=destination_player, house=house)
            auction = self.models.Auction.create()
            created_auction_ids.append(auction.id)
            bid = self.models.Bid.create(
                auction=auction,
                amount=1,
                player=destination_player,
                bidder=destination_player,
                house=house,
            )
            return SimpleNamespace(
                source_id=source_id,
                destination_id=destination_id,
                source_member_id=source.id,
                destination_member_id=destination.id,
                source_player_id=source_player.id,
                destination_player_id=destination_player.id,
                other_guild_player_id=other_guild_player.id,
                game_id=game.id,
                lineup_id=lineup.id,
                squad_id=squad.id,
                house_id=house.id,
                bid_id=bid.id,
            )

        def preview_request(graph):
            return workers.PlayerMigrationPreviewRequest(
                guild_id=guild_id,
                requester_id=int(self.settings.owner_id),
                source_id=graph.source_id,
                destination_id=graph.destination_id,
                destination_name=f'{marker}-new-name',
            )

        def commit_request(graph, preview):
            return workers.PlayerMigrationCommitRequest(
                guild_id=guild_id,
                requester_id=int(self.settings.owner_id),
                requester_description=marker,
                source_id=graph.source_id,
                destination_id=graph.destination_id,
                destination_name=f'{marker}-new-name',
                expected_fingerprint=preview.fingerprint,
            )

        try:
            committed = make_graph(10)
            self.models.db.close()
            committed_preview = asyncio.run(
                workers.run_preview(preview_request(committed))
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(committed_preview.blockers, ())
            self.assertIn(
                'canonical timezone',
                committed_preview.destination_metadata,
            )
            result = asyncio.run(workers.run_commit(
                commit_request(committed, committed_preview)
            ))
            self.assertTrue(self.models.db.is_closed())
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(result.players_merged, 1)
            self.assertEqual(result.players_reparented, 1)
            self.assertEqual(result.lineups_reassigned, 1)
            self.assertEqual(result.hosts_reassigned, 1)
            self.assertEqual(result.squad_memberships_deduplicated, 1)
            self.assertEqual(result.house_preferences_deduplicated, 1)
            self.assertEqual(result.bids_reassigned, 2)
            self.assertIsNone(self.models.DiscordMember.get_or_none(
                id=committed.destination_member_id
            ))
            surviving = self.models.DiscordMember.get_by_id(
                committed.source_member_id
            )
            self.assertEqual(surviving.discord_id, committed.destination_id)
            self.assertEqual(surviving.polytopia_name, f'{marker}-canonical')
            self.assertEqual(
                self.models.Lineup.get_by_id(committed.lineup_id).player_id,
                committed.source_player_id,
            )
            self.assertEqual(
                self.models.Game.get_by_id(committed.game_id).host_id,
                committed.source_player_id,
            )
            self.assertEqual(
                self.models.Player.get_by_id(
                    committed.other_guild_player_id
                ).discord_member_id,
                committed.source_member_id,
            )
            self.assertEqual(
                self.models.SquadMember.select().where(
                    self.models.SquadMember.squad == committed.squad_id
                ).count(),
                1,
            )
            self.assertEqual(
                self.models.PlayerHousePreference.select().where(
                    self.models.PlayerHousePreference.house == committed.house_id
                ).count(),
                1,
            )
            bid = self.models.Bid.get_by_id(committed.bid_id)
            self.assertEqual(
                (bid.player_id, bid.bidder_id),
                (committed.source_player_id, committed.source_player_id),
            )
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

            rolled_back = make_graph(20)
            self.models.db.close()
            rollback_preview = asyncio.run(
                workers.run_preview(preview_request(rolled_back))
            )
            with mock.patch.object(
                workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P9.4 forced rollback'),
            ):
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'forced rollback',
                ):
                    asyncio.run(workers.run_commit(
                        commit_request(rolled_back, rollback_preview)
                    ))
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(
                self.models.DiscordMember.get_by_id(
                    rolled_back.source_member_id
                ).discord_id,
                rolled_back.source_id,
            )
            self.assertIsNotNone(self.models.DiscordMember.get_or_none(
                id=rolled_back.destination_member_id
            ))
            self.assertEqual(
                self.models.Lineup.get_by_id(rolled_back.lineup_id).player_id,
                rolled_back.destination_player_id,
            )
            self.assertEqual(
                self.models.Game.get_by_id(rolled_back.game_id).host_id,
                rolled_back.destination_player_id,
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                if created_game_ids:
                    self.models.Game.delete().where(
                        self.models.Game.id.in_(created_game_ids)
                    ).execute()
                self.models.Bid.delete().where(
                    self.models.Bid.auction.in_(created_auction_ids or (-1,))
                ).execute()
                self.models.SquadMember.delete().where(
                    self.models.SquadMember.squad.in_(created_squad_ids or (-1,))
                ).execute()
                self.models.PlayerHousePreference.delete().where(
                    self.models.PlayerHousePreference.house.in_(created_house_ids or (-1,))
                ).execute()
                self.models.Squad.delete().where(
                    self.models.Squad.id.in_(created_squad_ids or (-1,))
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(created_member_ids or (-1,))
                ).execute()
                self.models.Auction.delete().where(
                    self.models.Auction.id.in_(created_auction_ids or (-1,))
                ).execute()
                self.models.House.delete().where(
                    self.models.House.id.in_(created_house_ids or (-1,))
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(marker)
                ).execute()

    def test_operator_player_deletion_commits_blocks_and_rolls_back(self):
        """Exercise P9.5's complete orphan graph on development PostgreSQL."""

        from modules import operator_player_deletion_workers as workers

        guild_id = int(self.profile.allowed_guild_ids[0])
        marker = f'P9.5-{uuid.uuid4().hex}'
        numeric = int('7' + uuid.uuid4().hex[:17], 16) % 8_000_000_000_000_000_000
        base_discord_id = max(numeric, 1_000_000_000_000_000_000)
        created_member_ids = []
        created_squad_ids = []
        created_house_ids = []
        created_game_ids = []
        created_auction_ids = []
        created_application_ids = []

        def make_graph(offset, *, blocked=False):
            member = self.models.DiscordMember.create(
                discord_id=base_discord_id + offset,
                name=f'{marker}-member-{offset}',
                polytopia_name=f'{marker}-canonical',
                elo_moonrise=1111,
            )
            created_member_ids.append(member.id)
            first = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id,
                name=member.name,
                nick='Orphan Nick',
                elo_moonrise=1050,
            )
            second = self.models.Player.create(
                discord_member=member,
                guild_id=guild_id + 1,
                name=member.name,
            )
            squad = self.models.Squad.create(
                guild_id=guild_id,
                name=f'{marker}-squad-{offset}',
            )
            created_squad_ids.append(squad.id)
            squad_member = self.models.SquadMember.create(
                player=first,
                squad=squad,
            )
            house = self.models.House.create(
                name=f'{marker}-house-{offset}',
            )
            created_house_ids.append(house.id)
            preference = self.models.PlayerHousePreference.create(
                player=second,
                house=house,
            )
            result = SimpleNamespace(
                discord_id=member.discord_id,
                member_id=member.id,
                player_ids=(first.id, second.id),
                squad_member_id=squad_member.id,
                preference_id=preference.id,
            )
            if blocked:
                game = self.models.Game.create(
                    name=f'{marker} blocked game {offset}',
                    guild_id=guild_id,
                    host=first,
                    size=[1, 1],
                    is_completed=False,
                    is_pending=False,
                )
                created_game_ids.append(game.id)
                side = self.models.GameSide.create(
                    game=game,
                    size=1,
                    position=1,
                )
                self.models.Lineup.create(
                    game=game,
                    gameside=side,
                    player=first,
                )
                auction = self.models.Auction.create()
                created_auction_ids.append(auction.id)
                self.models.Bid.create(
                    auction=auction,
                    amount=1,
                    player=first,
                    bidder=first,
                    house=house,
                )
                application = self.models.ApiApplication.create(
                    owner=member,
                    name=f'P95-{offset}',
                )
                created_application_ids.append(application.id)
            return result

        def preview_request(graph):
            return workers.PlayerDeletionPreviewRequest(
                guild_id=guild_id,
                requester_id=int(self.settings.owner_id),
                target_id=graph.discord_id,
            )

        def commit_request(graph, graph_preview):
            return workers.PlayerDeletionCommitRequest(
                guild_id=guild_id,
                requester_id=int(self.settings.owner_id),
                requester_description=marker,
                target_id=graph.discord_id,
                expected_fingerprint=graph_preview.fingerprint,
                confirmation_text=f'DELETE {graph.discord_id}',
            )

        try:
            committed = make_graph(10)
            self.models.db.close()
            committed_preview = asyncio.run(
                workers.run_preview(preview_request(committed))
            )
            self.assertTrue(self.models.db.is_closed())
            self.assertEqual(committed_preview.blockers, ())
            self.assertEqual(committed_preview.player_count, 2)
            self.assertEqual(committed_preview.squad_membership_count, 1)
            self.assertEqual(committed_preview.house_preference_count, 1)
            self.assertTrue(committed_preview.warnings)
            result = asyncio.run(workers.run_commit(
                commit_request(committed, committed_preview)
            ))
            self.assertTrue(self.models.db.is_closed())
            self.models.db.connect(reuse_if_open=True)
            self.assertEqual(result.players_deleted, 2)
            self.assertIsNone(self.models.DiscordMember.get_or_none(
                id=committed.member_id
            ))
            self.assertFalse(self.models.Player.select().where(
                self.models.Player.id.in_(committed.player_ids)
            ).exists())
            self.assertIsNone(self.models.SquadMember.get_or_none(
                id=committed.squad_member_id
            ))
            self.assertIsNone(self.models.PlayerHousePreference.get_or_none(
                id=committed.preference_id
            ))
            self.assertEqual(
                self.models.GameLog.select().where(
                    self.models.GameLog.message.contains(marker)
                ).count(),
                1,
            )

            blocked = make_graph(20, blocked=True)
            self.models.db.close()
            blocked_preview = asyncio.run(
                workers.run_preview(preview_request(blocked))
            )
            blocker_text = ' '.join(blocked_preview.blockers)
            self.assertIn('Lineup', blocker_text)
            self.assertIn('hosted game', blocker_text)
            self.assertIn('bid row', blocker_text)
            self.assertIn('API application', blocker_text)
            with self.assertRaises(workers.PlayerDeletionValidationError):
                asyncio.run(workers.run_commit(
                    commit_request(blocked, blocked_preview)
                ))
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNotNone(self.models.DiscordMember.get_or_none(
                id=blocked.member_id
            ))

            rolled_back = make_graph(30)
            self.models.db.close()
            rollback_preview = asyncio.run(
                workers.run_preview(preview_request(rolled_back))
            )
            with mock.patch.object(
                workers.models.GameLog,
                'write',
                side_effect=peewee.OperationalError('P9.5 forced rollback'),
            ):
                with self.assertRaisesRegex(
                    peewee.OperationalError,
                    'forced rollback',
                ):
                    asyncio.run(workers.run_commit(
                        commit_request(rolled_back, rollback_preview)
                    ))
            self.models.db.connect(reuse_if_open=True)
            self.assertIsNotNone(self.models.DiscordMember.get_or_none(
                id=rolled_back.member_id
            ))
            self.assertEqual(
                self.models.Player.select().where(
                    self.models.Player.id.in_(rolled_back.player_ids)
                ).count(),
                2,
            )
            self.assertIsNotNone(self.models.SquadMember.get_or_none(
                id=rolled_back.squad_member_id
            ))
            self.assertIsNotNone(
                self.models.PlayerHousePreference.get_or_none(
                    id=rolled_back.preference_id
                )
            )
        finally:
            self.models.db.connect(reuse_if_open=True)
            with self.models.db.atomic():
                if created_application_ids:
                    self.models.ApiApplication.delete().where(
                        self.models.ApiApplication.id.in_(created_application_ids)
                    ).execute()
                if created_game_ids:
                    self.models.Game.delete().where(
                        self.models.Game.id.in_(created_game_ids)
                    ).execute()
                self.models.Bid.delete().where(
                    self.models.Bid.auction.in_(created_auction_ids or (-1,))
                ).execute()
                self.models.SquadMember.delete().where(
                    self.models.SquadMember.squad.in_(created_squad_ids or (-1,))
                ).execute()
                self.models.PlayerHousePreference.delete().where(
                    self.models.PlayerHousePreference.house.in_(created_house_ids or (-1,))
                ).execute()
                self.models.Squad.delete().where(
                    self.models.Squad.id.in_(created_squad_ids or (-1,))
                ).execute()
                self.models.DiscordMember.delete().where(
                    self.models.DiscordMember.id.in_(created_member_ids or (-1,))
                ).execute()
                self.models.Auction.delete().where(
                    self.models.Auction.id.in_(created_auction_ids or (-1,))
                ).execute()
                self.models.House.delete().where(
                    self.models.House.id.in_(created_house_ids or (-1,))
                ).execute()
                self.models.GameLog.delete().where(
                    self.models.GameLog.message.contains(marker)
                ).execute()

    def test_beta_lab_persona_seed_commit_publication_and_reconciliation(self):
        """Exercise the staged P9.23c persona fixture on the stopped writer."""

        from dataclasses import replace
        from modules import beta_lab_personas as personas

        guild_id = int(self.profile.allowed_guild_ids[0])
        suffix = uuid.uuid4().hex[:12]
        policy = SimpleNamespace(
            guild_id=guild_id,
            tester_role_id=480905534019731476,
            house_name=f'Beta Lab IT House {suffix}',
            team_name=f'Beta Lab IT Team {suffix}',
            staff_role_name='Beta Lab Staff',
        )
        with tempfile.TemporaryDirectory(prefix='polybot-p923c-') as directory:
            project_root = Path(directory)
            profile = replace(
                self.profile,
                project_root=project_root,
                log_root=project_root / 'logs' / 'development',
            )
            role_state = {
                'schema_version': 1,
                'guild_id': guild_id,
                'team_role_id': 9_230_001,
                'team_role_name': policy.team_name,
                'staff_role_id': 9_230_002,
                'staff_role_name': policy.staff_role_name,
            }
            created_house_id = None
            created_team_id = None
            try:
                personas._write_state(
                    profile,
                    personas.ROLE_STATE_FILENAME,
                    role_state,
                )
                self.models.db.close()
                with mock.patch.object(personas, 'manifest', return_value=policy), \
                        mock.patch.object(
                            personas,
                            '_write_state',
                            side_effect=RuntimeError('P9.23c evidence write failure'),
                        ):
                    with self.assertRaisesRegex(
                        RuntimeError, 'evidence write failure',
                    ):
                        personas.seed_database(profile)

                self.models.db.connect(reuse_if_open=True)
                self.assertEqual(
                    self.models.House.select().where(
                        self.models.House.name == policy.house_name
                    ).count(),
                    0,
                )
                self.assertEqual(
                    self.models.Team.select().where(
                        (self.models.Team.guild_id == guild_id)
                        & (self.models.Team.name == policy.team_name)
                    ).count(),
                    0,
                )

                self.models.db.close()
                with mock.patch.object(personas, 'manifest', return_value=policy), \
                        mock.patch.object(
                            personas,
                            '_publish_database_state',
                            side_effect=personas.BetaLabPersonaError(
                                'P9.23c publication failure'
                            ),
                        ):
                    with self.assertRaisesRegex(
                        personas.BetaLabPersonaError,
                        'publication failure',
                    ):
                        personas.seed_database(profile)

                self.models.db.connect(reuse_if_open=True)
                house = self.models.House.get(
                    self.models.House.name == policy.house_name
                )
                team = self.models.Team.get(
                    (self.models.Team.guild_id == guild_id)
                    & (self.models.Team.name == policy.team_name)
                )
                created_house_id = int(house.id)
                created_team_id = int(team.id)
                self.assertEqual(int(team.house_id), created_house_id)
                self.assertEqual(int(team.league_tier), 1)

                self.models.db.close()
                with mock.patch.object(personas, 'manifest', return_value=policy):
                    blocked = personas.database_status(profile)
                    self.assertFalse(blocked.ready)
                    self.assertIn('Pending', blocked.detail)
                    reconciled = personas.reconcile_pending_database(profile)
                    self.assertTrue(reconciled.ready)
                    self.assertEqual(reconciled.team_id, created_team_id)
                    self.assertEqual(reconciled.house_id, created_house_id)
                    repeated = personas.seed_database(profile)
                    self.assertTrue(repeated.ready)
                    self.assertEqual(repeated.team_id, created_team_id)
            finally:
                self.models.db.connect(reuse_if_open=True)
                with self.models.db.atomic():
                    self.models.Team.delete().where(
                        (self.models.Team.guild_id == guild_id)
                        & (self.models.Team.name == policy.team_name)
                    ).execute()
                    self.models.House.delete().where(
                        self.models.House.name == policy.house_name
                    ).execute()


if __name__ == '__main__':
    unittest.main()
