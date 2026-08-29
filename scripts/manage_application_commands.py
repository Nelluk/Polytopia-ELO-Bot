#!/usr/bin/env python3
"""Plan and explicitly deploy guild-scoped application commands.

The default mode is an offline plan.  ``inspect`` fetches current commands;
``apply`` is the only mode that mutates Discord.  Remote modes inspect the
global tree read-only, but there is intentionally no global mutation scope in
this tool and no code path calls ``CommandTree.sync`` without an explicit
guild.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import copy
from dataclasses import dataclass
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
    build_capability_policy,
    plan_application_commands,
    policy_from_server_settings,
)
from modules import guild_configuration_storage as guild_storage  # noqa: E402
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


MAX_GUILD_CONFIGURATION_PLAN_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class RemoteCommandSnapshot:
    """Read-only remote state used to guard guild-scoped deployment."""

    global_commands: tuple[Any, ...]
    guild_commands: Mapping[int, tuple[Any, ...]]


@dataclass(frozen=True)
class GuildConfigurationPlanPolicy:
    policy: CapabilityPolicy
    bundle_digest: str


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


def _copy_command(command: Any) -> Any:
    """Detach a command while preserving identity-sensitive Discord enums."""

    # discord.py's enum values are identity-sensitive while serializing
    # string ranges (min_length/max_length). A plain deepcopy duplicates those
    # values and incorrectly emits numeric min_value/max_value fields, which
    # Discord discards.
    enum_memo = {
        id(value): value
        for value in discord.AppCommandOptionType
    }
    return copy.deepcopy(command, memo=enum_memo)


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
                    commands_by_root[command.name] = _copy_command(command)

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


def _parse_guild_scope(
    value: str | None,
    *,
    option_name: str,
    allowed_guild_ids: Iterable[int],
) -> tuple[int, ...] | None:
    if value is None:
        return None
    if value.strip().casefold() == 'all':
        return tuple(sorted(allowed_guild_ids))
    return _parse_guild_ids(value, option_name=option_name)


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
    return json.dumps(_plan_values(plans), indent=2, sort_keys=True)


def _plan_values(plans: Sequence[GuildCommandPlan]) -> list[dict[str, Any]]:
    return [
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
    ]


def _remote_plan_json(
        snapshot: RemoteCommandSnapshot,
        plans: Sequence[GuildCommandPlan]) -> str:
    global_roots = sorted(command.name for command in snapshot.global_commands)
    return json.dumps({
        'global': {
            'scope': 'global',
            'current_roots': global_roots,
            'count': len(global_roots),
            'guild_apply_safe': not global_roots,
        },
        'guilds': _plan_values(plans),
    }, indent=2, sort_keys=True)


async def fetch_remote_commands(
        client: commands.Bot,
        guild_ids: Iterable[int]) -> RemoteCommandSnapshot:
    """Fetch global and selected-guild state without synchronizing either."""

    global_commands = tuple(await client.tree.fetch_commands())
    current: dict[int, tuple[Any, ...]] = {}
    for guild_id in guild_ids:
        current[guild_id] = tuple(
            await client.tree.fetch_commands(guild=discord.Object(id=guild_id))
        )
    return RemoteCommandSnapshot(
        global_commands=global_commands,
        guild_commands=current,
    )


def validate_remote_global_commands(commands: Sequence[Any]) -> None:
    """Refuse guild mutation while Discord still exposes global commands."""

    roots = sorted(command.name for command in commands)
    if roots:
        raise CommandManagementError(
            'Remote global application-command tree is nonempty ('
            + ', '.join(roots)
            + '); refusing guild apply. This tool cannot remove or synchronize '
            'global commands.'
        )


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
        help=(
            'Comma-separated exact target guild IDs, or "all" for the exact '
            'runtime allowlist; required for remote modes.'
        ),
    )
    parser.add_argument(
        '--guild-configuration-plan',
        help=(
            'Production-only digest-bound import plan whose active document '
            'capabilities replace legacy static command assignments.'
        ),
    )
    parser.add_argument('--confirm-guild-configuration-plan')
    parser.add_argument('--confirm-environment')
    parser.add_argument('--confirm-guild-ids')
    parser.add_argument('--confirm-scope')
    parser.add_argument(
        '--confirm-no-global-sync',
        action='store_true',
        help='Required acknowledgment that this tool cannot deploy globally.',
    )
    return parser


def policy_from_guild_configuration_plan(
    path_value: str,
    *,
    profile: Any,
) -> GuildConfigurationPlanPolicy:
    if profile.environment != guild_storage.PRODUCTION_ENVIRONMENT:
        raise CommandManagementError(
            'A guild-configuration import plan is production-only.'
        )
    if (
            not isinstance(path_value, str)
            or not path_value
            or '\x00' in path_value
            or Path(path_value).is_absolute()
            or Path(path_value).suffix != '.json'
    ):
        raise CommandManagementError(
            'Guild-configuration plan must be one relative JSON file.'
        )
    relative = Path(path_value)
    directory = Path('logs/production/guild-configuration')
    try:
        relative.relative_to(directory)
    except ValueError as exc:
        raise CommandManagementError(
            'Guild-configuration plan must remain in '
            'logs/production/guild-configuration.'
        ) from exc
    if any(part in {'', '.', '..'} for part in relative.parts):
        raise CommandManagementError('Guild-configuration plan path is unsafe.')
    path = (PROJECT_ROOT / relative).resolve(strict=False)
    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise CommandManagementError(
            'Guild-configuration plan must remain inside the checkout.'
        ) from exc
    current = PROJECT_ROOT
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CommandManagementError(
                'Guild-configuration plan path may not traverse a symlink.'
            )
    if not path.is_file():
        raise CommandManagementError(
            f'Guild-configuration plan does not exist: {relative}'
        )
    payload = path.read_bytes()
    if len(payload) > MAX_GUILD_CONFIGURATION_PLAN_BYTES:
        raise CommandManagementError(
            'Guild-configuration plan exceeds its byte bound.'
        )
    try:
        mapping = json.loads(payload.decode('utf-8'))
        target = guild_storage.StorageTarget(
            environment=profile.environment,
            database_name=profile.database_name,
            database_user=profile.database_user,
            expected_application_id=profile.expected_bot_id,
            background_tasks_enabled=profile.background_tasks_enabled,
            api_enabled=profile.api_enabled,
            bullet_enabled=profile.bullet_enabled,
        )
        bundle = guild_storage.bundle_from_mapping(mapping, target=target)
    except (
        UnicodeError,
        json.JSONDecodeError,
        guild_storage.GuildConfigurationStorageError,
    ) as exc:
        raise CommandManagementError(
            'Guild-configuration plan is invalid or does not match production.'
        ) from exc
    allowed = tuple(sorted(int(value) for value in profile.allowed_guild_ids))
    if tuple(item.guild_id for item in bundle.imports) != allowed:
        raise CommandManagementError(
            'Guild-configuration plan does not match the runtime allowlist.'
        )
    return GuildConfigurationPlanPolicy(
        policy=build_capability_policy(
            {
                item.guild_id: item.document.command_capabilities
                for item in bundle.imports
            },
            allowed,
        ),
        bundle_digest=bundle.bundle_digest,
    )


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
        snapshot = await fetch_remote_commands(source_client, guild_ids)
        plans = plan_application_commands(
            policy,
            source_commands,
            snapshot.guild_commands,
            guild_ids=guild_ids,
            tree=source_client.tree,
        )
        print(_remote_plan_json(snapshot, plans))
        if mode == 'apply':
            validate_remote_global_commands(snapshot.global_commands)
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
        loaded_plan = None
        if args.guild_configuration_plan is not None:
            loaded_plan = policy_from_guild_configuration_plan(
                args.guild_configuration_plan,
                profile=profile,
            )
            policy = loaded_plan.policy
        else:
            policy = policy_from_server_settings(
                profile.server_settings,
                profile.allowed_guild_ids,
            )
        requested = _parse_guild_scope(
            args.guild_ids,
            option_name='--guild-ids',
            allowed_guild_ids=profile.allowed_guild_ids,
        )
        guild_ids = validate_target_guilds(
            requested,
            profile.allowed_guild_ids,
            require_explicit=args.mode in ('inspect', 'apply'),
        )
        if args.mode == 'apply':
            if loaded_plan is not None and (
                    args.confirm_guild_configuration_plan
                    != loaded_plan.bundle_digest
            ):
                raise CommandManagementError(
                    'Apply requires the exact guild-configuration plan digest.'
                )
            if (
                    loaded_plan is None
                    and args.confirm_guild_configuration_plan is not None
            ):
                raise CommandManagementError(
                    'A guild-configuration plan digest was confirmed without '
                    'a plan.'
                )
            confirmed = _parse_guild_scope(
                args.confirm_guild_ids,
                option_name='--confirm-guild-ids',
                allowed_guild_ids=profile.allowed_guild_ids,
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
