"""Explicitly gated integration tests for the development PostgreSQL database."""

import asyncio
from contextlib import contextmanager, nullcontext
import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
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

    def test_model_import_initialized_expected_schema(self):
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
                result = beta_wider_setup.seed_wider_beta_setup(
                    profile=self.profile,
                    manifest=manifest,
                    guild_id=guild_id,
                    database_factory=lambda _profile: self.models.db,
                    # The class gate owns the independently checked DB
                    # identity; this test never probes the durable beta lock.
                    writer_guard=lambda _profile: nullcontext(),
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
                self.assertIsNone(row[3])
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
            )
        ))
        self.assertEqual(result.player_id, player.id)
        self.assertEqual(result.discord_id, player.discord_member.discord_id)
        self.assertIsInstance(result.games, tuple)
        for row in result.games:
            self.assertIsInstance(row, player_workers.PlayerGameRow)

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

        marker = f'P2.1 integration {uuid.uuid4().hex}'
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

        marker = f'P2.1 worker rollback {uuid.uuid4().hex}'
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
        from modules import game_start_workers

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


if __name__ == '__main__':
    unittest.main()
