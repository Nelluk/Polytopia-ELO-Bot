# Development guild configuration

Status: **development-only**

The development beta can load guild configuration from PostgreSQL and manage
that configuration through private Discord workflows. Production continues to
use the reviewed static `server_settings.py` profile. This document describes
the current subsystem; it is not authorization to enable database authority in
production, change a database, or synchronize Discord commands.

## Authority model

The development profile selects exactly one source:

```ini
guild_configuration_source = static
```

or:

```ini
guild_configuration_source = database
```

`static` uses the development settings module. `database` requires the exact
development environment, database, and role; validates the complete stored
graph before publishing it; and exposes one immutable in-memory snapshot to
the running process. Startup fails closed on an absent, partial, mismatched, or
invalid graph. Production rejects database guild-configuration authority.

The stored envelope consists of:

- one registry row per known guild, with lifecycle state, active revision, and
  generation;
- immutable typed configuration revisions and protected audit events;
- at most one expiring inactive draft per enrolled guild; and
- an optional versioned delegation policy for an active guild.

Three related game concepts remain deliberately separate:

- A **Side** is the set of players allied together in one game.
- A **Squad** is the automatically tracked, guild-scoped combination formed
  when the same players complete multiplayer games together.
- A **Team** is a persistent named organization. Persistent Team and league
  features are opt-in and are not required for multiplayer Sides or Squads.

The stored keys `allow_uneven_teams` and `max_team_size` are retained for
compatibility, but the user-facing policy names are **Allow unequal side
sizes** and **Maximum players per side**.

Ordinary reads use the published snapshot, not live database queries. A
committed configuration change must reload and publish the complete active
graph before it is reported as reconciled. If a commit succeeds but publication
fails, restart the supervised development bot to load the committed graph; do
not repeat the database mutation.

## Current operator surface

These workflows require development database authority and the configured
owner unless a narrower delegated boundary is stated:

- `/operator guild list` — list active, suspended, and pending-visible guilds.
- `/operator guild settings` — inspect the invoking guild's active settings
  and open the owner editor with **Edit settings**.
- `/operator guild validate` — validate the invoking guild read-only against
  the database, typed schema, live Discord objects, and bot permissions.
- `/operator guild history` — inspect bounded revision and audit history.
- `/operator guild enroll` — preview and enroll a quarantined visible guild
  using the basic prefix-server template.
- `/operator guild edit` — open the same owner editor directly; the normal
  same-guild path creates or resumes a private editing session.
- `/operator guild rollback` — clone an earlier compatible document into a new
  active revision; history remains immutable.
- `/operator guild commands` — activate a capability-changing draft or
  reconcile one active guild's Discord command tree.
- `/operator guild suspend` and `/operator guild resume` — change lifecycle
  state while preserving configuration history.
- `/operator guild delegation` — grant or revoke the invoking guild's manager
  roles and optional activation permission.
- `/guild edit` — allow a delegated manager to edit ordinary settings only in
  the active guild where the command is invoked.

The normal owner flow is **settings → Edit settings → Save changes**. It shows
human-readable field changes, asks for the displayed server name, and performs
fresh complete validation as part of Save. Cancel discards only the inactive
editing session. Internal version, generation, and digest checks still reject
stale writes without exposing that machinery in the normal UI. Owner operations
remain owner-only even when the `guild` and `operator` roots share a Discord
capability assignment.

## Onboarding and lifecycle

An unknown but allowed development guild is quarantined: the bot may observe
enough identity to offer enrollment, but it does not treat that guild as active
configuration authority. Enrollment creates the first immutable revision from
the basic prefix-server template. Ordinary users begin at access level 2,
persistent Teams and league behavior are disabled, unequal side sizes are
disabled, sides are limited to two players, and global leaderboard
participation is disabled. The owner then reviews ordinary settings and
separately deploys any desired application-command capabilities.

Suspension and resumption must be run from a different active operator guild.
Suspension preserves revisions, drafts, delegation, and audit history while
removing the guild from the running active snapshot. Resumption performs full
stored and live Discord validation before republishing it. A database lifecycle
commit and its Discord command-tree reconciliation are distinct effects; when
the latter fails, use `/operator guild commands` to reconcile without repeating
the lifecycle write.

## Drafts, rollback, and delegation

A draft never changes runtime behavior until activation commits a new revision
and publishes the refreshed graph. Ordinary-setting activation cannot change
command capabilities. Capability changes use `/operator guild commands` so the
database revision and exact guild-scoped Discord tree remain coordinated.
The owner editor does not need a separate validation step: Save revalidates the
draft against current database, runtime, role, and channel state immediately
before the transaction commits.

Rollback never rewrites history. It creates a new revision from an earlier
same-guild document and refuses a source whose command capabilities differ from
the active revision. Command-tree changes must use their dedicated workflow.

Delegation is opt-in per active guild. The owner selects exact manager role IDs
and whether those managers may activate ordinary-setting drafts. Delegated
managers cannot edit capabilities, lifecycle, ownership, delegation, another
guild, or bot-wide operator state. `allow_teams`, `require_teams`, and global
leaderboard participation are protected owner settings; delegated managers can
still manage the unequal-side and maximum-players-per-side controls. Runtime
permission checks remain authoritative even when Discord hides or exposes a
command root.

Persistent `/team` behavior remains gated by `allow_teams`. In a deliberately
Team-enabled guild, configured moderators may manage that guild's Team records,
including `Team.external_server`. League-only House and tier behavior retains
its PolyChampions/test scope. PCPLUS remains a satellite/event venue: its
case-insensitive name/notes routing override sends eligible side channels to
PCPLUS while the Team records continue to be owned by PolyChampions.

## Discord command synchronization

Normal startup never synchronizes application commands. Source-level command
changes use [APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md](APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md).
Once the database-authority development bot already exposes the operator
workflow, `/operator guild commands` owns capability activation and
reconciliation for one exact active guild.

The capability split follows the same concepts. `core_user` includes
`/leaderboard squads`; the separate `squad` capability exposes `/squad`.
Neither requires `allow_teams`. The `team` capability exposes `/team`, whose
runtime checks still require the protected `allow_teams` setting. Changing a
capability assignment therefore remains a Discord command-tree operation even
when the underlying runtime policy is already enabled.

Both paths are guild-only, inspect the global tree, and refuse apply while any
global commands exist. A nonempty global tree is evidence for separate review,
not permission to remove it. Database activation does not authorize Discord
synchronization, and Discord inspection does not authorize a database write.

## Exceptional maintenance tools

The tracked scripts are bootstrap, schema-recovery, and verification tools—not
the ordinary running interface:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_storage.py plan
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_storage.py verify
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_drafts.py plan
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_drafts.py verify
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_delegation.py plan
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_delegation.py verify
```

`plan` modes are connection-free where documented by the tool; `verify` modes
are read-only. Snapshot capture, bootstrap, and every `apply` require their
tool's exact current arguments, reviewed output, stopped-writer boundary, and
separate authorization. Never copy a historical confirmation token from Git
history. Inspect `--help` and current source before an exceptional operation.

## Safety and validation

- Keep production and development bot identities, guilds, roles, databases,
  configurations, and supervisors separate.
- Never run a second beta writer or replace the development database while the
  guarded beta is active.
- Do not pass live Discord or Peewee objects across worker boundaries.
- Keep Discord API calls outside database transactions.
- Treat committed-but-unpublished results as reconciliation work, not an
  invitation to repeat a database mutation.
- Preserve default-deny capability behavior and exact owner/delegation checks.
- Run focused storage, runtime, operator, lifecycle, delegation, command-policy,
  and database-gated tests in proportion to the changed boundary.
- Command definition or capability-shape changes require an explicit guild-only
  plan and, when separately approved, synchronization. Ordinary source changes
  do not.

The completed architecture and rollout records remain available at Git
checkpoint `a226ade9`. They are historical evidence, not current procedures.
