"""Pure application-command capability policy and deployment planning.

This module deliberately has no Discord, Peewee, or settings imports.  The
runtime profile supplies allowed guild IDs and the server-settings module
supplies assignments; the command-management script supplies local command
templates and remote snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


class ApplicationCommandPolicyError(ValueError):
    """Raised when a capability policy cannot be made safe and deterministic."""


_ROOT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# Root names are a repository-backed vocabulary, not free-form configuration.
# Adding a future root requires a taxonomy/policy change first.
KNOWN_TOP_LEVEL_ROOTS = frozenset({
    'about', 'elo', 'game', 'guide', 'help', 'house', 'leaderboard',
    'guild', 'league', 'operator', 'player', 'squad', 'staffhelp', 'support', 'team',
    'tools',
})

# ``tools_support`` is deliberately explicit about the source roots it can
# expose.  The other names remain taxonomy vocabulary, but are not loaded by
# the current command source and therefore are not silently included in a
# capability assignment.
TOOLS_SUPPORT_IMPLEMENTED_ROOTS = ('staffhelp',)
TOOLS_SUPPORT_RESERVED_ROOTS = ('about', 'guide', 'help', 'support', 'tools')


@dataclass(frozen=True)
class CapabilityFamily:
    """One policy capability and the top-level roots it may expose."""

    name: str
    roots: tuple[str, ...]
    visibility: str = "public"
    description: str = ""
    compatibility_only: bool = False


# These are policy entries, not claims that every root already has a command
# implementation.  Future roots remain default-deny until a guild explicitly
# receives their capability.
DEFAULT_CAPABILITY_FAMILIES = (
    CapabilityFamily(
        name="core_user",
        roots=("game", "leaderboard", "player"),
        visibility="public",
        description="Core read and game-user commands.",
    ),
    CapabilityFamily(
        name="elo_maintenance",
        roots=("elo",),
        visibility="staff",
        description="ELO maintenance and management commands.",
    ),
    CapabilityFamily(
        name="team",
        roots=("team",),
        visibility="future",
        description="Reserved team command root.",
    ),
    CapabilityFamily(
        name="league",
        roots=("league",),
        visibility="future",
        description="Reserved league command root.",
    ),
    CapabilityFamily(
        name="house",
        roots=("house",),
        visibility="future",
        description="Reserved house command root.",
    ),
    CapabilityFamily(
        name="squad",
        roots=("squad",),
        visibility="future",
        description="Reserved squad command root.",
    ),
    CapabilityFamily(
        name="tools_support",
        # The source loader currently contains one tools-support root.  Keep
        # the other taxonomy names reserved rather than making a capability
        # assignment fail because it refers to roots that are not loaded.
        roots=TOOLS_SUPPORT_IMPLEMENTED_ROOTS,
        visibility="public",
        description=(
            "Public staff-help intake with an environment-explicit delivery backend."
        ),
    ),
    CapabilityFamily(
        name="beta_testing",
        roots=(),
        visibility="development-only",
        description="Retired compatibility assignment with no command roots.",
        compatibility_only=True,
    ),
    CapabilityFamily(
        name="operator",
        roots=("guild", "operator"),
        visibility="administrator-default",
        description=(
            "Cross-guild operator commands with authoritative configured-ID "
            "checks."
        ),
    ),
)


def _family_map(
        families: Iterable[CapabilityFamily],
        *,
        available_roots: Iterable[str] | None = None) -> Mapping[str, CapabilityFamily]:
    """Validate family definitions and return a deterministic name mapping."""

    by_name: dict[str, CapabilityFamily] = {}
    by_root: dict[str, str] = {}
    known_roots = {
        _validate_root(root, "available root")
        for root in (
            KNOWN_TOP_LEVEL_ROOTS if available_roots is None else available_roots
        )
    }

    for family in families:
        if not isinstance(family, CapabilityFamily):
            raise ApplicationCommandPolicyError(
                "Capability definitions must contain CapabilityFamily values."
            )
        if not isinstance(family.name, str) or not family.name:
            raise ApplicationCommandPolicyError(
                "Capability names must be non-empty strings."
            )
        if family.name in by_name:
            raise ApplicationCommandPolicyError(
                f"Duplicate application-command capability: {family.name!r}."
            )
        roots = tuple(_validate_root(root, f"capability {family.name!r}")
                      for root in family.roots)
        if len(roots) != len(set(roots)):
            raise ApplicationCommandPolicyError(
                f"Capability {family.name!r} contains duplicate roots."
            )
        unknown = set(roots) - known_roots
        if unknown:
            raise ApplicationCommandPolicyError(
                f"Capability {family.name!r} contains unknown root(s): "
                + ", ".join(sorted(unknown))
                + "."
            )
        for root in roots:
            previous = by_root.get(root)
            if previous is not None:
                raise ApplicationCommandPolicyError(
                    f"Root {root!r} is assigned to conflicting capabilities "
                    f"{previous!r} and {family.name!r}."
                )
            by_root[root] = family.name
        by_name[family.name] = CapabilityFamily(
            name=family.name,
            roots=roots,
            visibility=family.visibility,
            description=family.description,
            compatibility_only=family.compatibility_only,
        )

    return MappingProxyType(by_name)


def _validate_root(root: Any, context: str) -> str:
    if not isinstance(root, str) or not _ROOT_PATTERN.fullmatch(root):
        raise ApplicationCommandPolicyError(
            f"{context} must be a lower-case top-level command root; "
            f"received {root!r}."
        )
    return root


def _validate_guild_id(guild_id: Any, context: str) -> int:
    if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
        raise ApplicationCommandPolicyError(
            f"{context} must be a positive integer guild ID; received {guild_id!r}."
        )
    return guild_id


@dataclass(frozen=True)
class GuildCapabilityAssignment:
    guild_id: int
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityPolicy:
    """Validated default-deny policy for one runtime profile."""

    allowed_guild_ids: tuple[int, ...]
    families: Mapping[str, CapabilityFamily]
    assignments: tuple[GuildCapabilityAssignment, ...]

    def capabilities_for_guild(self, guild_id: int) -> tuple[str, ...]:
        for assignment in self.assignments:
            if assignment.guild_id == guild_id:
                return assignment.capabilities
        return ()

    def roots_for_guild(self, guild_id: int) -> tuple[str, ...]:
        roots = {
            root
            for capability in self.capabilities_for_guild(guild_id)
            for root in self.families[capability].roots
        }
        return tuple(sorted(roots))

    def assigned_guild_ids(self) -> tuple[int, ...]:
        return tuple(assignment.guild_id for assignment in self.assignments)


def build_capability_policy(
        assignments: Mapping[int, Sequence[str]] | None,
        allowed_guild_ids: Iterable[int],
        *,
        all_guild_capabilities: Sequence[str] = (),
        families: Iterable[CapabilityFamily] = DEFAULT_CAPABILITY_FAMILIES,
        available_roots: Iterable[str] | None = None) -> CapabilityPolicy:
    """Validate repository assignments against one runtime guild allowlist.

    Missing or empty assignments intentionally produce a valid empty policy.
    That is the default-deny state.  Configured capability names are sorted in
    the returned immutable policy, so equivalent mappings produce equal plans.
    """

    allowed = tuple(sorted({
        _validate_guild_id(guild_id, "allowed guild ID")
        for guild_id in allowed_guild_ids
    }))
    family_map = _family_map(families, available_roots=available_roots)
    if assignments is None:
        assignments = {}
    if not isinstance(assignments, Mapping):
        raise ApplicationCommandPolicyError(
            "application_command_capabilities must be a mapping of guild IDs "
            "to capability names."
        )

    def validate_capabilities(
            raw_capabilities: Sequence[str], context: str) -> tuple[str, ...]:
        if isinstance(raw_capabilities, (str, bytes)) or not isinstance(
                raw_capabilities, Sequence):
            raise ApplicationCommandPolicyError(
                f'{context} must be a sequence of capability names.'
            )
        capabilities = tuple(raw_capabilities)
        if any(not isinstance(capability, str) for capability in capabilities):
            raise ApplicationCommandPolicyError(
                f'{context} contains a non-string capability name.'
            )
        if len(capabilities) != len(set(capabilities)):
            raise ApplicationCommandPolicyError(
                f'{context} contains duplicate capability assignments.'
            )
        for capability in capabilities:
            if capability not in family_map:
                raise ApplicationCommandPolicyError(
                    f'{context} references unknown capability {capability!r}.'
                )
            if (not family_map[capability].roots
                    and not family_map[capability].compatibility_only):
                raise ApplicationCommandPolicyError(
                    f'Capability {capability!r} has no application-command '
                    'roots and cannot be registered.'
                )
        return tuple(sorted(capabilities))

    universal = validate_capabilities(
        all_guild_capabilities,
        'All-guild application-command capabilities',
    )
    by_guild: dict[int, tuple[str, ...]] = {}
    for raw_guild_id, raw_capabilities in assignments.items():
        guild_id = _validate_guild_id(
            raw_guild_id, "application-command assignment guild ID"
        )
        if guild_id not in allowed:
            raise ApplicationCommandPolicyError(
                f"Guild {guild_id} is not in the runtime profile's allowed "
                "guild IDs."
            )
        capabilities = validate_capabilities(
            raw_capabilities,
            f'Assignments for guild {guild_id}',
        )
        overlap = set(capabilities) & set(universal)
        if overlap:
            raise ApplicationCommandPolicyError(
                f'Guild {guild_id} redundantly assigns all-guild capability '
                + ', '.join(sorted(overlap))
                + '.'
            )
        by_guild[guild_id] = tuple(sorted((*capabilities, *universal)))

    if universal:
        for guild_id in allowed:
            by_guild.setdefault(guild_id, universal)

    validated = tuple(
        GuildCapabilityAssignment(guild_id=guild_id, capabilities=capabilities)
        for guild_id, capabilities in sorted(by_guild.items())
        if capabilities
    )
    return CapabilityPolicy(
        allowed_guild_ids=allowed,
        families=family_map,
        assignments=validated,
    )


@dataclass(frozen=True)
class CommandDescriptor:
    """Immutable comparison data for a top-level command template/snapshot."""

    name: str
    fingerprint: str
    command: Any = field(default=None, repr=False, compare=False)


def _without_runtime_ids(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_runtime_ids(item)
            for key, item in value.items()
            if key not in {"id", "application_id"}
        }
    if isinstance(value, (list, tuple)):
        return [_without_runtime_ids(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _permission_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return getattr(value, 'value', str(value))


_EMPTY_EQUIVALENT_FIELDS = frozenset({
    'channel_types', 'choices', 'description_localizations',
    'name_localizations', 'options',
})


def _canonicalize_discord_defaults(value: Any) -> Any:
    """Remove API response defaults that discord.py omits when sending."""

    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            # Guild-scoped commands cannot be invoked in DMs. Discord returns
            # dm_permission=True even when the local guild template serializes
            # False, so it is not meaningful in this tool's only scope.
            if key in {'dm_permission', 'contexts', 'integration_types'}:
                continue
            normalized = _canonicalize_discord_defaults(item)
            if normalized is None:
                continue
            if key in _EMPTY_EQUIVALENT_FIELDS and normalized in ({}, []):
                continue
            if key == 'autocomplete' and normalized is False:
                continue
            canonical[str(key)] = normalized
        return canonical
    if isinstance(value, list):
        return [_canonicalize_discord_defaults(item) for item in value]
    return value


def _canonical_command_payload(command: Any, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Align discord.py local and fetched command serialization shapes."""

    canonical = dict(payload)

    if hasattr(command, 'nsfw'):
        canonical.setdefault('nsfw', bool(command.nsfw))
    if hasattr(command, 'default_member_permissions'):
        canonical.setdefault(
            'default_member_permissions',
            _permission_value(command.default_member_permissions),
        )
    elif hasattr(command, 'default_permissions'):
        canonical.setdefault(
            'default_member_permissions',
            _permission_value(command.default_permissions),
        )
    return _canonicalize_discord_defaults(canonical)


def command_payload(command: Any, *, tree: Any = None) -> Mapping[str, Any]:
    """Return stable command payload data from local or remote command objects."""

    to_dict = getattr(command, "to_dict", None)
    if to_dict is None or not callable(to_dict):
        if isinstance(command, Mapping):
            payload = command
        else:
            payload = {
                "name": getattr(command, "name", None),
                "description": getattr(command, "description", ""),
                "options": getattr(command, "options", ()),
            }
    else:
        try:
            signature = inspect.signature(to_dict)
            if len(signature.parameters) == 0:
                payload = to_dict()
            else:
                payload = to_dict(tree)
        except (TypeError, ValueError):
            try:
                payload = to_dict(tree)
            except TypeError:
                payload = to_dict()
    normalized = _without_runtime_ids(payload)
    if not isinstance(normalized, Mapping):
        raise ApplicationCommandPolicyError(
            f"Command {getattr(command, 'name', None)!r} did not serialize "
            "to a mapping."
        )
    return _canonical_command_payload(command, normalized)


def describe_command(command: Any, *, tree: Any = None) -> CommandDescriptor:
    name = getattr(command, "name", None)
    if not isinstance(name, str) or not name:
        if isinstance(command, Mapping):
            name = command.get("name")
    if not isinstance(name, str) or not name:
        raise ApplicationCommandPolicyError(
            f"Application command has no valid top-level name: {command!r}."
        )
    payload = command_payload(command, tree=tree)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return CommandDescriptor(
        name=name,
        fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        command=command,
    )


def select_command_templates(
        source_commands: Iterable[Any],
        roots: Iterable[str],
        *,
        tree: Any = None) -> tuple[CommandDescriptor, ...]:
    """Select only configured roots from loaded local command definitions."""

    allowed_roots = tuple(sorted({
        _validate_root(root, "configured command root") for root in roots
    }))
    allowed = set(allowed_roots)
    selected: list[CommandDescriptor] = []
    seen: set[str] = set()
    for command in source_commands:
        descriptor = describe_command(command, tree=tree)
        if descriptor.name not in allowed:
            continue
        if descriptor.name in seen:
            raise ApplicationCommandPolicyError(
                f"Loaded command definitions contain duplicate root "
                f"{descriptor.name!r}."
            )
        seen.add(descriptor.name)
        selected.append(descriptor)
    missing = allowed - seen
    if missing:
        raise ApplicationCommandPolicyError(
            "Configured application-command root(s) are not present in the "
            "loaded command source: " + ", ".join(sorted(missing)) + "."
        )
    return tuple(sorted(selected, key=lambda item: item.name))


@dataclass(frozen=True)
class CommandDiff:
    scope: str
    guild_id: int
    creates: tuple[str, ...]
    updates: tuple[str, ...]
    unchanged: tuple[str, ...]
    removals: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.creates or self.updates or self.removals)


@dataclass(frozen=True)
class GuildCommandPlan:
    scope: str
    guild_id: int
    desired: tuple[CommandDescriptor, ...]
    current: tuple[CommandDescriptor, ...]
    diff: CommandDiff


def diff_command_state(
        *,
        scope: str,
        guild_id: int,
        desired: Iterable[CommandDescriptor],
        current: Iterable[CommandDescriptor | Any],
        tree: Any = None) -> CommandDiff:
    """Compare one desired/current guild scope deterministically."""

    desired_map = {item.name: item for item in desired}
    current_descriptors = tuple(
        item if isinstance(item, CommandDescriptor)
        else describe_command(item, tree=tree)
        for item in current
    )
    current_map = {item.name: item for item in current_descriptors}
    if len(current_map) != len(current_descriptors):
        raise ApplicationCommandPolicyError(
            f"Current command snapshot for guild {guild_id} contains duplicate roots."
        )
    names = sorted(set(desired_map) | set(current_map))
    creates = tuple(name for name in names if name not in current_map)
    updates = tuple(
        name for name in names
        if name in desired_map and name in current_map
        and desired_map[name].fingerprint != current_map[name].fingerprint
    )
    unchanged = tuple(
        name for name in names
        if name in desired_map and name in current_map
        and desired_map[name].fingerprint == current_map[name].fingerprint
    )
    removals = tuple(name for name in names if name not in desired_map)
    return CommandDiff(
        scope=scope,
        guild_id=_validate_guild_id(guild_id, "plan guild ID"),
        creates=creates,
        updates=updates,
        unchanged=unchanged,
        removals=removals,
    )


def plan_guild_commands(
        policy: CapabilityPolicy,
        guild_id: int,
        source_commands: Iterable[Any],
        current_commands: Iterable[CommandDescriptor | Any] = (),
        *,
        tree: Any = None) -> GuildCommandPlan:
    guild_id = _validate_guild_id(guild_id, "plan guild ID")
    if guild_id not in policy.allowed_guild_ids:
        raise ApplicationCommandPolicyError(
            f"Guild {guild_id} is outside the runtime profile's allowed guild IDs."
        )
    desired = select_command_templates(
        source_commands,
        policy.roots_for_guild(guild_id),
        tree=tree,
    )
    current = tuple(
        item if isinstance(item, CommandDescriptor)
        else describe_command(item, tree=tree)
        for item in current_commands
    )
    return GuildCommandPlan(
        scope="guild",
        guild_id=guild_id,
        desired=desired,
        current=current,
        diff=diff_command_state(
            scope="guild",
            guild_id=guild_id,
            desired=desired,
            current=current,
            tree=tree,
        ),
    )


def plan_application_commands(
        policy: CapabilityPolicy,
        source_commands: Iterable[Any],
        current_by_guild: Mapping[int, Iterable[CommandDescriptor | Any]] | None = None,
        *,
        guild_ids: Iterable[int] | None = None,
        tree: Any = None) -> tuple[GuildCommandPlan, ...]:
    """Build desired-vs-current plans for every selected guild scope.

    All allowed guilds are included by default, including unassigned guilds,
    so default-deny also produces an explicit removal/pruning plan.
    """

    selected = tuple(sorted({
        _validate_guild_id(guild_id, "selected guild ID")
        for guild_id in (policy.allowed_guild_ids if guild_ids is None else guild_ids)
    }))
    disallowed = set(selected) - set(policy.allowed_guild_ids)
    if disallowed:
        raise ApplicationCommandPolicyError(
            "Selected guild IDs are outside the runtime profile allowlist: "
            + ", ".join(str(guild_id) for guild_id in sorted(disallowed))
            + "."
        )
    current_by_guild = current_by_guild or {}
    unknown_current = set(current_by_guild) - set(selected)
    if unknown_current:
        raise ApplicationCommandPolicyError(
            "Current command snapshots contain guilds outside the selected "
            "deployment scope: "
            + ", ".join(str(guild_id) for guild_id in sorted(unknown_current))
            + "."
        )
    return tuple(
        plan_guild_commands(
            policy,
            guild_id,
            source_commands,
            current_by_guild.get(guild_id, ()),
            tree=tree,
        )
        for guild_id in selected
    )


def policy_from_server_settings(
        server_settings: Any,
        allowed_guild_ids: Iterable[int],
        *,
        families: Iterable[CapabilityFamily] = DEFAULT_CAPABILITY_FAMILIES) -> CapabilityPolicy:
    """Build a policy from a settings module without importing runtime settings."""

    assignments = getattr(server_settings, "application_command_capabilities", {})
    all_guild_capabilities = getattr(
        server_settings,
        'application_command_all_guild_capabilities',
        (),
    )
    return build_capability_policy(
        assignments,
        allowed_guild_ids,
        all_guild_capabilities=all_guild_capabilities,
        families=families,
    )
