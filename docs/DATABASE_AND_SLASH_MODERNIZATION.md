# Database Access and Slash Command Modernization

Last updated: 2026-07-29

Status: Active

Current branch at last update: `codex/slash-async-unwin-pilot`

Source task: `thread://019fae66-8e3a-7a50-9a0f-d3d7160d2287`

## Purpose

This is the durable planning and execution record for modernizing PolyBot's
Peewee database access and Discord command interfaces. It is intended to
survive task handoffs and context compaction.

Every task that changes database execution or adds application commands must:

1. Read this document before implementation.
2. Reverify the repository state instead of trusting this snapshot.
3. Update the current work unit, evidence, decisions, and next action before
   finishing.

This document records intent and evidence. The code, tests, current
configuration, and `AGENTS.md` remain authoritative.

## Safety boundaries

- Work only in `/home/nelluk/PolyBot39-dev`.
- Do not access or modify `/home/nelluk/PolyBot39`, the production checkout.
- Do not connect to or modify the production database `polytopia2`.
- Production services, production command synchronization, and production
  deployment require separate explicit approval.
- A beta-bot launch or Discord command synchronization requires explicit
  approval for that test session.
- Development-database integration tests may run only through the existing
  gates that verify:
  - `POLYBOT_ENV=development`
  - database `polytopia_dev`
  - database role `polybot_dev`
- Do not weaken a safety gate to make a test run.
- Do not install dependencies, perform a schema migration, or begin a broad
  ORM replacement without separate approval.
- Preserve unrelated working-tree changes.

Before any privileged VPS operation, also follow the server-level
instructions supplied with the task. In particular, inspect
`/home/nelluk/disk-audit-latest.txt` before requesting a `sudo` command.

## Goals

1. Keep Discord's event loop responsive during database queries,
   recalculations, graph generation, exports, and other blocking work.
2. Make each multi-step write workflow atomic where practical.
3. Keep all Discord API calls outside database transactions.
4. Give worker threads their own Peewee connection lifecycle.
5. Prevent conflicting ELO and game-state mutations from racing.
6. Add native Discord slash interaction where the command has a clear,
   safe option model.
7. Preserve prefix commands during the transition.
8. Deliver small, independently testable and revertible units.
9. Accumulate enough evidence to decide later whether Peewee remains suitable;
   do not make a full async ORM migration part of these phases.

## Non-goals

- Converting every command merely to increase the slash-command count.
- Passing live Peewee model instances between the event-loop thread and a
  worker.
- Wrapping scattered individual queries in unbounded `asyncio.to_thread`
  calls.
- Routing every database operation through the single ELO executor.
- Changing permissions, game rules, ELO behavior, or prefix aliases unless a
  work unit explicitly calls for it.
- Globally synchronizing experimental application commands.
- Removing prefix commands before a separately approved deprecation plan.

## Working architecture

### Transactional write flow

A modernized write command should normally follow this boundary:

1. On the event-loop thread:
   - parse Discord-native inputs;
   - perform Discord-only checks and resolve members/channels;
   - capture primitive identifiers and immutable values;
   - defer a slash interaction before work that may exceed Discord's response
     window.
2. Reserve the appropriate domain coordinator if the operation conflicts with
   other jobs.
3. In one bounded worker:
   - open a worker-local Peewee connection;
   - reload records using primitive IDs;
   - revalidate mutable database state;
   - execute the complete synchronous transaction;
   - commit or roll back;
   - return an immutable result containing primitive data.
4. Back on the event-loop thread, and only after commit:
   - edit announcements;
   - create, rename, or delete Discord channels;
   - update roles;
   - send completion messages.
5. Release locks and coordinator state in guaranteed cleanup.

No Discord `await` belongs inside `db.atomic()`.

### Read flow

Read-heavy commands should use a separately bounded read executor or an
equivalent reusable read service. Each read job must own its Peewee connection
and return primitive/view-model data. Rendering that is CPU-heavy should also
stay off the event loop.

The read path must not share the one-thread ELO executor. Ordinary reads and
Discord events should remain responsive while an ELO mutation is running.

### Coordinator scope

- Keep `elo_job_coordinator` for operations that can conflict through ELO
  reversal, finalization, or recalculation.
- Introduce a different coordinator only when a concrete race requires it.
- Prefer a keyed per-game claim for ordinary game mutations over one global
  database lock.
- Do not generalize the pilot into a universal job framework until at least two
  non-ELO work units demonstrate the same need.

### Thread-boundary rules

Allowed worker inputs include integers, strings, booleans, timestamps, enums,
and immutable dataclasses made only from such values.

Do not pass these into workers:

- Peewee model instances or lazy queries;
- Discord guild, member, channel, message, interaction, or context objects;
- open transaction or connection objects;
- mutable collections that the event-loop thread may continue changing.

## Slash-interface rules

Use a hybrid command when the prefix grammar maps cleanly to native typed
options. Use a slash-only wrapper around shared application logic when the
prefix command is overloaded, relies on aliases with different meanings, or
has a free-form grammar that would make a poor slash interface.

For every command touched:

1. Decide explicitly whether slash conversion is:
   - included now;
   - deferred for a documented UX reason; or
   - intentionally not applicable.
2. Preserve the prefix command and existing aliases.
3. Use native option types where possible:
   - integer game IDs;
   - Discord members rather than free-form mentions;
   - choices for stable enumerations;
   - autocomplete only when it can respond cheaply.
4. Defer immediately before potentially slow work.
5. Keep permission checks equivalent between prefix and slash paths.
6. Make error visibility deliberate; permission or validation errors should
   generally be ephemeral for slash users.
7. Add registration tests for both interfaces.

Do not rename the five beta-validated pilot slash commands without a separate
compatibility and deprecation decision.

## Status vocabulary

- **Planned**: scoped but no implementation is in progress.
- **In progress**: the active work unit.
- **Implemented**: code and offline tests pass, but beta acceptance is pending
  when applicable.
- **Beta-validated**: approved live beta smoke testing passed.
- **Complete**: committed and integrated into the intended base branch.
- **Blocked**: a named decision, approval, or external change is required.
- **Deferred**: intentionally postponed with a recorded reason.

Only one unit should normally be **In progress**.

## Current execution pointer

The slash/ELO pilot is beta-validated on its feature branch. At the last
repository check:

- branch: `codex/slash-async-unwin-pilot`
- worktree: clean before this document was added
- branch position: two commits ahead of `origin/master`
- implementation checkpoint: `a9375b3`
- development-guild sync fix: `9a64ce1`
- beta acceptance: all five application commands reported working
- offline suite at the pilot checkpoint: 76 tests passed, with five gated
  database tests skipped as designed
- gated `polytopia_dev` suite: five tests passed

Current unit: **P1 — Pilot close-out and integration**

Recommended next code unit after P1: **P2.1 — Separate `newgame` transaction
work from Discord effects**. This is prioritized because inspection found a
Discord `await` inside the command's `db.atomic()` block.

Runtime status is deliberately not recorded as fact here. Verify whether a
beta process is running before starting or stopping one.

## Phase summary

| Phase | Status | Scope | Exit checkpoint |
|---|---|---|---|
| P0 | Beta-validated | Serialized ELO workers and first five slash commands | Commits `a9375b3`, `9a64ce1`; live beta acceptance |
| P1 | In progress | Close out, review, and integrate the pilot branch | Clean tests, beta stopped if still running, reviewed branch/PR or approved merge |
| P2 | Planned | Fix known game-creation transaction boundary | `newgame` workflow atomic and Discord effects post-commit |
| P3 | Planned | Owner ELO maintenance and job observability | Typed slash maintenance interface and active-job status |
| P4 | Planned | Game correction and metadata mutations | Bounded workers plus slash interfaces for clear typed operations |
| P5 | Planned | Matchmaking lifecycle | Atomic open/join/leave/kick/start flows and native interactions |
| P6 | Planned | Registration and player preferences | Worker-safe profile writes and slash UX |
| P7 | Planned | Read-heavy game, player, and leaderboard commands | Bounded read path and responsive slash queries |
| P8 | Planned | League and remaining administration workflows | Audited domain workers and selected native interfaces |
| P9 | Planned | Production rollout and later prefix deprecation decision | Approved deployment, monitoring, and separate deprecation plan |

## P0 — Serialized ELO and slash-command pilot

Status: **Beta-validated**

Completed scope:

- `unwin`, finalized `win`, confirmation, completed-game deletion, and
  recalculation use the ELO coordinator/worker boundary.
- The old independent `settings.recalculation_mode` Boolean was removed.
- ELO jobs expose operation, game, requester, and start time.
- `/win`, `/unwin`, `/delete`, `/confirm`, and `/unconfirmed` were added while
  preserving prefix behavior.
- The overloaded prefix `$confirm [GAME_ID|auto]` remains separate from
  slash-only `/confirm` and `/unconfirmed`.
- Development startup copies global command definitions into the approved
  development guild before guild synchronization.
- Live beta testing accepted all five commands.

Current limitations:

- `recalc_games_from` uses the worker but is still prefix-only.
- Short validations and many unrelated commands still query synchronously on
  the event-loop thread.
- The pilot branch has not been recorded here as merged into `origin/master`.
- Prefix interfaces remain required.

## P1 — Pilot close-out and integration

Status: **In progress**

Objective: finish the pilot as a reviewable checkpoint before adding another
large command group.

Work units:

- [ ] Reverify the beta bot is stopped; if it is still running, obtain the
  required authority and stop only the development process cleanly.
- [ ] Review both pilot commits for permission, transaction, cancellation, and
  command-sync regressions.
- [ ] Run `git diff --check`.
- [ ] Run the complete offline suite.
- [ ] Run the gated development-database suite only through its safety gates.
- [ ] Record any live-test fixture cleanup required in `polytopia_dev`.
- [ ] Push/open a PR or merge only when explicitly requested.
- [ ] Record the final integration commit or PR in the progress log.

Exit criteria:

- no beta process unintentionally left running;
- clean worktree apart from intentional planning-document updates;
- required tests green;
- pilot changes reviewed and integrated, or a named integration action is
  explicitly blocked on user direction.

## P2 — Game creation transaction boundary

Status: **Planned**

Why this phase is next:

`modules/games.py:newgame` currently performs Discord sends inside a
`db.atomic()` block. It also mixes Discord member resolution, database
creation, host assignment, logging, warnings, and post-create messaging.

### P2.1 — Extract the database workflow

- Resolve Discord members and permissions on the event-loop thread.
- Pass guild ID, requester ID, game properties, and participant Discord IDs to
  a synchronous worker.
- Reload/create all Peewee records in the worker.
- Create the game, sides, lineups, host assignment, and audit log in one
  transaction.
- Return warnings and primitive IDs/data.
- Send warnings and create/update Discord artifacts only after commit.
- Prove rollback leaves no partial game, lineup, or log records.
- Prove database failure causes no Discord channel or announcement effects.

### P2.2 — Decide the native command UX

Prefix parity includes quoted names, aliases that select ranked/platform
variants, shortcuts that infer the author, and flexible multi-side player
lists. Do not expose that grammar as one opaque slash string without review.

Evaluate:

- a typed `/newgame` with name, ranked/platform choices, and bounded side
  options; or
- a slash command plus modal for flexible side entry; or
- a documented temporary slash deferral if neither preserves existing game
  shapes safely.

The database extraction proceeds even if slash UX is deferred. Record the
decision and reason in the decision log.

Exit criteria:

- no Discord await inside the creation transaction;
- worker-local connection and primitive inputs;
- transaction and post-commit fault tests;
- prefix behavior preserved;
- slash decision implemented or explicitly deferred;
- approved beta test if an application command is added.

## P3 — Owner ELO maintenance and observability

Status: **Planned**

Candidate scope:

- `recalc_games_from`
- active ELO job status
- cancellation/abort semantics documentation
- review of hidden `reverse_duplicated_elo`
- command-line full recalculation consistency

Proposed slash interface:

- `/recalc-games-from game_id confirm`
  - owner-only;
  - required confirmation option;
  - immediate defer;
  - same coordinator and worker as the prefix command.
- `/elo-job-status`
  - staff-visible or owner-only, decided during implementation;
  - reports operation, game, requester, and elapsed time;
  - read-only and ephemeral.

Do not add a slash interface for a one-off repair command merely because the
code is touched. Decide whether `reverse_duplicated_elo` should be retained,
tested, documented as emergency-only, or retired.

Exit criteria:

- prefix `recalc_games_from` preserved;
- typed permission-equivalent slash path;
- conflict and status behavior tested;
- beta acceptance covers defer, confirmation, conflict, and harmless-command
  responsiveness.

## P4 — Game correction and metadata mutations

Status: **Planned**

Split this phase into small vertical units. Do not implement all candidates in
one commit.

### P4.1 — Staff state corrections

Candidates:

- `rankset`
- `rankunset`
- `unstart`
- `extend`

Use typed game IDs and choices. If these commands can race with finalized ELO
state, use the ELO coordinator; otherwise use a per-game claim rather than
blocking all ELO work.

### P4.2 — Game metadata

Candidates:

- `rename`
- `setmap`
- `settribe`
- `gamenotes`

Recommended slash shape:

- typed game ID;
- map/tribe choices or cheap autocomplete;
- Discord member input for a single-player tribe update;
- a separate bulk staff operation if bulk tribe assignment remains necessary.

Database mutation and audit logging should commit before announcement/channel
edits. Prefix inference from a game channel may remain a prefix convenience;
slash commands should prefer explicit game IDs.

Exit criteria for each unit:

- command-specific atomic worker;
- permission and cross-guild validation in both event-loop and worker layers;
- post-commit Discord effects;
- prefix aliases preserved;
- registration, rollback, and beta acceptance evidence.

## P5 — Matchmaking lifecycle

Status: **Planned**

Candidate order:

1. `join` and `leave`
2. `kick`
3. `open`/`opengame`
4. `start`
5. reaction listeners and background purge jobs

Why it is later:

These paths combine role checks, capacity/ELO restrictions, game/side/lineup
records, reactions, messages, channels, and background tasks. They need a
shared lifecycle service, not independent thread wrappers around individual
queries.

Required design:

- revalidate capacity and eligibility inside the transaction;
- use database constraints or row locking where practical;
- serialize mutations per pending game;
- return primitive result data for all Discord effects;
- make reaction and command paths call the same application service;
- ensure a Discord failure does not roll back an already committed database
  change, and define reconciliation behavior for failed Discord effects.

Likely slash interfaces:

- `/open-game`
- `/join game_id side`
- `/leave game_id`
- `/kick game_id member`
- `/start-game game_id name`

Names and grouping must be reviewed against Discord's application-command
limits and existing top-level commands before implementation.

## P6 — Registration and player preferences

Status: **Planned**

Candidate scope:

- `setname`
- `settime`
- squad naming and similar low-risk profile writes

Goals:

- use typed Discord member inputs for staff overrides;
- place lookup/create/update/log operations in one worker transaction;
- keep role changes and direct messages post-commit;
- add `/set-name` and `/set-time` or a reviewed command group while preserving
  prefix aliases;
- avoid exposing sensitive identifiers in public error messages.

This phase is a suitable proving ground for a reusable non-ELO write executor
if P2 and P4 demonstrate common infrastructure.

## P7 — Read-heavy commands and analytics

Status: **Planned**

Candidate scope:

- `game`
- `player`
- `team`
- `lb`, `lbteam`, `lbsquad`
- `games`, `allgames`, `incomplete`, `wins`
- `logs`
- graph and export generation

Goals:

- inventory which queries or render steps block the event loop;
- replace uses of the default executor with a bounded read/render executor;
- use worker-local Peewee connections;
- return display DTOs rather than live models or lazy queries;
- paginate slash results without reaction-only controls;
- add autocomplete only for indexed, bounded lookups;
- test responsiveness with simulated slow queries and rendering.

Read commands should be migrated in small related groups. Leaderboards and
image/graph generation should not share the ELO mutation worker.

## P8 — League and remaining administration workflows

Status: **Planned**

Candidate domains:

- team and house administration;
- drafts, trades, promotions, and auction operations;
- migrations and player deletion;
- purge/background maintenance;
- exports and backup-command review;
- API cog writes if the development API is later brought into scope.

Before implementation, create a command/data-flow inventory for the selected
domain. Many of these operations span Discord roles, messages, and several
database tables, so each needs its own transaction and reconciliation design.

Do not expose destructive or emergency maintenance commands as slash commands
without explicit confirmation UX, narrow permissions, audit logging, and
separate approval.

## P9 — Production rollout and prefix lifecycle

Status: **Planned**

Production rollout is a separate operational phase, not an implied consequence
of beta acceptance.

Required gates:

- all intended commits integrated and reviewed;
- offline and gated development-database suites green;
- beta command synchronization and smoke matrix green;
- production configuration and command-sync target verified;
- rollback point identified;
- monitoring and log checks defined;
- explicit approval for production deployment/restart/sync.

After a stable observation period, separately decide whether to:

- keep prefix commands indefinitely;
- mark selected prefix commands deprecated;
- remove only aliases with usage evidence and a communication plan.

## Standard work-unit template

Copy this section under the active phase for each implementation unit.

```markdown
### Unit Px.y — Short name

Status: In progress

Branch/base:

Objective:

In scope:

Out of scope:

Database boundary:

Slash decision:

Permissions to preserve:

Discord effects after commit:

Files expected:

Tests required:

- [ ] focused offline
- [ ] rollback/fault injection
- [ ] event-loop responsiveness when relevant
- [ ] conflict/concurrency when relevant
- [ ] prefix registration/behavior
- [ ] slash registration/defer/permissions
- [ ] complete offline suite
- [ ] gated development-database suite
- [ ] beta smoke test, if an application command or Discord effect changed

Approvals required:

Implementation evidence:

Commit(s):

Beta result:

Remaining limitations:

Next action:
```

## Validation matrix

Apply the rows relevant to the unit.

| Risk | Required proof |
|---|---|
| Event-loop blocking | A simulated slow query/calculation does not delay an unrelated coroutine |
| Conflicting writes | A second conflicting command rejects or queues promptly with useful state |
| Worker exception | Coordinator/lock/connection cleanup occurs in `finally` |
| Cancellation | State remains reserved until non-cancellable thread work actually finishes |
| Transaction failure | All related database changes and audit logs roll back |
| Discord/database ordering | No Discord post-effect occurs after database failure |
| Thread ownership | Worker opens/closes its connection and reloads by primitive ID |
| Permission parity | Prefix and slash paths allow/deny the same actors |
| Command compatibility | Prefix command and aliases remain registered |
| Interaction timing | Slash path defers before slow work |
| Guild isolation | Development sync targets only the approved development guild |
| Production safety | No production checkout, service, bot, or database touched |

Standard commands at the current checkpoint:

```bash
cd /home/nelluk/PolyBot39-dev

git status --short --branch
git diff --check

POLYBOT_ENV=development \
MPLCONFIGDIR=/tmp/polybot-matplotlib \
.venv/bin/python -m unittest discover -v

POLYBOT_ENV=development \
POLYBOT_RUN_DB_INTEGRATION=1 \
MPLCONFIGDIR=/tmp/polybot-matplotlib \
.venv/bin/python -m unittest tests.test_database_integration -v
```

Always inspect the current test layout and safety gates before reusing these
commands. Do not treat a historical command line as authority to bypass a new
gate.

## Beta acceptance template

An approved beta session should record:

- commit/branch under test;
- beta bot application ID;
- development guild ID;
- database name and role;
- background-task/API policy;
- exact application commands accepted by Discord;
- prefix parity cases;
- permission-denial cases;
- successful mutation and rollback/correction cases;
- event-loop responsiveness;
- conflict response;
- shutdown result;
- any disposable rows or channels requiring cleanup.

If startup synchronizes commands automatically, launch approval and sync
approval are one combined operational gate.

## Decision log

### D-001 — Preserve prefix commands during migration

Status: Accepted

Slash commands are additive until a separate deprecation plan is approved.

### D-002 — Use hybrid commands only for clean grammar parity

Status: Accepted

`win`, `unwin`, and `delete` map cleanly. The overloaded prefix `confirm`
does not, so `/confirm` and `/unconfirmed` are slash-only wrappers over shared
logic.

### D-003 — Keep synchronous Peewee behind explicit worker boundaries

Status: Accepted for the current roadmap

The project is not performing a full async ORM migration. Workers own
connections and synchronous transaction boundaries.

### D-004 — Primitive data crosses thread boundaries

Status: Accepted

Workers reload database models and return immutable result data. Live Peewee
or Discord objects remain on their owning thread.

### D-005 — Discord effects happen after commit

Status: Accepted

No Discord await occurs inside a transaction. Failures after commit require
logging/reconciliation rather than database rollback.

### D-006 — ELO mutations use one coordinator

Status: Accepted

Finalization, reversal, deletion with ELO impact, and recalculation conflict
through one single-worker coordinator. Ordinary reads and unrelated writes do
not use it.

### D-007 — Development guild sync copies global definitions first

Status: Accepted

In development, `copy_global_to` precedes guild synchronization so application
commands appear immediately without a global sync.

### D-008 — Defer general worker-framework extraction

Status: Accepted

Do not generalize `elo_job_coordinator` after only one domain. Reconsider after
P2 and at least one P4/P5 unit reveal concrete shared requirements.

## Progress log

### 2026-07-29 — ELO/slash pilot beta accepted

- Added serialized, cancellation-aware ELO job coordination.
- Added synchronous worker transactions for win, unwin, confirmation,
  deletion, and recalculation.
- Added `/win`, `/unwin`, `/delete`, `/confirm`, and `/unconfirmed`.
- Preserved relevant prefix commands and aliases.
- Fixed development guild synchronization.
- Recorded commits `a9375b3` and `9a64ce1`.
- User reported all five slash commands worked in beta.
- Next: close out/integrate the pilot, then P2.1.

## Resume checklist

At the start of a new or compacted task:

1. Read `AGENTS.md` and this entire document.
2. Run `pwd`, `git status --short --branch`, and inspect recent commits.
3. Confirm the current phase and that no other unit is marked **In progress**.
4. Reinspect the target command and model code; line numbers in old summaries
   may be stale.
5. Reverify runtime configuration before any database or Discord operation.
6. State the exact unit being started and its exit criteria.
7. Update the work-unit record as evidence is produced.

At the end of every unit:

1. Update phase/unit status.
2. Record files changed and important design decisions.
3. Record exact tests and results, including skipped gated tests.
4. Record commit hashes, PR/merge state, and beta acceptance if applicable.
5. Record limitations and the single recommended next action.
6. Ensure the progress log has a dated entry.

