"""Explicit guild-only application-command capability planning and apply.

The database activation remains in the guild-configuration worker.  This
module owns only immutable Discord plan evidence and the explicitly confirmed
remote guild apply.  It has no database imports and no global sync path.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

import discord

from modules.application_command_policy import (
    ApplicationCommandPolicyError,
    CapabilityPolicy,
    build_capability_policy,
    describe_command,
    plan_guild_commands,
)


ACTIVATE = 'activate'
RECONCILE = 'reconcile'
LIFECYCLE = 'lifecycle'
MODES = frozenset({ACTIVATE, RECONCILE, LIFECYCLE})
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class OperatorGuildCommandCapabilityError(RuntimeError):
    """A capability plan or explicit guild-only apply could not finish."""


class OperatorGuildCommandCapabilityDrift(
    OperatorGuildCommandCapabilityError,
):
    """Database, source-command, or remote Discord evidence changed."""


class OperatorGuildCommandCapabilityCommitted(
    OperatorGuildCommandCapabilityError,
):
    """The database committed but command-tree convergence is unverified."""

    def __init__(self, *, revision: int, generation: int, detail: str):
        self.revision = int(revision)
        self.generation = int(generation)
        super().__init__(
            f'Configuration r{self.revision}/g{self.generation} committed and '
            'the running policy is fail-closed, but the exact guild command '
            f'tree is not verified ({detail}). Reopen the server from '
            '`/operator guild list` and choose **Repair commands** to '
            'reconcile without another database write.'
        )


@dataclass(frozen=True)
class GuildCommandCapabilityPlan:
    mode: str
    guild_id: int
    active_revision: int
    active_generation: int
    active_document_digest: str
    current_capabilities: tuple[str, ...]
    desired_capabilities: tuple[str, ...]
    draft_version: int | None
    draft_document_digest: str | None
    current_commands: tuple[tuple[str, str], ...]
    desired_commands: tuple[tuple[str, str], ...]
    creates: tuple[str, ...]
    updates: tuple[str, ...]
    unchanged: tuple[str, ...]
    removals: tuple[str, ...]
    plan_digest: str

    @property
    def confirmation(self) -> str:
        if self.mode == ACTIVATE:
            return (
                f'ACTIVATE COMMANDS {self.draft_document_digest} '
                f'{self.plan_digest}'
            )
        if self.mode == RECONCILE:
            return f'SYNC COMMANDS {self.plan_digest}'
        return f'LIFECYCLE COMMANDS {self.plan_digest}'


@dataclass(frozen=True)
class GuildCommandCapabilityApplyResult:
    guild_id: int
    roots: tuple[str, ...]
    synced_count: int


@dataclass(frozen=True)
class GuildCommandCapabilityCompletion:
    plan: GuildCommandCapabilityPlan
    apply: GuildCommandCapabilityApplyResult
    committed_revision: int | None = None
    committed_generation: int | None = None


def _strict_positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperatorGuildCommandCapabilityError(f'{label} is invalid.')
    return value


def _assignments(
    policy: CapabilityPolicy,
    guild_id: int,
    desired_capabilities: Sequence[str],
) -> Mapping[int, tuple[str, ...]]:
    values = {
        current_id: policy.capabilities_for_guild(current_id)
        for current_id in policy.allowed_guild_ids
    }
    values[guild_id] = tuple(desired_capabilities)
    return values


def candidate_policy(
    policy: CapabilityPolicy,
    *,
    guild_id: int,
    desired_capabilities: Sequence[str],
) -> CapabilityPolicy:
    """Return the complete candidate policy with only one guild replaced."""

    guild_id = _strict_positive(guild_id, 'Target guild ID')
    if guild_id not in policy.allowed_guild_ids:
        raise OperatorGuildCommandCapabilityError(
            'The target guild is outside the running database inventory.'
        )
    try:
        return build_capability_policy(
            _assignments(policy, guild_id, desired_capabilities),
            policy.allowed_guild_ids,
            families=tuple(policy.families.values()),
        )
    except ApplicationCommandPolicyError as exc:
        raise OperatorGuildCommandCapabilityError(str(exc)) from exc


def _command_evidence(
    commands: Sequence[Any],
    *,
    tree: Any,
) -> tuple[tuple[str, str], ...]:
    descriptors = tuple(describe_command(command, tree=tree) for command in commands)
    values = tuple(sorted((value.name, value.fingerprint) for value in descriptors))
    if len(values) != len({name for name, _fingerprint in values}):
        raise OperatorGuildCommandCapabilityError(
            'The Discord command snapshot contains duplicate roots.'
        )
    return values


def _payload(
    *,
    mode: str,
    guild_id: int,
    active_revision: int,
    active_generation: int,
    active_document_digest: str,
    current_capabilities: Sequence[str],
    desired_capabilities: Sequence[str],
    draft_version: int | None,
    draft_document_digest: str | None,
    current_commands: Sequence[tuple[str, str]],
    desired_commands: Sequence[tuple[str, str]],
    creates: Sequence[str],
    updates: Sequence[str],
    unchanged: Sequence[str],
    removals: Sequence[str],
) -> dict[str, Any]:
    return {
        'mode': mode,
        'guild_id': guild_id,
        'active_revision': active_revision,
        'active_generation': active_generation,
        'active_document_digest': active_document_digest,
        'current_capabilities': list(current_capabilities),
        'desired_capabilities': list(desired_capabilities),
        'draft_version': draft_version,
        'draft_document_digest': draft_document_digest,
        'global_commands': [],
        'current_commands': [list(value) for value in current_commands],
        'desired_commands': [list(value) for value in desired_commands],
        'creates': list(creates),
        'updates': list(updates),
        'unchanged': list(unchanged),
        'removals': list(removals),
    }


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _replace_local_guild_commands(
    tree: Any,
    desired: Sequence[Any],
    guild: discord.Object,
) -> None:
    """Install source templates without deep-copying their live cog bindings."""

    tree.clear_commands(guild=guild)
    for descriptor in desired:
        if descriptor.command is None:
            raise OperatorGuildCommandCapabilityError(
                f'No local template exists for command root {descriptor.name!r}.'
            )
        # This matches discord.py's copy_global_to(): one command object may
        # safely occupy global and guild mappings. Deepcopy would traverse the
        # bound cog and fail on normal runtime objects such as thread locks.
        tree.add_command(descriptor.command, guild=guild)


async def inspect_command_plan(
    *,
    bot: Any,
    policy: CapabilityPolicy,
    guild_id: int,
    active_revision: int,
    active_generation: int,
    active_document_digest: str,
    current_capabilities: Sequence[str],
    desired_capabilities: Sequence[str],
    mode: str,
    draft_version: int | None = None,
    draft_document_digest: str | None = None,
) -> GuildCommandCapabilityPlan:
    """Fetch the global/target trees and freeze one explicit bounded plan."""

    if mode not in MODES:
        raise OperatorGuildCommandCapabilityError('The command plan mode is invalid.')
    guild_id = _strict_positive(guild_id, 'Target guild ID')
    active_revision = _strict_positive(active_revision, 'Active revision')
    active_generation = _strict_positive(active_generation, 'Active generation')
    if not isinstance(active_document_digest, str) or not _HEX_DIGEST.fullmatch(
            active_document_digest):
        raise OperatorGuildCommandCapabilityError(
            'The active document digest is invalid.'
        )
    current_capabilities = tuple(current_capabilities)
    desired_capabilities = tuple(desired_capabilities)
    if policy.capabilities_for_guild(guild_id) != current_capabilities:
        raise OperatorGuildCommandCapabilityDrift(
            'The running capability policy differs from the active document.'
        )
    if mode == ACTIVATE:
        _strict_positive(draft_version, 'Draft version')
        if (
                not isinstance(draft_document_digest, str)
                or not _HEX_DIGEST.fullmatch(draft_document_digest)
        ):
            raise OperatorGuildCommandCapabilityError(
                'Capability activation requires exact draft evidence.'
            )
        if current_capabilities == desired_capabilities:
            raise OperatorGuildCommandCapabilityError(
                'The draft does not change command capabilities; use ordinary activation.'
            )
    elif mode == RECONCILE:
        if draft_version is not None or draft_document_digest is not None:
            raise OperatorGuildCommandCapabilityError(
                'Command-tree reconciliation does not accept draft evidence.'
            )
        if current_capabilities != desired_capabilities:
            raise OperatorGuildCommandCapabilityError(
                'Reconciliation can apply only the already-active capability policy.'
            )
    else:
        if draft_version is not None or draft_document_digest is not None:
            raise OperatorGuildCommandCapabilityError(
                'Guild lifecycle planning does not accept draft evidence.'
            )

    desired_policy = candidate_policy(
        policy,
        guild_id=guild_id,
        desired_capabilities=desired_capabilities,
    )
    source_commands = tuple(bot.tree.get_commands())
    global_commands = tuple(await bot.tree.fetch_commands())
    if global_commands:
        roots = ', '.join(sorted(command.name for command in global_commands))
        raise OperatorGuildCommandCapabilityError(
            f'The remote global command tree is nonempty ({roots}); refusing '
            'guild capability planning and apply.'
        )
    guild = discord.Object(id=guild_id)
    current_remote = tuple(await bot.tree.fetch_commands(guild=guild))
    try:
        plan = plan_guild_commands(
            desired_policy,
            guild_id,
            source_commands,
            current_remote,
            tree=bot.tree,
        )
    except ApplicationCommandPolicyError as exc:
        raise OperatorGuildCommandCapabilityError(str(exc)) from exc
    active_root_updates = tuple(sorted(
        set(plan.diff.updates) & set(policy.roots_for_guild(guild_id))
    ))
    if active_root_updates:
        raise OperatorGuildCommandCapabilityError(
            'Discord has an older registered version of the active command '
            + ('group' if len(active_root_updates) == 1 else 'groups')
            + ': '
            + ', '.join(f'/{value}' for value in active_root_updates)
            + '. No configuration or Discord commands were changed. Deploy '
            'the reviewed source update with the external guild command tool, '
            'then reopen the server from `/operator guild list` and choose '
            '**Repair commands** if its type also changed.'
        )
    current_evidence = _command_evidence(current_remote, tree=bot.tree)
    desired_evidence = tuple(
        (value.name, value.fingerprint) for value in plan.desired
    )
    payload = _payload(
        mode=mode,
        guild_id=guild_id,
        active_revision=active_revision,
        active_generation=active_generation,
        active_document_digest=active_document_digest,
        current_capabilities=current_capabilities,
        desired_capabilities=desired_capabilities,
        draft_version=draft_version,
        draft_document_digest=draft_document_digest,
        current_commands=current_evidence,
        desired_commands=desired_evidence,
        creates=plan.diff.creates,
        updates=plan.diff.updates,
        unchanged=plan.diff.unchanged,
        removals=plan.diff.removals,
    )
    return GuildCommandCapabilityPlan(
        mode=mode,
        guild_id=guild_id,
        active_revision=active_revision,
        active_generation=active_generation,
        active_document_digest=active_document_digest,
        current_capabilities=current_capabilities,
        desired_capabilities=desired_capabilities,
        draft_version=draft_version,
        draft_document_digest=draft_document_digest,
        current_commands=current_evidence,
        desired_commands=desired_evidence,
        creates=plan.diff.creates,
        updates=plan.diff.updates,
        unchanged=plan.diff.unchanged,
        removals=plan.diff.removals,
        plan_digest=_digest(payload),
    )


async def apply_command_plan(
    *,
    bot: Any,
    policy: CapabilityPolicy,
    plan: GuildCommandCapabilityPlan,
) -> GuildCommandCapabilityApplyResult:
    """Recheck exact remote evidence, sync one guild, then verify convergence."""

    if not isinstance(plan, GuildCommandCapabilityPlan):
        raise OperatorGuildCommandCapabilityError(
            'A frozen command-capability plan is required.'
        )
    if plan.mode not in MODES or plan.plan_digest != _digest(_payload(
        mode=plan.mode,
        guild_id=plan.guild_id,
        active_revision=plan.active_revision,
        active_generation=plan.active_generation,
        active_document_digest=plan.active_document_digest,
        current_capabilities=plan.current_capabilities,
        desired_capabilities=plan.desired_capabilities,
        draft_version=plan.draft_version,
        draft_document_digest=plan.draft_document_digest,
        current_commands=plan.current_commands,
        desired_commands=plan.desired_commands,
        creates=plan.creates,
        updates=plan.updates,
        unchanged=plan.unchanged,
        removals=plan.removals,
    )):
        raise OperatorGuildCommandCapabilityDrift(
            'The frozen command-capability plan digest is invalid.'
        )
    desired_policy = candidate_policy(
        policy,
        guild_id=plan.guild_id,
        desired_capabilities=plan.desired_capabilities,
    )
    if desired_policy.roots_for_guild(plan.guild_id) != tuple(
        name for name, _fingerprint in plan.desired_commands
    ):
        raise OperatorGuildCommandCapabilityDrift(
            'The running desired command policy changed after preview.'
        )
    global_commands = tuple(await bot.tree.fetch_commands())
    if global_commands:
        raise OperatorGuildCommandCapabilityDrift(
            'The remote global command tree became nonempty; guild apply was refused.'
        )
    guild = discord.Object(id=plan.guild_id)
    current_remote = tuple(await bot.tree.fetch_commands(guild=guild))
    if _command_evidence(current_remote, tree=bot.tree) != plan.current_commands:
        raise OperatorGuildCommandCapabilityDrift(
            'The target guild command tree changed after preview; open a fresh plan.'
        )

    source_commands = tuple(bot.tree.get_commands())
    try:
        desired = plan_guild_commands(
            desired_policy,
            plan.guild_id,
            source_commands,
            current_remote,
            tree=bot.tree,
        )
    except ApplicationCommandPolicyError as exc:
        raise OperatorGuildCommandCapabilityError(str(exc)) from exc
    if tuple((item.name, item.fingerprint) for item in desired.desired) != plan.desired_commands:
        raise OperatorGuildCommandCapabilityDrift(
            'The local command source changed after preview; open a fresh plan.'
        )
    if desired.diff.has_changes:
        _replace_local_guild_commands(bot.tree, desired.desired, guild)
        synced = tuple(await bot.tree.sync(guild=guild))
    else:
        synced = ()

    verify_global = tuple(await bot.tree.fetch_commands())
    if verify_global:
        raise OperatorGuildCommandCapabilityError(
            'The global command tree became nonempty after guild apply.'
        )
    verify_remote = tuple(await bot.tree.fetch_commands(guild=guild))
    verify = plan_guild_commands(
        desired_policy,
        plan.guild_id,
        source_commands,
        verify_remote,
        tree=bot.tree,
    )
    if verify.diff.has_changes:
        raise OperatorGuildCommandCapabilityError(
            'The target guild command tree did not converge to the confirmed plan.'
        )
    return GuildCommandCapabilityApplyResult(
        guild_id=plan.guild_id,
        roots=tuple(item.name for item in verify.desired),
        synced_count=len(synced),
    )


__all__ = [
    'ACTIVATE',
    'MODES',
    'OperatorGuildCommandCapabilityDrift',
    'OperatorGuildCommandCapabilityCommitted',
    'OperatorGuildCommandCapabilityError',
    'RECONCILE',
    'GuildCommandCapabilityApplyResult',
    'GuildCommandCapabilityCompletion',
    'GuildCommandCapabilityPlan',
    'LIFECYCLE',
    'apply_command_plan',
    'candidate_policy',
    'inspect_command_plan',
    '_replace_local_guild_commands',
]
