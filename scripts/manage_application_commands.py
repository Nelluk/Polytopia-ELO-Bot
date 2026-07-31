#!/usr/bin/env python3
"""Plan and explicitly deploy guild-scoped application commands.

The default mode is an offline plan.  ``inspect`` fetches current commands;
``apply`` is the only mode that mutates Discord.  There is intentionally no
global scope in this tool, and no code path calls ``CommandTree.sync`` without
an explicit guild.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import copy
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable, Iterator, Mapping, Sequence

import discord
from discord import app_commands
from discord.ext import commands


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.application_command_policy import (  # noqa: E402
    ApplicationCommandPolicyError,
    CapabilityPolicy,
    CommandDescriptor,
    GuildCommandPlan,
    plan_application_commands,
    policy_from_server_settings,
)
from runtime_config import (  # noqa: E402
    RuntimeConfigurationError,
    load_runtime_profile,
)


COMMAND_SOURCE_MODULES = (
    'modules.games',
    'modules.customhelp',
    'modules.matchmaking',
    'modules.administration',
    'modules.misc',
    'modules.league',
    'modules.api_cog',
    'modules.antiscam',
    'modules.bullet',
)


class CommandManagementError(RuntimeError):
    """Raised when the explicit deployment workflow cannot proceed safely."""


class _ModelImportPlaceholder:
    """Non-database stand-in used while reading command metadata.

    Command callbacks are not executed by the deployment tool.  The command
    decorators only need model names and the registration-check decorator, so
    a deliberately inert placeholder is sufficient.  This avoids importing
    ``modules.models`` whose historical import side effect opens PostgreSQL
    and creates tables.
    """

    def __init__(self, name: str = 'model-placeholder'):
        self.name = name

    def __call__(self, *_args: Any, **_kwargs: Any) -> '_ModelImportPlaceholder':
        return self

    def __getattr__(self, name: str) -> '_ModelImportPlaceholder':
        if name.startswith('__'):
            raise AttributeError(name)
        return _ModelImportPlaceholder(f'{self.name}.{name}')

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __bool__(self) -> bool:
        return False

    def __eq__(self, _other: Any) -> bool:
        return False

    def __hash__(self) -> int:
        return id(self)


@contextmanager
def _model_free_command_imports() -> Iterator[None]:
    """Load command-bearing cogs without the database-backed model module."""

    unsafe_loaded = {
        name for name in ('modules.models', *COMMAND_SOURCE_MODULES)
        if name in sys.modules
    }
    if unsafe_loaded:
        raise CommandManagementError(
            'Command-source loading must run before the database-backed bot '
            'modules are imported; start a fresh management-process invocation.'
        )

    before = dict(sys.modules)
    modules_package = sys.modules.get('modules')
    before_package_attrs = (
        None if modules_package is None else set(vars(modules_package))
    )
    placeholder = ModuleType('modules.models')
    placeholder.__getattr__ = lambda name: _ModelImportPlaceholder(
        f'models.{name}'
    )
    placeholder.is_registered_member = lambda: (lambda callback: callback)
    placeholder.db = _ModelImportPlaceholder('models.db')
    sys.modules['modules.models'] = placeholder
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name.startswith('modules.') and name not in before:
                sys.modules.pop(name, None)
        for name, module in before.items():
            if sys.modules.get(name) is not module:
                sys.modules[name] = module
        if modules_package is not None and before_package_attrs is not None:
            for attr in tuple(vars(modules_package)):
                if attr not in before_package_attrs:
                    delattr(modules_package, attr)


def _local_command_classes(module: ModuleType) -> Iterable[type[commands.Cog]]:
    for _name, candidate in inspect.getmembers(module, inspect.isclass):
        if (
                candidate.__module__ == module.__name__
                and hasattr(candidate, '__cog_app_commands__')
                and getattr(candidate, '__cog_app_commands__')
        ):
            yield candidate


def load_command_source() -> tuple[commands.Bot, tuple[Any, ...]]:
    """Return the loaded global command templates without opening PostgreSQL."""

    with _model_free_command_imports():
        modules = [importlib.import_module(name) for name in COMMAND_SOURCE_MODULES]
        client = commands.Bot(
            command_prefix='!',
            intents=discord.Intents.none(),
        )
        commands_by_root: dict[str, Any] = {}
        for module in modules:
            for cog_class in _local_command_classes(module):
                for command in cog_class.__cog_app_commands__:
                    if command.name in commands_by_root:
                        raise CommandManagementError(
                            f'Duplicate loaded application-command root: '
                            f'{command.name!r}.'
                        )
                    commands_by_root[command.name] = copy.deepcopy(command)

        for command in commands_by_root.values():
            client.tree.add_command(command)
        return client, tuple(
            client.tree.get_commands()
        )


def _parse_guild_ids(value: str | None, *, option_name: str) -> tuple[int, ...]:
    if value is None or not value.strip():
        raise CommandManagementError(f'{option_name} must not be empty.')
    try:
        guild_ids = tuple(sorted({
            int(part.strip())
            for part in value.split(',')
            if part.strip()
        }))
    except ValueError as exc:
        raise CommandManagementError(
            f'{option_name} must be a comma-separated list of integer guild IDs.'
        ) from exc
    if not guild_ids or any(guild_id <= 0 for guild_id in guild_ids):
        raise CommandManagementError(
            f'{option_name} must contain positive integer guild IDs.'
        )
    return guild_ids


def validate_target_guilds(
        requested_guild_ids: Iterable[int] | None,
        allowed_guild_ids: Iterable[int],
        *,
        require_explicit: bool = False) -> tuple[int, ...]:
    allowed = tuple(sorted(allowed_guild_ids))
    if requested_guild_ids is None:
        if require_explicit:
            raise CommandManagementError(
                'Remote inspection/apply requires an explicit --guild-ids '
                'scope.'
            )
        return allowed
    selected = tuple(sorted(set(requested_guild_ids)))
    unknown = set(selected) - set(allowed)
    if unknown:
        raise CommandManagementError(
            'Requested guild IDs are outside the runtime profile allowlist: '
            + ', '.join(str(guild_id) for guild_id in sorted(unknown))
            + '.'
        )
    return selected


def validate_apply_confirmation(
        *,
        selected_environment: str,
        expected_environment: str,
        selected_guild_ids: Iterable[int],
        confirmed_guild_ids: Iterable[int] | None,
        confirmed_environment: str | None,
        scope: str,
        confirmed_scope: str | None,
        confirmed_no_global_sync: bool) -> None:
    """Require an exact environment, guild set, and guild-only scope."""

    if selected_environment != expected_environment:
        raise CommandManagementError(
            f'Environment confirmation mismatch: selected '
            f'{selected_environment!r}, expected {expected_environment!r}.'
        )
    if confirmed_environment != selected_environment:
        raise CommandManagementError(
            'Apply requires --confirm-environment matching --environment.'
        )
    selected = tuple(sorted(set(selected_guild_ids)))
    confirmed = None if confirmed_guild_ids is None else tuple(
        sorted(set(confirmed_guild_ids))
    )
    if confirmed != selected:
        raise CommandManagementError(
            'Apply requires --confirm-guild-ids matching the exact selected '
            'guild set.'
        )
    if scope != 'guild' or confirmed_scope != 'guild':
        raise CommandManagementError(
            'Only explicit guild scope is supported; global deployment is '
            'disabled.'
        )
    if not confirmed_no_global_sync:
        raise CommandManagementError(
            'Apply requires --confirm-no-global-sync.'
        )


def _plan_json(plans: Sequence[GuildCommandPlan]) -> str:
    return json.dumps([
        {
            'scope': plan.scope,
            'guild_id': plan.guild_id,
            'desired_roots': [item.name for item in plan.desired],
            'current_roots': [item.name for item in plan.current],
            'creates': list(plan.diff.creates),
            'updates': list(plan.diff.updates),
            'unchanged': list(plan.diff.unchanged),
            'removals': list(plan.diff.removals),
        }
        for plan in plans
    ], indent=2, sort_keys=True)


async def fetch_current_commands(
        client: commands.Bot,
        guild_ids: Iterable[int]) -> Mapping[int, Sequence[Any]]:
    current: dict[int, Sequence[Any]] = {}
    for guild_id in guild_ids:
        current[guild_id] = await client.tree.fetch_commands(
            guild=discord.Object(id=guild_id)
        )
    return current


def _prepare_guild_commands(
        client: commands.Bot,
        desired: Iterable[CommandDescriptor],
        guild: discord.Object) -> app_commands.CommandTree:
    """Replace one guild's local commands on the client's existing tree."""

    tree = client.tree
    tree.clear_commands(guild=guild)
    for descriptor in desired:
        if descriptor.command is None:
            raise CommandManagementError(
                f'No loaded command template is available for root '
                f'{descriptor.name!r}.'
            )
        tree.add_command(copy.deepcopy(descriptor.command), guild=guild)
    return tree


async def apply_guild_plans(
        client: commands.Bot,
        plans: Sequence[GuildCommandPlan]) -> Mapping[int, Sequence[Any]]:
    """Apply only changed guild plans and explicitly prune obsolete roots."""

    synced: dict[int, Sequence[Any]] = {}
    for plan in plans:
        if plan.scope != 'guild':
            raise CommandManagementError(
                f'Unsupported deployment scope: {plan.scope!r}.'
            )
        if not plan.diff.has_changes:
            continue
        guild = discord.Object(id=plan.guild_id)
        tree = _prepare_guild_commands(client, plan.desired, guild)
        # This is the only sync call in the tool.  It always has an explicit
        # guild. Replacing only that guild's local definitions causes Discord
        # to prune absent roots without touching globals or another guild.
        synced[plan.guild_id] = await tree.sync(guild=guild)
    return synced


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Plan or explicitly deploy guild-scoped application commands.'
    )
    parser.add_argument(
        '--environment',
        required=True,
        choices=('development', 'production'),
        help='Runtime profile to verify; POLYBOT_ENV must match exactly.',
    )
    parser.add_argument(
        '--mode',
        choices=('plan', 'inspect', 'apply'),
        default='plan',
        help='plan is offline and default-deny; inspect/apply are explicit remote modes.',
    )
    parser.add_argument(
        '--guild-ids',
        help='Comma-separated exact target guild IDs; required for remote modes.',
    )
    parser.add_argument('--confirm-environment')
    parser.add_argument('--confirm-guild-ids')
    parser.add_argument('--confirm-scope')
    parser.add_argument(
        '--confirm-no-global-sync',
        action='store_true',
        help='Required acknowledgment that this tool cannot deploy globally.',
    )
    return parser


async def _remote_mode(
        *,
        mode: str,
        profile: Any,
        source_client: commands.Bot,
        source_commands: Sequence[Any],
        policy: CapabilityPolicy,
        guild_ids: Sequence[int]) -> int:
    await source_client.login(profile.discord_token)
    try:
        current = await fetch_current_commands(source_client, guild_ids)
        plans = plan_application_commands(
            policy,
            source_commands,
            current,
            guild_ids=guild_ids,
            tree=source_client.tree,
        )
        print(_plan_json(plans))
        if mode == 'apply':
            synced = await apply_guild_plans(source_client, plans)
            print(json.dumps({
                'applied_guild_ids': sorted(synced),
                'unchanged_guild_ids': sorted(
                    plan.guild_id for plan in plans
                    if not plan.diff.has_changes
                ),
            }, indent=2, sort_keys=True))
    finally:
        await source_client.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    selected_environment = os.environ.get('POLYBOT_ENV', '').strip()
    if selected_environment != args.environment:
        print(
            'Refusing command management: POLYBOT_ENV must be set to the '
            f'exact requested environment {args.environment!r}.',
            file=sys.stderr,
        )
        return 2

    try:
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        policy = policy_from_server_settings(
            profile.server_settings,
            profile.allowed_guild_ids,
        )
        requested = (
            None if args.guild_ids is None
            else _parse_guild_ids(args.guild_ids, option_name='--guild-ids')
        )
        guild_ids = validate_target_guilds(
            requested,
            profile.allowed_guild_ids,
            require_explicit=args.mode in ('inspect', 'apply'),
        )
        if args.mode == 'apply':
            confirmed = (
                None if args.confirm_guild_ids is None
                else _parse_guild_ids(
                    args.confirm_guild_ids,
                    option_name='--confirm-guild-ids',
                )
            )
            validate_apply_confirmation(
                selected_environment=args.environment,
                expected_environment=profile.environment,
                selected_guild_ids=guild_ids,
                confirmed_guild_ids=confirmed,
                confirmed_environment=args.confirm_environment,
                scope='guild',
                confirmed_scope=args.confirm_scope,
                confirmed_no_global_sync=args.confirm_no_global_sync,
            )

        source_client, source_commands = load_command_source()
        try:
            if args.mode == 'plan':
                plans = plan_application_commands(
                    policy,
                    source_commands,
                    guild_ids=guild_ids,
                    tree=source_client.tree,
                )
                print(_plan_json(plans))
                return 0
            return asyncio.run(_remote_mode(
                mode=args.mode,
                profile=profile,
                source_client=source_client,
                source_commands=source_commands,
                policy=policy,
                guild_ids=guild_ids,
            ))
        except Exception:
            # The remote helper owns close() after login.  Plan-mode clients
            # are closed here so an offline invocation leaves no task behind.
            if args.mode == 'plan':
                asyncio.run(source_client.close())
            raise
    except (ApplicationCommandPolicyError, CommandManagementError,
            RuntimeConfigurationError) as exc:
        print(f'Command-management error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
