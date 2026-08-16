"""Small human-oriented Beta Lab tests backed by the shared showcase packs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuickTest:
    key: str
    title: str
    duration: str
    steps: tuple[str, ...]


QUICK_TESTS = (
    QuickTest(
        key='player-profile',
        title='Explore a player profile',
        duration='About 3 minutes',
        steps=(
            'Run `/player show` for yourself and open Analytics. If you do not '
            'have a mirrored profile, choose a beta member who has one instead.',
            'Switch between current-reset and all-time ELO.',
            'Open another registered player and check desktop/mobile layout.',
            'Report any stale control, unclear label, or implausible record.',
        ),
    ),
    QuickTest(
        key='leaderboards',
        title='Exercise leaderboard navigation',
        duration='About 5 minutes',
        steps=(
            'Run `/leaderboard players` and change one Common filter.',
            'Open Advanced filters, then clear or replace the active filter.',
            'Use Next, Previous, and Jump to page.',
            'Run `/leaderboard teams` and confirm historical Teams appear.',
        ),
    ),
    QuickTest(
        key='team-house',
        title='Browse Teams and Houses',
        duration='About 4 minutes',
        steps=(
            'Run `/team show` and choose an active Team offered by autocomplete.',
            'Check roster, ELO, image/graph, and recent activity controls.',
            'Run `/house list`, select a House with Teams, then return to the list.',
            'Report missing roles, stale controls, or confusing empty data.',
        ),
    ),
    QuickTest(
        key='game-discovery',
        title='Find and inspect games',
        duration='About 5 minutes',
        steps=(
            'Run `/game search` with one status or player filter.',
            'Open a result and use the controls appropriate to its state.',
            'Run `/game logs` for a game you can access.',
            'Check that failures stay private and successful reads are clear.',
        ),
    ),
    QuickTest(
        key='league-reads',
        title='Check league workspaces',
        duration='About 5 minutes',
        steps=(
            'Run `/league guide` and follow one current command reference.',
            'Run `/league season` with no season, then choose one season.',
            'Open `/league tokens` without options and select a House.',
            'Report missing context, implausible values, or mobile layout issues.',
        ),
    ),
)
