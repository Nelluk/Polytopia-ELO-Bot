# Development guild configuration

Status: **beta operations guide**

The development beta and upstream production bot load active guild
configuration from PostgreSQL and manage it through private Discord workflows.
Production's completed cutover and guild-only command release are recorded in
`PRODUCTION_GUILD_CONFIGURATION.md`. This document is not authorization to
change a database or synchronize Discord commands.

## Authority model

The development profile selects exactly one source:

```ini
guild_configuration_source = static
```

or:

```ini
guild_configuration_source = database
```

`static` uses the selected environment's settings module. `database` requires
one exact reviewed application/database topology; validates the complete
stored graph before publishing it; and exposes one immutable in-memory snapshot
to the running process. Startup fails closed on an absent, partial, mismatched,
or invalid graph. Both upstream environments currently select `database`;
their ignored settings modules still define the allowed guild inventory and
historical shortcut names, and production retains its static values as a
reviewed rollback source.

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

The owner-facing server type is derived from the protected Team keys rather
than stored as a second setting:

- **Standard** — Squads and ordinary game commands; persistent Teams off.
- **Team** — Standard behavior plus optional persistent named Teams.
- **League** — persistent Teams required, with House and league commands.

The environment's control guild is a separate operational designation, not a
fourth gameplay type. Global leaderboard participation is also independent of
type and remains owner-only.

Ordinary reads use the published snapshot, not live database queries. A
committed configuration change must reload and publish the complete active
graph before it is reported as reconciled. If a commit succeeds but publication
fails, restart the supervised development bot to load the committed graph; do
not repeat the database mutation.

## Current operator surface

The ordinary same-server entry point is:

- `/guild settings` — privately view and edit settings for the server where
  the command is invoked. The configured bot owner sees the complete editor;
  an explicitly delegated guild manager sees ordinary fields only. Protected
  settings such as persistent Teams, global leaderboard participation, roles,
  and private destinations remain hidden and rejected
  by the worker if submitted.

The `/operator guild ...` surface requires database authority and
the configured bot owner. It intentionally has only two subcommands:

- `/operator guild list` — open the paginated server-management console.
  Select any enrolled server visible to the bot, then use target-bound actions
  for validation, history and restore, suspension/resumption, manager policy,
  or command-tree repair.
- `/operator guild enroll` — preview and enroll a quarantined visible guild,
  or update an enrolled guild's Standard/Team/League type and optional global
  leaderboard participation without resetting its other settings.

The normal settings flow is **`/guild settings` → choose a field → Save
changes**. It shows
human-readable field changes, asks for the displayed server name, and performs
fresh complete validation as part of Save. Cancel discards only the inactive
editing session. Internal version, generation, and digest checks still reject
stale writes without exposing that machinery in the normal UI. Discord guild
ownership does not grant `/operator` access; those workflows remain protected
by the configured bot-owner check. The Discord guild owner always has the same
ordinary-setting edit and activation access as a delegated manager, but cannot
edit protected settings.

## Onboarding and lifecycle

An unknown but allowed development guild is quarantined: the bot may observe
enough identity to offer enrollment, but it does not treat that guild as active
configuration authority. Enrollment creates the first immutable revision from
the basic prefix-server template. Standard is the default type. Ordinary users
begin at access level 2,
persistent Teams and league behavior are disabled, unequal side sizes are
disabled, sides are limited to two players, and global leaderboard
participation is disabled. The owner then reviews ordinary settings and uses
the selected server's **Repair commands** action when ready.

Suspension and resumption are opened for the selected target from
`/operator guild list` in an active operator guild.
Suspension preserves revisions, drafts, delegation, and audit history while
removing the guild from the running active snapshot. Resumption performs full
stored and live Discord validation before republishing it. A database lifecycle
commit and its Discord command-tree reconciliation are distinct effects; when
the latter fails, reopen the target from `/operator guild list` and choose
**Repair commands** without repeating the lifecycle write.

## Drafts, rollback, and delegation

A draft never changes runtime behavior until activation commits a new revision
and publishes the refreshed graph. Command policy is derived from server type
and configured destinations; it is not directly editable. Discord registration
remains a separate explicit guild-only synchronization.
The owner editor does not need a separate validation step: Save revalidates the
draft against current database, runtime, role, and channel state immediately
before the transaction commits.

Restore from the selected server's **History** action never rewrites history.
It creates a new revision from an earlier same-guild document and refuses a
source whose command capabilities differ from the active revision.

Delegation is opt-in per active guild. From the selected server's **Managers**
action, the bot owner enters an exact role name or numeric role ID. Ambiguous
duplicate names are refused and the resolved target role is shown before
confirmation. The owner also chooses whether those managers may activate
ordinary-setting drafts. The Discord guild owner has ordinary edit and
activation access independently of this optional role policy. Delegated
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
workflow, the selected server's **Repair commands** action under
`/operator guild list` reconciles the active type-derived policy for one exact
guild.

The capability split follows the same concepts. `core_user` includes
`/leaderboard squads`; the separate `squad` capability exposes `/squad`.
Neither requires `allow_teams`. The `team` capability exposes `/team`, whose
runtime checks still require the protected `allow_teams` setting. Changing a
server type or a destination that controls command availability therefore
still requires a Discord command-tree operation after the underlying runtime
policy is published.

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
