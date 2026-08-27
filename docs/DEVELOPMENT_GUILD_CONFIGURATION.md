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

Ordinary reads use the published snapshot, not live database queries. A
committed configuration change must reload and publish the complete active
graph before it is reported as reconciled. If a commit succeeds but publication
fails, restart the supervised development bot to load the committed graph; do
not repeat the database mutation.

## Current operator surface

These workflows require development database authority and the configured
owner unless a narrower delegated boundary is stated:

- `/operator guild list` — list active, suspended, and pending-visible guilds.
- `/operator guild settings` — inspect the invoking guild's active settings.
- `/operator guild validate` — validate the invoking guild read-only against
  the database, typed schema, live Discord objects, and bot permissions.
- `/operator guild history` — inspect bounded revision and audit history.
- `/operator guild enroll` — preview and enroll a quarantined visible guild
  using the basic prefix-server template.
- `/operator guild edit` — create, edit, validate, and activate an inactive
  draft for one active guild.
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

All mutating workspaces are private and use exact version, generation, digest,
or confirmation checks. A stale preview or draft must be reopened rather than
forced through. Owner operations remain owner-only even when the `guild` and
`operator` roots share a Discord capability assignment.

## Onboarding and lifecycle

An unknown but allowed development guild is quarantined: the bot may observe
enough identity to offer enrollment, but it does not treat that guild as active
configuration authority. Enrollment creates the first immutable revision from
the basic prefix-server template. The owner then reviews ordinary settings and
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

Rollback never rewrites history. It creates a new revision from an earlier
same-guild document and refuses a source whose command capabilities differ from
the active revision. Command-tree changes must use their dedicated workflow.

Delegation is opt-in per active guild. The owner selects exact manager role IDs
and whether those managers may activate ordinary-setting drafts. Delegated
managers cannot edit capabilities, lifecycle, ownership, delegation, another
guild, or bot-wide operator state. Runtime permission checks remain
authoritative even when Discord hides or exposes a command root.

## Discord command synchronization

Normal startup never synchronizes application commands. Source-level command
changes use [APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md](APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md).
Once the database-authority development bot already exposes the operator
workflow, `/operator guild commands` owns capability activation and
reconciliation for one exact active guild.

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
