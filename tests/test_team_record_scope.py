"""Regression coverage for the one explicit persistent-Team ownership alias."""

import unittest

from modules import team_record_scope


class PersistentTeamScopeTests(unittest.TestCase):
    def test_only_pcplus_uses_polychampions_team_records(self):
        self.assertEqual(
            team_record_scope.persistent_team_guild_id(
                team_record_scope.PCPLUS_GUILD_ID
            ),
            team_record_scope.POLYCHAMPIONS_GUILD_ID,
        )
        self.assertEqual(
            team_record_scope.persistent_team_guild_id(
                team_record_scope.POLYCHAMPIONS_GUILD_ID
            ),
            team_record_scope.POLYCHAMPIONS_GUILD_ID,
        )
        self.assertEqual(
            team_record_scope.persistent_team_guild_id(123456789),
            123456789,
        )
