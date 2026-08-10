"""Regression coverage for the approved P9.8 prefix retirements."""

import unittest

from tests.test_newgame_worker import import_offline_runtime


administration = import_offline_runtime('modules.administration')
league = import_offline_runtime('modules.league')


RETIRED_PREFIX_NAMES = frozenset(
    {'gtest', 'ptrophies', 'boost_from', 'boost_from_norole'}
)

# These H6 commands remain intentionally executable until their separately
# recorded replacement or retirement decisions are implemented.
RETAINED_H6_OPERATOR_DISPOSITIONS = {
    'restart': 'retain until guarded systemd replacement is approved',
    'restart_force': 'retain restart force alias until replacement',
    'quit': 'retain restart alias until replacement',
    'purge_game_channels': 'retain owner-only pending preview-worker decision',
}


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

    def test_retained_h6_operator_commands_have_explicit_dispositions(self):
        self.assertEqual(
            set(RETAINED_H6_OPERATOR_DISPOSITIONS),
            {'restart', 'restart_force', 'quit', 'purge_game_channels'},
        )
        registered = prefix_names(administration.administration)

        self.assertTrue(
            set(RETAINED_H6_OPERATOR_DISPOSITIONS).issubset(registered)
        )


if __name__ == '__main__':
    unittest.main()
