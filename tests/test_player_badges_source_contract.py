"""Dependency-free source-shape checks for the P12.1 integration surface."""

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(relative):
    return (ROOT / relative).read_text(encoding='utf-8')


class PlayerBadgeSourceContractTests(unittest.TestCase):
    def test_all_changed_python_parses(self):
        paths = (
            'modules/league.py',
            'modules/league_badges.py',
            'modules/league_badges_views.py',
            'modules/league_badges_workers.py',
            'modules/models.py',
            'modules/player_badges_migration.py',
            'modules/player_badges_production_migration.py',
            'modules/player_workers.py',
            'modules/player_views.py',
            'scripts/migrate_player_badges.py',
            'scripts/migrate_player_badges_production.py',
        )
        for path in paths:
            with self.subTest(path=path):
                ast.parse(source(path), filename=path)

    def test_model_and_migration_contract_are_exact(self):
        model = source('modules/models.py')
        migration = source('modules/player_badges_migration.py')
        self.assertIn('badges = ArrayField(', model)
        self.assertIn('default=list,', model)
        self.assertIn("constraints=[SQL('DEFAULT ARRAY[]::TEXT[]')]", model)
        self.assertIn(
            'ADD COLUMN "badges" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]',
            migration,
        )
        self.assertNotIn('from modules import models', migration)

    def test_commands_are_nested_slash_only(self):
        league = source('modules/league.py')
        self.assertIn("name='badge'", league)
        self.assertIn('parent=league_group', league)
        self.assertIn("@league_badge_group.command(\n        name='add'", league)
        self.assertIn("@league_badge_group.command(\n        name='remove'", league)
        for path in (
            'modules/league_badges.py',
            'modules/league_badges_views.py',
            'modules/league_badges_workers.py',
        ):
            self.assertNotIn('@commands.command', source(path))

    def test_private_selector_and_confirmation_are_bounded(self):
        value = source('modules/league_badges_views.py')
        self.assertIn('discord.ui.UserSelect(', value)
        self.assertIn('min_values=1', value)
        self.assertIn('max_values=25', value)
        self.assertIn("label=f'Confirm {self.draft.operation}'", value)
        self.assertIn("label='Cancel'", value)
        self.assertIn('Only the Mod who opened this badge draft', value)
        self.assertIn('on_timeout', value)
        self.assertIn('/league badge {self.draft.operation}', value)

    def test_atomic_worker_has_ordered_locks_explicit_save_and_audit(self):
        value = source('modules/league_badges_workers.py')
        self.assertIn('with models.db.connection_context():', value)
        self.assertIn('with models.db.atomic():', value)
        self.assertIn('.order_by(models.Player.id)', value)
        self.assertIn('query = query.for_update()', value)
        self.assertIn('save(only=[models.Player.badges])', value)
        self.assertIn('models.GameLog.write(', value)
        self.assertIn('if changed_ids:', value)
        self.assertIn('while not future.done():', value)

    def test_publication_is_after_worker_and_failure_says_committed(self):
        league = source('modules/league.py')
        worker_position = league.index(
            'result = await league_badges_workers.run_badge_mutation(request)'
        )
        publication_position = league.index(
            'await league_badges.publish_result(component_interaction, result)'
        )
        self.assertLess(worker_position, publication_position)
        service = source('modules/league_badges.py')
        self.assertIn('The badge transaction committed', service)
        self.assertIn('roles=False', service)
        self.assertIn('everyone=False', service)
        self.assertIn('CUSTOM_EMOJI_BADGE', service)

    def test_profile_is_guild_gated_and_snapshot_paginated(self):
        workers = source('modules/player_workers.py')
        views = source('modules/player_views.py')
        self.assertIn("settings.server_ids['polychampions']", workers)
        self.assertIn('badges: tuple[str, ...] = ()', workers)
        self.assertIn('snapshot.badges[:6]', views)
        self.assertIn('BADGE_PAGE_SIZE = 10', views)
        self.assertIn("('badges', 'Badges')", views)

    def test_operator_destructive_fingerprints_cover_badges(self):
        migration = source('modules/operator_player_migration_workers.py')
        deletion = source('modules/operator_player_deletion_workers.py')
        for value in (migration, deletion):
            self.assertIn("'trophies', 'badges', 'is_banned'", value)
        self.assertIn('_merged_badges(', migration)
        self.assertIn('badge_count=', deletion)

    def test_startup_is_read_only_and_requires_schema_before_model_code(self):
        startup = source('modules/startup_schema_preflight.py')
        bot = source('bot.py')
        self.assertIn('player_badges_migration.schema_metadata(cursor)', startup)
        self.assertIn('missing the required player.badges', startup)
        self.assertNotIn('apply_migration(', bot)
        cli = source('scripts/migrate_player_badges.py')
        self.assertIn('set_session(readonly=True, autocommit=True)', cli)

    def test_excluded_scope_is_absent(self):
        combined = '\n'.join(source(path) for path in (
            'modules/league_badges.py',
            'modules/league_badges_views.py',
            'modules/league_badges_workers.py',
        )).casefold()
        for forbidden in (
            'role import', 'hall of champions', 'gallery', 'ptrophies',
            'external emoji', 'requests.get', 'aiohttp',
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == '__main__':
    unittest.main()
