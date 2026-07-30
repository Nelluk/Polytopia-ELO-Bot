#!/usr/bin/env python3
"""Seed, inspect, or clean deterministic beta fixtures in polytopia_dev."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import peewee


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import dev_fixtures
from runtime_config import get_runtime_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Manage clearly owned beta-test games in the isolated '
            'development database.'
        )
    )
    parser.add_argument(
        '--guild',
        type=int,
        help='Development guild ID; defaults when exactly one is configured.',
    )
    parser.add_argument(
        '--manifest',
        type=Path,
        default=dev_fixtures.default_manifest_path(PROJECT_ROOT),
        help='Ignored local ownership/status manifest path.',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    seed = commands.add_parser('seed')
    seed.add_argument(
        '--user',
        type=int,
        action='append',
        required=True,
        help='Existing registered dev-guild Discord user ID; repeat 2/4/6/8 times.',
    )

    commands.add_parser('status')

    cleanup = commands.add_parser('cleanup')
    cleanup.add_argument(
        '--confirm',
        action='store_true',
        help='Required acknowledgement before deleting owned fixture games.',
    )
    return parser


def _guild_id(profile, requested_guild_id: int | None) -> int:
    if requested_guild_id is not None:
        return requested_guild_id
    if len(profile.allowed_guild_ids) != 1:
        raise dev_fixtures.FixtureValidationError(
            'Specify --guild when multiple development guilds are configured.'
        )
    return profile.allowed_guild_ids[0]


def _print_state(state: dev_fixtures.FixtureState) -> None:
    print(f'Development guild: {state.guild_id}')
    print(
        'Referenced users: '
        + (', '.join(str(user_id) for user_id in state.user_ids) or '(none)')
    )
    if not state.games:
        print('Owned fixture games: (none)')
        return
    print('Owned fixture games:')
    for game in state.games:
        print(
            f'  {game.scenario}: game {game.game_id} '
            f'(completed={game.is_completed}, '
            f'confirmed={game.is_confirmed}, ranked={game.is_ranked}, '
            f'pending={game.is_pending}, expiration={game.expiration})'
        )


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = get_runtime_profile()
        dev_fixtures.validate_profile(profile)
        guild_id = _guild_id(profile, args.guild)

        # Import database-backed modules only after the static profile gate.
        from modules import models

        if args.command == 'seed':
            state = dev_fixtures.seed_fixtures(
                profile=profile,
                models_module=models,
                guild_id=guild_id,
                user_ids=args.user,
                manifest_path=args.manifest,
            )
        elif args.command == 'status':
            state = dev_fixtures.fixture_status(
                profile=profile,
                models_module=models,
                guild_id=guild_id,
            )
        else:
            state = dev_fixtures.cleanup_fixtures(
                profile=profile,
                models_module=models,
                guild_id=guild_id,
                manifest_path=args.manifest,
                confirmed=args.confirm,
            )
        _print_state(state)
        return 0
    except (
        dev_fixtures.FixtureSafetyError,
        dev_fixtures.FixtureValidationError,
    ) as exc:
        print(f'Fixture operation refused: {exc}', file=sys.stderr)
        return 2
    except peewee.OperationalError as exc:
        print(f'Development database operation failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
