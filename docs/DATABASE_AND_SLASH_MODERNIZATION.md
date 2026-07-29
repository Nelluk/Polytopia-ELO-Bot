# Database Access and Slash Command Modernization

Last updated: 2026-07-29

Status: Active

Current branch at last update: `codex/p2-1-newgame-worker`

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

The slash/ELO pilot is beta-validated, locally green, and established as the
base of the modernization accumulation branch. At the latest repository
check:

- accumulation branch: `codex/database-slash-modernization`
- preserved pilot checkpoint branch: `codex/slash-async-unwin-pilot`
- implementation checkpoint: `a9375b3`
- development-guild sync fix: `9a64ce1`
- initial roadmap checkpoint: `8593183`
- repeated-cancellation cleanup fix: `46e053e`
- P2.1 implementation checkpoint: `0594629`
- P2.1 accumulation merge: `ecdd01e`
- P2.2 implementation checkpoints: `25b9d50`, `8c350c1`
- beta acceptance: all five application commands reported working
- task-owned beta process: stopped cleanly; the foreground session exited and
  a follow-up session poll confirmed it was gone
- complete offline suite: 85 tests passed, with seven gated database tests
  skipped as designed
- gated `polytopia_dev` suite: seven tests passed under the required
  `development` / `polytopia_dev` / `polybot_dev` checks
- live-test game fixture: game `61` was deleted successfully
- optional cleanup: unused `Team.id=9`, `Phase7 Test Team`, remains in
  `polytopia_dev` with zero players and zero game sides

Current unit: **P2.2 — Decide the native `newgame` command UX**, Implemented
on `codex/p2-2-newgame-slash-ux`, based on
`codex/database-slash-modernization` at `f7b1e3e`.

Runtime status is deliberately not recorded as fact here. Verify whether a
beta process is running before starting or stopping one.

## Branch integration strategy

`master` remains the production-ready baseline while modernization work
accumulates independently:

1. `codex/database-slash-modernization` is the integration target for the
   roadmap through P8.
2. Each bounded unit starts on a dedicated branch created from the current
   accumulation branch.
3. After its tests and any required beta acceptance pass, the unit is merged
   back into `codex/database-slash-modernization`, not `master`.
4. Preserve unit branches or commits as independently reviewable rollback
   points.
5. Bring relevant changes from `master` into the accumulation branch
   periodically so conflicts are discovered before P9. Do not rewrite shared
   accumulation history after it is published.
6. P9 performs the separately approved final review and integration from the
   accumulation branch into `master`, followed by separately approved
   production deployment.

Creating, pushing, merging, or deleting a branch still requires the authority
applicable to that task. This strategy does not authorize production work,
beta launches, command synchronization, pushes, or PR operations by itself.

## Phase summary

| Phase | Status | Scope | Exit checkpoint |
|---|---|---|---|
| P0 | Complete | Serialized ELO workers and first five slash commands | Commits `a9375b3`, `9a64ce1`; live beta acceptance; accumulation-branch base |
| P1 | Complete | Close out and establish the pilot on the accumulation branch | Clean tests, beta stopped, reviewed branch, local accumulation branch |
| P2 | In progress | Fix known game-creation transaction boundary | `newgame` workflow atomic and Discord effects post-commit |
| P3 | Planned | Owner ELO maintenance and job observability | Typed slash maintenance interface and active-job status |
| P4 | Planned | Game correction and metadata mutations | Bounded workers plus slash interfaces for clear typed operations |
| P5 | Planned | Matchmaking lifecycle | Atomic open/join/leave/kick/start flows and native interactions |
| P6 | Planned | Registration and player preferences | Worker-safe profile writes and slash UX |
| P7 | Planned | Read-heavy game, player, and leaderboard commands | Bounded read path and responsive slash queries |
| P8 | Planned | League and remaining administration workflows | Audited domain workers and selected native interfaces |
| P9 | Planned | Production rollout and later prefix deprecation decision | Approved deployment, monitoring, and separate deprecation plan |

## P0 — Serialized ELO and slash-command pilot

Status: **Complete**

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
- The modernization accumulation branch is intentionally not merged into
  `master`; production integration remains P9.
- The unused development fixture `Team.id=9` remains pending an explicit
  cleanup decision.
- Prefix interfaces remain required.

## P1 — Pilot close-out and integration

Status: **Complete**

Integration result: `codex/database-slash-modernization` was created at the
validated pilot HEAD. `codex/slash-async-unwin-pilot` remains unchanged as the
pilot checkpoint. For this roadmap, the accumulation branch—not `master`—is
the intended integration target until P9.

Objective: finish the pilot as a reviewable checkpoint before adding another
large command group.

Work units:

- [x] Reverify the beta bot is stopped; if it is still running, obtain the
  required authority and stop only the development process cleanly.
- [x] Review both pilot commits for permission, transaction, cancellation, and
  command-sync regressions.
- [x] Run `git diff --check`.
- [x] Run the complete offline suite.
- [x] Run the gated development-database suite only through its safety gates.
- [x] Record any live-test fixture cleanup required in `polytopia_dev`.
- [x] Establish the reviewed pilot as the base of
  `codex/database-slash-modernization`.
- [x] Preserve `codex/slash-async-unwin-pilot` as the pilot checkpoint.
- [x] Record the integration result in the progress log.

Review evidence:

- Worker functions open a worker-local Peewee connection, reload games from
  primitive IDs, and keep all `db.atomic()` scopes synchronous.
- Win, unwin, confirmation, and completed-game deletion perform Discord
  channel/message effects only after the worker transaction returns.
- The single coordinator promptly rejects conflicting ELO jobs and clears
  state after success or exceptions.
- P1 hardened repeated cancellation so the coordinator remains reserved until
  the underlying non-cancellable worker actually finishes; focused
  coordinator tests pass 4/4.
- Prefix/slash permission paths preserve participant, staff, moderator, and
  guild checks. Registration/defer/denial tests remain green, and all five
  commands passed live beta acceptance.
- Development command synchronization copies global definitions only into
  guild `478571892832206869`; no global or production synchronization was
  performed.
- `git diff --check` is clean after removing the roadmap's extra EOF blank
  line.

Validation evidence:

- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest
  tests.test_elo_jobs.EloJobCoordinatorTests -v`: 4 passed.
- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest discover -v`: 77 passed, 5 skipped because the
  explicit database gate was not enabled.
- `POLYBOT_ENV=development POLYBOT_RUN_DB_INTEGRATION=1
  MPLCONFIGDIR=/tmp/polybot-matplotlib .venv/bin/python -m unittest
  tests.test_database_integration -v`: 5 passed after confirming database
  `polytopia_dev` and role `polybot_dev`.

Remaining limitations:

- Some short pre-worker validation and post-commit model reloads remain
  synchronous on the event-loop thread; long mutation/recalculation work is
  isolated as intended for the pilot.
- `recalc_games_from` remains owner-only and prefix-only pending P3.
- The unused `Phase7 Test Team` development fixture has not been deleted.

Next action: create a P2.1 unit branch from
`codex/database-slash-modernization`, implement the `newgame` transaction
separation, and merge that reviewed unit back into the accumulation branch.
Do not merge the accumulation branch into `master` before P9 approval.

Exit criteria:

- no beta process unintentionally left running;
- clean worktree apart from intentional planning-document updates;
- required tests green;
- pilot changes reviewed and established on the intended accumulation branch.

## P2 — Game creation transaction boundary

Status: **In progress**

Why this phase is next:

`modules/games.py:newgame` currently performs Discord sends inside a
`db.atomic()` block. It also mixes Discord member resolution, database
creation, host assignment, logging, warnings, and post-create messaging.

### P2.1 — Extract the database workflow

Status: **Complete**

Branch/base: `codex/p2-1-newgame-worker` from
`codex/database-slash-modernization` at `b992b7f`.

Objective: move the complete `newgame` database creation workflow into one
bounded synchronous worker transaction and keep all Discord effects
post-commit.

In scope:

- Preserve existing prefix parsing, aliases, permissions, and game rules.
- Capture primitive guild/requester/participant data on the event-loop thread.
- Reload or create model records inside a worker-local connection.
- Atomically create the game, sides, lineups, host assignment, and audit log.
- Return immutable primitive result data and warnings.
- Add focused rollback, connection, responsiveness, and post-commit tests.

Out of scope:

- Matchmaking `open`/`join`/`start` workflows.
- A general async ORM migration or universal worker framework.
- Production or beta operations.

Database boundary: the worker owns its Peewee connection and complete
transaction. No Discord object, live Peewee model, or lazy query crosses into
the worker.

Slash decision: deferred to P2.2 because the prefix command's aliases and
flexible multi-side grammar require a separate native UX decision. P2.1 adds
no application command and preserves all prefix interfaces.

Permissions to preserve: existing registration, bot-channel, per-alias,
participant, team, ranked, platform, and moderator-override behavior.

Discord effects after commit: warning messages, game announcements, embeds,
and any channel/role effects run only after a successful worker result.

Files expected: `modules/games.py`, a bounded game-creation worker module,
focused tests, and this roadmap. Modify `modules/models.py` only if a narrow
extraction is required to preserve model behavior.

Tests required:

- [x] focused offline
- [x] rollback/fault injection
- [x] event-loop responsiveness
- [x] primitive worker inputs and worker-local connection
- [x] prefix registration/aliases/behavior
- [x] no Discord effects after database failure
- [x] complete offline suite
- [x] gated development-database suite

Approvals required: gated development-database tests may use their existing
safety gate. Beta launch, Discord synchronization, dependency changes, push,
merge, and production work require separate approval.

Implementation evidence:

- `NewGameRequest` and `NewGameParticipant` are frozen primitive snapshots;
  no Discord or Peewee object crosses into the worker.
- A dedicated one-thread `polybot-newgame` executor bounds and serializes game
  creation separately from the ELO executor.
- The worker opens its Peewee connection, rebuilds a worker-local member view,
  and runs `Game.create_game`, host assignment, and `GameLog.write` inside one
  outer synchronous transaction.
- The prefix command awaits the worker result before sending worker warnings,
  loading the committed game for display, or invoking
  `post_newgame_messaging`.
- Fault injection proves an audit-log failure rolls back game, host, and log
  state and closes the worker connection.
- Command-level failure injection proves database failure does not load the
  game or invoke announcement/channel effects.
- The simulated slow worker leaves an unrelated event-loop heartbeat
  responsive.
- Prefix command name and aliases (`newgameunranked`, `newsteamgame`, and
  `newsteamgameunranked`) are preserved; no slash command was added.
- Gated PostgreSQL tests prove a complete real game/side/lineup/host/log graph
  and separately prove worker-thread rollback after graph creation. Both
  paths leave no test rows.

Commit(s):

- `0594629` — Move newgame creation into bounded worker.
- `ca4fb59` — Record P2.1 newgame worker evidence.
- `ecdd01e` — Merge P2.1 newgame worker into
  `codex/database-slash-modernization`.

Beta result: not required for this internal boundary unit because no
application command or intended Discord behavior changed. No beta session was
launched or authorized.

Remaining limitations:

- Discord member resolution and short permission/format validation remain on
  the event-loop thread.
- The post-commit `Game.load_full_game` and legacy messaging helpers still
  perform some synchronous model reads/writes on the event-loop thread.
- Announcement message/channel IDs are reconciliation metadata written after
  the Discord send; broader post-effect reconciliation is not part of P2.1.
- Cancellation after worker submission can allow the synchronous transaction
  to finish without running later Discord effects; this is recorded for a
  future ordinary-game job lifecycle design rather than introducing a general
  coordinator in the first non-ELO unit.

Validation evidence:

- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest tests.test_newgame_worker -v`: 6 passed.
- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest discover -v`: 85 passed, 7 skipped because the
  explicit database gate was not enabled.
- `POLYBOT_ENV=development POLYBOT_RUN_DB_INTEGRATION=1
  MPLCONFIGDIR=/tmp/polybot-matplotlib .venv/bin/python -m unittest
  tests.test_database_integration -v`: 7 passed after confirming database
  `polytopia_dev` and role `polybot_dev`.
- `git diff --check`: clean.

Next action: create a dedicated P2.2 branch from accumulation merge
`ecdd01e`, select and implement a bounded native `newgame` UX, and preserve
the flexible prefix interface.

### P2.2 — Decide the native command UX

Status: **Implemented**

Branch/base: `codex/p2-2-newgame-slash-ux` from
`codex/database-slash-modernization` at `f7b1e3e`.

Prefix parity includes quoted names, aliases that select ranked/platform
variants, shortcuts that infer the author, and flexible multi-side player
lists. Do not expose that grammar as one opaque slash string without review.

Evaluate:

- a typed `/newgame` with name, ranked/platform choices, and bounded side
  options; or
- a slash command plus modal for flexible side entry; or
- a documented temporary slash deferral if neither preserves existing game
  shapes safely.

Selected interface: a typed top-level `/newgame` with explicit Discord member
selectors for two sides, optional slots through 4v4, a ranked Boolean, and a
Mobile/Steam platform choice. This covers common game shapes without opaque
member text parsing. The existing prefix command and aliases remain available
for larger games, more than two sides, and the one-opponent author shortcut.
Both interfaces feed the same prefix validation and P2.1 worker/post-commit
pipeline so permission and transaction behavior do not fork.

The database extraction proceeds even if slash UX is deferred. Record the
decision and reason in the decision log.

Implementation evidence:

- `/newgame` is guild-only and uses typed Discord member selectors for two
  required players plus optional slots through 4v4.
- Ranked is a Boolean option and platform is a Mobile/Steam choice; these map
  to the existing four prefix variants before shared validation executes.
- The interaction defers before checks, member adaptation, or worker
  submission.
- `Command.can_run` executes the prefix command's existing registration,
  configured bot-channel, cog, and global checks for the synthetic
  interaction context.
- The slash adapter then calls the same prefix callback, P2.1 worker, and
  post-commit Discord path. Prefix command name and all three aliases remain
  registered.
- The synthetic context uses the guild's configured prefix in compatibility
  messages and embeds. Native one-word-name failures do not incorrectly tell
  slash users to add quotation marks.

Tests required:

- [x] focused slash registration and typed option shape
- [x] immediate defer before shared checks/pipeline
- [x] check failure stops before creation
- [x] prefix registration and aliases preserved
- [x] P2.1 rollback, responsiveness, and post-commit ordering remain green
- [x] complete offline suite
- [x] gated development-database suite
- [ ] approved beta synchronization and smoke test

Commit(s):

- `25b9d50` — Add typed slash interface for newgame.
- `8c350c1` — Polish newgame slash compatibility.

Beta result: pending separate approval. No beta process was launched and no
Discord command synchronization was performed in this unit.

Remaining limitations:

- Native creation is intentionally limited to two sides and four players per
  side. Larger games, games with more than two sides, and the prefix
  one-opponent shortcut remain available through the prefix command.
- The adapter deliberately reuses the prefix callback during migration rather
  than duplicating its validation rules. A later cleanup may extract the
  shared application service once another caller demonstrates the useful
  boundary.
- P2.1's recorded post-commit synchronous model reads/writes and cancellation
  limitation remain unchanged.

Next action: with separate approval, launch the development beta profile and
synchronize the development guild, then smoke-test `/newgame` for ranked
Mobile 1v1, unranked Steam 2v2, participant/staff permissions, configured
channel enforcement, failure messaging, and preserved prefix aliases. Record
and clean up any created development-database games and Discord channels.

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

### D-009 — Accumulate modernization outside `master` until P9

Status: Accepted

`codex/database-slash-modernization` is the intended integration branch for
P0 through P8. Unit branches are based on it and merge back into it after
their own validation. `master` remains the production-ready baseline until a
separately approved P9 integration, avoiding reliance on manually withholding
a production pull or restart after every development unit.

### D-010 — Separate `newgame` database work before slash UX

Status: Accepted

P2.1 preserves the prefix-only `newgame` interface and moves its database
creation workflow first. Alias-driven ranked/platform behavior, the author
shortcut, and flexible multi-side grammar require the dedicated P2.2 native
UX decision rather than an opaque or hastily designed slash string.

### D-011 — Use a bounded typed `/newgame` plus prefix fallback

Status: Accepted

The native command supports common two-sided games through 4v4 with Discord
member selectors and explicit ranked/platform options. The existing prefix
grammar remains the supported path for larger or multi-side games. A modal
was rejected because free-text member entry would discard Discord's native
member selection and recreate the ambiguity of prefix parsing.

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

### 2026-07-29 — P1 local close-out complete

- Stopped the task-owned development beta process cleanly; it was not
  relaunched and no command synchronization was performed.
- Reviewed transaction/connection boundaries, post-commit Discord effects,
  coordinator cleanup, permission parity, and development-guild isolation.
- Hardened repeated cancellation cleanup in commit `46e053e`.
- Passed the four focused coordinator tests, the complete 77-test offline
  suite (five gated skips), and all five explicitly gated development-database
  tests.
- Confirmed live-test game `61` was deleted. Recorded unused
  `Team.id=9` / `Phase7 Test Team` as optional cleanup.
- P1 was initially blocked on choosing an integration target. P2.1 `newgame`
  transaction separation remained the next proposed code unit.

### 2026-07-29 — Accumulation branch strategy adopted

- Created local branch `codex/database-slash-modernization` at validated P1
  checkpoint `a43b4d3`.
- Preserved `codex/slash-async-unwin-pilot` unchanged at `a43b4d3`.
- Designated the accumulation branch as the integration target through P8;
  `master` remains the production-ready baseline until P9.
- Marked P0 and P1 Complete on the intended accumulation branch.
- No push, PR, beta launch, command synchronization, or production operation
  was performed.
- Next: create a dedicated P2.1 unit branch from the accumulation branch when
  implementation is authorized.

### 2026-07-29 — P2.1 newgame worker implemented

- Created `codex/p2-1-newgame-worker` from accumulation checkpoint `b992b7f`.
- Added immutable participant/request snapshots and a bounded, dedicated
  one-thread creation executor.
- Moved game, sides, lineups, host assignment, and audit logging into one
  worker-local synchronous transaction.
- Preserved the prefix command and all ranked/platform aliases; deferred slash
  UX to P2.2.
- Added rollback, connection, primitive-boundary, responsiveness,
  post-commit-ordering, and prefix registration tests.
- Passed 85 offline tests with seven gated skips and all seven explicitly
  gated development-database tests.
- Recorded implementation commit `0594629`.
- No beta, command synchronization, production, dependency, or schema action
  was performed.
- Integrated P2.1 into `codex/database-slash-modernization` with merge commit
  `ecdd01e`; the unit is Complete.
- Next: create the P2.2 unit branch from `ecdd01e` and implement the selected
  native command UX separately.

### 2026-07-29 — P2.2 typed newgame slash UX implemented

- Created `codex/p2-2-newgame-slash-ux` from accumulation checkpoint
  `f7b1e3e`.
- Added a guild-only typed `/newgame` for two-sided games through 4v4 with
  ranked and platform options.
- Reused the prefix checks, validation, P2.1 worker, and post-commit effects;
  preserved `newgame` and its three aliases.
- Added registration, option-shape, defer-order, check-failure, mapping, and
  configured-prefix tests.
- Passed nine focused tests, 88 offline tests with seven gated skips, and all
  seven explicitly gated development-database tests.
- Recorded implementation commits `25b9d50` and `8c350c1`.
- No beta launch, command synchronization, production, dependency, or schema
  action was performed.
- Next: obtain separate beta launch/synchronization approval, run the P2.2
  smoke matrix, clean up fixtures, and integrate the accepted unit into the
  accumulation branch.

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
