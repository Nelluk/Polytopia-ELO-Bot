"""Regression coverage for the approved P9.8 prefix retirements."""

import unittest

from tests.test_newgame_worker import import_offline_runtime


administration = import_offline_runtime('modules.administration')
league = import_offline_runtime('modules.league')


RETIRED_PREFIX_NAMES = frozenset(
    {
        'gtest',
        'ptrophies',
        'boost_from',
        'boost_from_norole',
        'purge_game_channels',
        'restart',
        'restart_force',
        'quit',
    }
)

def prefix_names(cog_type):
    return {
        name
        for command in cog_type.__cog_commands__
        for name in (command.name, *command.aliases)
    }


class ApprovedOperatorRetirementTests(unittest.TestCase):
    def test_approved_retirements_are_absent_from_prefix_registry(self):
        registered = prefix_names(administration.administration)
        registered.update(prefix_names(league.league))

        self.assertTrue(RETIRED_PREFIX_NAMES.isdisjoint(registered))

if __name__ == '__main__':
    unittest.main()
