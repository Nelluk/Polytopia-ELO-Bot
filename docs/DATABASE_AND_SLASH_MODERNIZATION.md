# Database Access and Slash Command Modernization

Last updated: 2026-07-29

Status: Active

Current branch at last update: `codex/p4-1c-unstart-separation`

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
   Successful competitive-state mutations should generally be public so the
   native interface preserves the transparency of the corresponding prefix
   command. Deviations require a recorded privacy or safety reason.
7. Add registration tests for both interfaces.
8. Record any native-interface compromise in the compatibility ledger below,
   including user impact, acceptance, and a possible mitigation. Do not let
   parity gaps live only in task commentary.

Do not rename the five beta-validated pilot slash commands without a separate
compatibility and deprecation decision.

### Slash taxonomy review

Status: **Awaiting staff vote**

This review is a naming and information-architecture gate, not authority to
change registrations. No additional slash command should be finalized after
P4.1b until one taxonomy is selected. Prefix names and aliases remain
unchanged under every proposal.

The repository-wide inventory and conversion dispositions are maintained in
`docs/SLASH_COMMAND_TAXONOMY_REVIEW.md`. It covers all 83 in-scope explicit prefix
handlers, the customized help command, optional command families, commands
that need interaction redesign, and commands that should remain
operator-only. The legacy API cog and its seven hidden commands are excluded.
The table below is only the current native surface being renamed; it is not
the complete modernization inventory.

The current native surface under review is:

- `/newgame`, `/win`, `/unwin`, `/delete`, `/confirm`, `/unconfirmed`;
- `/set-ranked`, `/extend`;
- `/recalc-games-from`, `/elo-job-status`.

`/extend` is present in the uncommitted P4.1b worktree and has not yet been
beta-synchronized. The first nine commands have already been synchronized
during approved development-guild sessions.

#### T-A — Domain groups (recommended)

Organize commands by the object or workflow they affect:

| Current native name | Proposed name |
|---|---|
| `/newgame` | `/game create` |
| `/win` | `/game win` |
| `/unwin` | `/game unwin` |
| `/delete` | `/game delete` |
| `/confirm` | `/game confirm` |
| `/unconfirmed` | `/game unconfirmed` |
| `/set-ranked` | `/game set-ranked` |
| `/extend` | `/match extend` |
| `/recalc-games-from` | `/elo recalculate` |
| `/elo-job-status` | `/elo status` |

Future families would use `/game show|rename|set-map|set-tribe|notes|logs`,
`/match open|join|leave|kick|start|list`,
`/player show|set-name|set-time|names`, and
`/leaderboard players|teams|squads`.

Advantages: the vocabulary follows user-facing domain concepts; ranked and
unranked games remain under `/game`; matchmaking has a clear lifecycle; ELO
is reserved for rating-specific maintenance and reporting. It scales without
turning one root into a miscellaneous drawer.

Costs: every current native name changes; `/game` becomes a group, so viewing
a game is `/game show` rather than bare `/game`; grouped wrappers must be
separate from the preserved prefix decorators.

#### T-B — One ELO-branded umbrella

Treat the bot as one ELO application and put nearly all native commands under
one root:

| Current native name | Proposed name |
|---|---|
| `/newgame` | `/elo game create` |
| `/win` | `/elo game win` |
| `/unwin` | `/elo game unwin` |
| `/delete` | `/elo game delete` |
| `/confirm` | `/elo game confirm` |
| `/unconfirmed` | `/elo game unconfirmed` |
| `/set-ranked` | `/elo game set-ranked` |
| `/extend` | `/elo match extend` |
| `/recalc-games-from` | `/elo admin recalculate` |
| `/elo-job-status` | `/elo admin status` |

Future subcommand groups would include `game`, `match`, `player`,
`leaderboard`, `team`, and `admin`. Discord supports this root/group/command
shape, such as `/elo game unwin`.

Advantages: users learn one root; autocomplete after `/elo` exposes the
available families; the application has a strong branded identity.

Costs: unranked games, registration, matchmaking, and league operations are
not naturally ELO concepts; the root can become crowded; Discord permits only
one subcommand-group level, so deeper organization is unavailable. The
meaning of `/elo` becomes “everything PolyBot does,” not specifically rating.

If staff prefer this structure but dislike the semantic mismatch, `/poly` or
`/bot` can replace `/elo` without changing the rest of the proposal.

#### T-C — Conservative flat commands

Keep the beta-tested names and use consistent prefixes only for future
additions:

| Existing names retained | Example future names |
|---|---|
| `/newgame`, `/win`, `/unwin`, `/delete` | `/game-info`, `/game-rename`, `/game-set-map` |
| `/confirm`, `/unconfirmed`, `/set-ranked`, `/extend` | `/match-open`, `/match-join`, `/match-start` |
| `/recalc-games-from`, `/elo-job-status` | `/player-info`, `/player-set-name`, `/leaderboard` |

Advantages: least migration work and no relearning of the already-tested
surface; hybrid commands can remain hybrids; each command is directly visible
at the top level.

Costs: the command picker becomes a long alphabetical list; related operations
are scattered; naming conventions remain partly historical (`newgame` versus
hyphenated names); later cleanup becomes harder after production adoption.

#### Vote and transition rules

Staff should vote on the taxonomy, not individual spellings. After a winner is
selected, perform a short spelling review for terms such as `create` versus
`new`, `unconfirmed` versus `pending-confirmation`, and `match` versus
`lobby`.

Recommended ballot:

1. T-A — domain groups;
2. T-B — one umbrella (with a second choice of `/elo`, `/poly`, or `/bot`);
3. T-C — conservative flat commands.

Use ranked-choice voting if practical. Include “no preference” rather than
forcing a random ranking. The implementation decision should record the vote
result and selected spellings before registrations change.

Because these commands have not reached production, the cleanest transition
is one coordinated beta rename followed by a development-guild sync and
smoke test. If staff need an adjustment period, old top-level slash names may
remain as explicitly deprecated wrappers for one beta cycle. Discord does not
redirect renamed commands, so every retained alias is a separately registered
application command. Do not carry transitional aliases into production
without a separate decision.

## Slash compatibility compromise ledger

This is the running record of behavior that a native interaction does not
cover. A gap may be accepted when the affected command is rare or the native
path covers normal usage. Prefix availability during the transition means a
listed gap is not necessarily a current loss; the **message-intent impact**
column states what would become unavailable if prefix processing could no
longer be retained.

| ID / command | Native coverage | Accepted compromise and message-intent impact | Possible future mitigation | Status |
|---|---|---|---|---|
| C-001 `/newgame` | Typed two-sided games from 1v1 through 4v4; explicit ranked and Mobile/Steam options; requester is selected explicitly when participating | Native slash does not cover more than two sides, more than four players per side, or the one-opponent shortcut that infers the requester. Those shapes/conveniences currently remain on the prefix command and would be unavailable if message-content intent were retired. This is accepted for the initial conversion: `newgame` is rare in practice and normal usage is overwhelmingly even, two-sided games. Ranked/platform alias behavior is preserved as slash options. | If actual demand warrants it, add an interaction-only `/game custom` draft: modal for name, buttons to add/edit/remove sides, Discord user-select components to fill each side, review/validation, then explicit confirmation into the existing worker transaction. A short-lived in-memory draft is sufficient initially; persistence can be added only if restart survival matters. | Accepted temporary gap; not a P2.2 blocker |

Every later slash conversion must add a row when parity is intentionally
reduced. If there is no compromise, its unit evidence should explicitly say
so rather than adding an empty ledger row.

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
- P2.2 sync-evidence checkpoint: `5bb67c2`
- P2.2 accumulation merge: `2b77f13`
- P3.1 implementation checkpoint: `1bebce6`
- P3.1 accumulation merge: `5f62998`
- P3.2 implementation checkpoint: `63c9378`
- P3.2 accumulation merge: `41bd614`
- P4.1a implementation checkpoints: `3e1f395`, `d2526b4`
- P4.1a visibility checkpoint: `2cba1cc`
- P4.1a accumulation merge: `5888c02`
- P4.1b implementation checkpoint: `c0945a3`
- P4.1c implementation checkpoint: `204ab40`
- T1 fixture-harness implementation checkpoint: `4551bec`
- T1 roadmap-evidence checkpoint: `d6e826b`
- T1 accumulation merge: `aacace4`
- pilot beta acceptance: all five original application commands reported
  working
- combined development sync: all eight expected commands synchronized to
  guild `478571892832206869`; P2.2 and P3.1 accepted by the user
- complete offline suite: 123 tests passed, with eight gated database tests
  skipped as designed
- gated `polytopia_dev` suite: eight tests passed under the required
  `development` / `polytopia_dev` / `polybot_dev` checks
- live-test game fixture: game `61` was deleted successfully
- optional cleanup: unused `Team.id=9`, `Phase7 Test Team`, remains in
  `polytopia_dev` with zero players and zero game sides

Current unit: **P4.1c — Unstart transaction separation**, Implemented on
`codex/p4-1c-unstart-separation`, stacked from the P4.1b evidence checkpoint
`af4ef51`. P0 through P3 and T1 are Complete on the intended accumulation
branch.

Owned fixture games `149`-`151` are intentionally retained. At the latest
gated status check, `149` is incomplete/unranked, `150` is
unconfirmed/ranked, and `151` is confirmed/ranked. Inspect them before reuse.
Interactive `/newgame` game `118` (`Foobar`) also remains an unowned manual
fixture whose state must be inspected before reuse.

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
| P2 | Complete | Fix known game-creation transaction boundary | `newgame` workflow atomic and Discord effects post-commit |
| P3 | Complete | Owner ELO maintenance and job observability | Typed slash maintenance interface and active-job status |
| T1 | Complete | Deterministic development beta fixtures | Gated, idempotent seed/status/cleanup tooling |
| P4 | In progress | Game correction and metadata mutations | Bounded workers plus slash interfaces for clear typed operations |
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

Status: **Complete**

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

Status: **Complete**

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
- [x] approved beta synchronization and smoke test

Commit(s):

- `25b9d50` — Add typed slash interface for newgame.
- `8c350c1` — Polish newgame slash compatibility.
- `2b77f13` — Merge P2.2 into `codex/database-slash-modernization`.

Beta result: launch and development-guild synchronization approved and
performed from checkpoint `89e5710` on 2026-07-29. The runtime preflight
selected environment `development`, beta application `479029527553638401`,
database `polytopia_dev`, development guild `478571892832206869`, and disabled
background tasks, API, and Bullet integration. Discord authenticated the
expected beta bot and synchronized six guild commands: `win`, `unwin`,
`delete`, `newgame`, `confirm`, and `unconfirmed`. At that checkpoint,
functional `/newgame` acceptance was still pending, and the task-owned beta
session was stopped cleanly so P2.2 acceptance could be combined with P3.1 in
one later beta launch.

Combined beta result: accepted by the user on 2026-07-29 from
`codex/dev-beta-fixture-harness`. Startup synchronized all eight expected
commands. Development logs confirm live `/newgame` creation of ranked Mobile
game `118`; the user reported the tested command behavior looked correct.
Unranked Steam creation and optional 2v2 were not separately evidenced in the
logs, but their option mapping and shape remain covered offline. Game `118`
is ordinary/unowned and is intentionally retained for later development
testing.

Remaining limitations:

- Native creation is intentionally limited to two sides and four players per
  side. Larger games, games with more than two sides, and the prefix
  one-opponent shortcut remain available through the prefix command. This
  accepted initial-conversion compromise is tracked as C-001 in the slash
  compatibility compromise ledger.
- The adapter deliberately reuses the prefix callback during migration rather
  than duplicating its validation rules. A later cleanup may extract the
  shared application service once another caller demonstrates the useful
  boundary.
- P2.1's recorded post-commit synchronous model reads/writes and cancellation
  limitation remain unchanged.

Next action: P2 is closed on the accumulation branch. Preserve C-001 as the
accepted native-interface limitation, and inspect retained manual fixture
`118` before any later reuse.

Exit criteria:

- no Discord await inside the creation transaction;
- worker-local connection and primitive inputs;
- transaction and post-commit fault tests;
- prefix behavior preserved;
- slash decision implemented or explicitly deferred;
- approved beta test if an application command is added.

## P3 — Owner ELO maintenance and observability

Status: **Complete**

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

### P3.1 — Recalculation control and active-job status

Status: **Complete**

Branch/base: `codex/p3-1-elo-maintenance-ux`, stacked from
`codex/p2-2-newgame-slash-ux` at `4a7fba6`. P2.2 and P3.1 are
Beta-validated from the same combined session; this does not mark either unit
Complete or merge either unit implicitly.

Objective: add a confirmed, owner-only native entry point for the existing
serialized recalculation worker and expose useful active-job state to staff.

In scope:

- Preserve owner-only prefix `recalc_games_from`.
- Add owner-only `/recalc-games-from game_id confirm`.
- Require an affirmative confirmation before worker submission.
- Defer immediately for confirmed recalculation.
- Add staff-visible, ephemeral `/elo-job-status`.
- Report operation, game, requester, start time, and elapsed time.
- Reuse the existing ELO coordinator and `recalculate_games_from` worker.

Out of scope:

- Cancellation or forced termination of a running thread.
- A new coordinator or executor.
- Changes to ELO calculation rules.
- Activating or exposing `reverse_duplicated_elo`.

Slash decision: both interfaces have clear typed models. Recalculation gains a
required confirmation Boolean; job status is a slash-only read interface.
No native parity compromise is expected, so no compatibility-ledger row is
planned unless implementation review discovers one.

Tests required:

- [x] prefix registration and owner permission preserved
- [x] slash registration and option types
- [x] non-owner and unconfirmed requests rejected before defer/submission
- [x] confirmed request defers before coordinator submission
- [x] conflict and worker error responses
- [x] active and idle status formatting and staff permissions
- [x] recalculation worker connection, commit, and rollback behavior
- [x] complete offline suite
- [x] gated development-database suite
- [x] combined P2.2/P3.1 beta smoke test

Implementation evidence:

- Prefix `recalc_games_from` remains hidden and owner-checked.
- Prefix and slash paths call one `_run_recalculation_job` entry point using
  the existing ELO coordinator and synchronous worker.
- Prefix validation no longer loads a Peewee game on the event-loop thread;
  the worker reloads and validates the primitive game ID inside its local
  connection and transaction.
- `/recalc-games-from` requires typed integer `game_id` and required Boolean
  `confirm`; non-owner and false-confirmation requests return ephemerally
  without deferring or submitting work.
- Confirmed slash requests defer ephemerally before coordinator submission and
  report conflicts, validation failures, database rollback, or success only
  after the worker returns.
- `/elo-job-status` is staff-visible, read-only, and ephemeral. It reports
  operation, game, requester, absolute/relative start time, and elapsed time
  from the coordinator's single source of truth.
- Focused tests prove the recalculation worker owns and closes its connection,
  commits success, rolls back failure, and accepts only a primitive game ID.
- No slash compatibility compromise was introduced: the native maintenance
  path adds explicit confirmation, and the existing owner prefix behavior is
  retained. No ledger row is required.

Files changed:

- `modules/administration.py`
- `tests/test_elo_jobs.py`
- `docs/DATABASE_AND_SLASH_MODERNIZATION.md`

Commit(s):

- `1bebce6` — Add ELO maintenance slash controls.
- `5f62998` — Merge P3.1 into `codex/database-slash-modernization`.

Validation evidence:

- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest tests.test_elo_jobs.EloWorkerTests
  tests.test_elo_jobs.HybridUnwinCommandTests -v`: 28 passed.
- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest discover -v`: 97 passed, seven gated database
  tests skipped as designed.
- `POLYBOT_ENV=development POLYBOT_RUN_DB_INTEGRATION=1
  MPLCONFIGDIR=/tmp/polybot-matplotlib .venv/bin/python -m unittest
  tests.test_database_integration -v`: seven passed after the gate confirmed
  `polytopia_dev` and role `polybot_dev`.
- `git diff --check`: clean.

Beta result: accepted by the user on 2026-07-29 from
`codex/dev-beta-fixture-harness`. Startup synchronized
`recalc-games-from` and `elo-job-status` with the six prior commands.
Development logs confirm recalculation from owned confirmed game `117`
completed. The user reported the tested status/confirmation behavior looked
correct. Active-conflict timing and non-owner denial were not independently
observable in the logs and remain covered by focused offline tests. The beta
process was stopped after testing.

Remaining limitations:

- Status is a current in-process snapshot; it does not persist job history.
- Running synchronous workers cannot be safely cancelled, so P3.1 exposes no
  abort command.
- Hidden unfinished `reverse_duplicated_elo` remains disabled and requires a
  separate retain/retire decision; it was not exposed as slash.

Next action: perform P3.2's bounded consistency review of command-line full
recalculation, cancellation semantics documentation, and the disabled
`reverse_duplicated_elo` command.

Exit criteria:

- prefix `recalc_games_from` preserved;
- typed permission-equivalent slash path;
- conflict and status behavior tested;
- beta acceptance covers defer, confirmation, conflict, and harmless-command
  responsiveness.

### P3.2 — ELO maintenance consistency review

Status: **Complete**

Branch/base: `codex/p3-2-elo-maintenance-consistency` from
`codex/database-slash-modernization` at `55425a5`.

Objective: close the remaining ELO-maintenance consistency questions without
expanding the pilot into a new command family.

In scope:

- Review the `bot.py --recalc_elo` full-recalculation path against the
  worker-local connection and synchronous transaction rules.
- Make cancellation semantics and the limits of non-cancellable synchronous
  workers explicit in operator/developer documentation.
- Decide whether the hidden, disabled `reverse_duplicated_elo` command should
  be retired or retained as an emergency-only operation; do not expose it as
  slash merely because it is reviewed.
- Add focused offline tests for any narrow consistency fix.

Out of scope:

- New ELO rules, a full async ORM migration, generalized job persistence, or
  live beta/Discord work unless the review produces a user-facing command
  change that warrants a separate approval.

Database boundary: `bot.py --recalc_elo` remains a standalone synchronous
operator mode. It now owns an explicit Peewee connection around the existing
coordinator claim and all-or-nothing model transaction. It does not use the
Discord executor because no Discord event loop exists in that process.

Slash decision: no command was added. The supported recalculation-from-game
workflow already has prefix and slash interfaces. The unfinished hidden
`reverse_duplicated_elo` prefix command was retired rather than modernized;
it always returned before its unsafe body and therefore provided no working
behavior to preserve. No slash compatibility compromise was introduced.

Implementation evidence:

- The full CLI recalculation opens and closes its process-local Peewee
  connection explicitly on both success and failure.
- The existing full-recalculation implementation retains one synchronous
  `db.atomic()` transaction and a guaranteed coordinator `claimed()` cleanup.
- This P3.2 record documents the supported repair paths, process-local
  coordination, non-cancellable worker behavior, shutdown expectations, and
  the retired command decision.
- `reverse_duplicated_elo` was removed. Its unreachable body bypassed the
  coordinator/transaction boundary and referenced `self.gamesides` on the cog
  instead of game sides.
- Registration testing proves the retired prefix command is absent while
  `recalc_games_from`, `/recalc-games-from`, and `/elo-job-status` remain.
- No beta process was running during the unit.

Files changed:

- `bot.py`
- `modules/administration.py`
- `tests/test_runtime_config.py`
- `tests/test_elo_jobs.py`
- `docs/DATABASE_AND_SLASH_MODERNIZATION.md`

Commit(s):

- `63c9378` — Harden ELO maintenance paths.
- `41bd614` — Merge P3.2 into `codex/database-slash-modernization`.

Tests required:

- [x] CLI connection lifecycle on success
- [x] CLI connection cleanup after recalculation failure
- [x] supported/retired command registration
- [x] complete offline suite
- [x] gated development-database suite

Validation evidence:

- Focused CLI/registration tests: three passed.
- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest discover -v`: 108 passed, eight gated
  database tests skipped as designed.
- `POLYBOT_ENV=development POLYBOT_RUN_DB_INTEGRATION=1
  MPLCONFIGDIR=/tmp/polybot-matplotlib .venv/bin/python -m unittest
  tests.test_database_integration -v`: eight passed after the gate confirmed
  `development`, `polytopia_dev`, and `polybot_dev`.
- `git diff --check`: clean.

Beta result: not required. No application command or working Discord behavior
changed, and no beta launch or synchronization was performed.

Remaining limitations:

- All ELO coordination is process-local. The bot and full CLI recalculation
  must not run concurrently against the same database.
- A running synchronous worker cannot be force-cancelled safely; status is
  in-memory and no job history is persisted.
- The full CLI flag retains its historical operator interface without an
  additional confirmation prompt. Runtime and operational approval gates
  remain the protection against accidental execution.

Operational guidance:

- Use owner-only `recalc_games_from` or `/recalc-games-from` for a bounded
  rebuild from one completed game; use `/elo-job-status` for the current
  in-process job snapshot.
- Cancellation of the awaiting Discord task does not stop synchronous Peewee
  work. The coordinator remains reserved until the worker finishes, and a
  successful transaction may commit after its awaiting task is cancelled.
- Allow an ELO worker to finish during shutdown. If hard termination is
  unavoidable, PostgreSQL determines whether the open transaction rolls back.
- `bot.py --recalc_elo` is a standalone full rebuild. Run it only while every
  bot process using that database is stopped because coordinators cannot
  serialize across processes.
- P3.2 retired `reverse_duplicated_elo`; supported repairs use recalculation
  from a game or the separately controlled full CLI rebuild.

Next action: begin P4.1a ranked-state correction as the first P4 code unit.

## T1 — Development beta fixture harness

Status: **Complete**

Branch/base: `codex/dev-beta-fixture-harness`, stacked from P3.1 checkpoint
`013bab2`.

Objective: make repeatable beta command testing practical using selected real
development-guild users without weakening database isolation or relying on
ad-hoc rows.

In scope:

- A separate repository-backed `seed`, `status`, and `cleanup` command.
- Hard refusal unless the runtime profile and live session both identify
  `development`, database `polytopia_dev`, and role `polybot_dev`.
- Primitive Discord user IDs supplied explicitly; existing `DiscordMember`
  and guild `Player` records are validated and reused, never fabricated or
  modified.
- Clearly named fixture games covering incomplete, unconfirmed, and confirmed
  ranked states useful for P2.2/P3.1 and pilot command testing.
- Idempotent discovery and narrowly owned cleanup with a required confirmation
  option.
- An ignored local manifest for operator visibility, with database ownership
  markers remaining authoritative if the manifest is stale or absent.

Out of scope:

- Discord login, beta launch, or command synchronization.
- Production data, schema changes, permanent default/reference data, or fake
  Discord identities.
- Automatic cleanup of arbitrary games that do not carry the fixture ownership
  marker.

Database boundary: each operation opens its own Peewee connection and keeps
seed/cleanup transactions synchronous. Ranked fixture cleanup uses the model's
existing ELO reversal/recalculation behavior before deleting fixture rows.

Slash decision: not applicable. This is an offline development operator tool,
not a Discord command.

Tests required:

- [x] focused offline safety, ownership, idempotency, and rollback tests
- [x] complete offline suite
- [x] gated development-database seed/status/cleanup round trip

Approvals required: implementation and gated development-database validation
are approved. Beta launch/sync, dependency changes, push, integration, and
production work remain separately gated.

Implementation evidence:

- `modules/dev_fixtures.py` owns exact profile/live-session gates, fixture
  validation, atomic scenario creation, state reporting, and synchronous
  cleanup using the existing ranked-game ELO reversal/recalculation path.
- `scripts/manage_dev_fixtures.py` imports database models only after the
  static development-profile gate and exposes `seed`, `status`, and
  explicitly confirmed `cleanup`.
- User inputs are primitive Discord IDs. Seed requires 2, 4, 6, or 8 unique
  users and reuses only existing guild `Player` records.
- Database ownership requires the exact notes marker, allowed guild, and
  fixture name prefix. Unknown or duplicate owned scenarios stop seeding;
  cleanup refuses any selected row that fails ownership validation.
- Seed is idempotent for one user set. The manifest is written atomically
  after commit and is ignored by Git; database markers remain authoritative.
- Focused fault injection proves seed rollback, connection cleanup, no
  manifest write after failure, cleanup confirmation before database access,
  and refusal/rollback when ownership validation fails.
- The gated PostgreSQL round trip created three scenarios twice, preserved
  their IDs on the second seed, reported them, removed them with ELO-aware
  cleanup, preserved an ordinary control game, and removed its temporary
  users.
- The first gated run exposed a test-finalizer generator bug after the fixture
  and control games had already been removed. Two verified zero-game
  temporary users remained; they were narrowly deleted. The corrected full
  gated suite then passed 8/8 with no test rows left.
- The operator fixture set was seeded after verification using the two real
  development players from prior beta testing. Repeating seed returned the
  same game IDs `115`, `116`, and `117`.

Commit(s):

- `4551bec` — Add gated development beta fixtures.
- `d6e826b` — Record fixture-harness roadmap evidence.
- `aacace4` — Merge T1 into `codex/database-slash-modernization`.

Beta result: not applicable; the harness prepares data but never connects to
Discord.

Remaining limitations:

- The CLI coordinator is process-local and cannot serialize against a running
  beta bot. Seed and cleanup must be run while the beta process is stopped.
- Interactive `/newgame` output is not automatically adopted; its game ID
  must be deleted through beta, recorded for explicit cleanup, or documented
  as an intentionally retained manual fixture.
- One owned fixture set is supported per development guild. Change users by
  cleaning the current set first.

Validation evidence:

- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest tests.test_dev_fixtures -v`: 8 passed.
- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest discover -v`: 106 passed, eight gated database
  tests skipped as designed.
- `POLYBOT_ENV=development POLYBOT_RUN_DB_INTEGRATION=1
  MPLCONFIGDIR=/tmp/polybot-matplotlib .venv/bin/python -m unittest
  tests.test_database_integration -v`: corrected suite passed 8/8 after the
  gate confirmed `polytopia_dev` and role `polybot_dev`.
- `git diff --check`: clean.

Updated beta procedure:

- `docs/DEVELOPMENT_BETA_FIXTURES.md` is the authoritative operator sequence.
- Run harness `status` before launch and only seed if the owned set is absent.
- Use the confirmed owned fixture as the recalculation target.
- Treat conflict observation as timing-dependent and skip it if recalculation
  finishes before a second interaction.
- Record every interactive `/newgame` ID; harness cleanup cannot remove it.
- Stop the beta before harness status/cleanup, confirm cleanup explicitly, and
  verify the owned set is empty afterward.

Combined beta/cleanup result:

- The user accepted the observed P2.2/P3.1 behavior.
- All eight expected application commands synchronized to the development
  guild.
- Owned game `117` successfully exercised recalculation.
- Owned games `115`-`117` were removed with confirmed harness cleanup while
  the beta was stopped.
- A final gated status check reports no owned fixtures.
- Interactive game `118` remains outside harness authority and is
  intentionally retained for later command testing.

Next action: retain the harness for future beta batches. Seed and cleanup only
while the beta is stopped and continue to inspect any retained manual game
separately from harness-owned fixtures.

## P4 — Game correction and metadata mutations

Status: **In progress**

Split this phase into small vertical units. Do not implement all candidates in
one commit.

### P4.1 — Staff state corrections

Candidates:

- `rankset`
- `rankunset`
- `unstart`
- `extend`

Start with P4.1a as a paired `rankset`/`rankunset` unit. They share one
staff-only ranked-state mutation and post-commit notification boundary.
Keep `unstart` separate because it deletes Discord channels and edits an
announcement; keep `extend` separate because it is a pending-game timer
operation with no ELO interaction.

#### P4.1a — Ranked-state correction

Status: **Complete**

Branch/base: `codex/p4-1a-ranked-state-correction` from
`codex/database-slash-modernization` at `f215bae`.

Commit(s):

- `3e1f395` — Modernize ranked state corrections.
- `d2526b4` — Complete ranked correction coverage.
- `2cba1cc` — Make ranked corrections publicly visible.
- `5888c02` — Merge P4.1a into
  `codex/database-slash-modernization`.

Implementation evidence:

- Prefix `rankset` and `rankunset` remain registered and staff-gated through
  the administration cog.
- New staff-only `/set-ranked game_id ranked` uses typed integer and Boolean
  options and defers before worker submission.
- One bounded ordinary-game worker opens its own connection, reloads by
  primitive game/guild IDs, revalidates incomplete and cross-guild state, and
  commits the flag plus audit log in one synchronous transaction.
- The existing per-game claim rejects overlapping mutation attempts.
- Squad-channel notification and completion output happen only after commit;
  fault injection proves database failure prevents those effects.
- No ELO coordinator is used because completed games are rejected.
- No slash compatibility compromise was introduced.

Validation evidence:

- `tests.test_ranked_state`: seven passed, covering commit, rollback,
  connection cleanup, event-loop responsiveness, registration, permission
  denial, defer ordering, and post-commit ordering.
- Complete offline suite: 115 passed with eight gated skips.
- Existing gated development-database suite: eight passed after confirming
  `development`, `polytopia_dev`, and `polybot_dev`.
- `git diff --check`: clean.

Beta result: accepted by the user on 2026-07-29. Startup synchronized all nine
expected commands to the development guild, including `/set-ranked`.
Fixture-backed ranked/unranked corrections and the preserved prefix commands
worked as expected. The test exposed that successful slash responses were
ephemeral; the user requested public success output for competitive
transparency and waived a second live retest. Permission, validation, and
database-error responses remain ephemeral.

Fixture result: the task-owned beta process stopped cleanly. The retained
owned set remains games `149`-`151`; game `149` is incomplete and unranked
after the smoke test, while `150` remains unconfirmed ranked and `151`
confirmed ranked. The set was intentionally retained for later units and
must be inspected before reuse.

Remaining limitations:

- The short post-commit game reload remains synchronous on the event-loop
  thread.
- `unstart` and `extend` remain unchanged for separate bounded units.

Next action: P4.1b pending-game extension. Preserve prefix `extend`, add a
typed native interface, and move timer validation/mutation into the bounded
ordinary-game worker.

#### P4.1b — Pending-game extension

Status: **Implemented**

Branch/base: `codex/p4-1b-pending-game-extension` from
`codex/database-slash-modernization` at `62ea671`.

Commit(s):

- `c0945a3` — Modernize pending game extension.

Objective: preserve the staff prefix timer extension while moving its
database mutation off the event loop and adding a transparent typed native
interface.

Database boundary:

- The bounded ordinary-game executor accepts primitive game/guild/requester
  data and opens a worker-local Peewee connection.
- The worker reloads and revalidates the game, computes the new deadline, and
  commits the expiration plus a new audit log in one synchronous transaction.
- A per-game claim covers the worker lifecycle. There are no Discord effects
  beyond the post-commit completion response and no model reload is needed
  after commit.

Slash decision: preserve prefix `$extend` and add staff-only
`/extend game_id`. A separate slash wrapper keeps the existing `PolyGame`
prefix converter intact while providing a typed integer option. Successful
output is public under D-017; denials, validation failures, lock conflicts,
and database errors are ephemeral. No compatibility-ledger gap was
introduced.

Implementation evidence:

- The legacy rule is preserved: a future deadline gains 24 hours from its
  existing value, while an expired deadline becomes 24 hours from execution.
- Worker validation rejects cross-guild and non-pending games before writes.
- Audit-log failure rolls back the expiration and closes the worker
  connection.
- A simulated slow worker leaves an unrelated event-loop timer responsive.
- Prefix and slash registration, staff denial before defer, public defer
  ordering, and typed option shape are covered offline.
- The shared executor was renamed from `polybot-newgame` to
  `polybot-game-write` because it now bounds multiple ordinary game-write
  workflows.

Files changed:

- `modules/game_workers.py`
- `modules/administration.py`
- `tests/test_game_extension.py`
- `docs/DATABASE_AND_SLASH_MODERNIZATION.md`

Validation evidence:

- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest tests.test_game_extension -v`: eight passed.
- Complete offline suite: 123 passed with eight gated database tests skipped
  as designed.
- Existing gated development-database suite: seven passed and the fixture
  round-trip test skipped itself to preserve operator-managed games
  `149`-`151`; the gate confirmed `development`, `polytopia_dev`, and
  `polybot_dev`.
- `git diff --check`: clean.

Beta result: pending. None of the retained owned fixtures is a known pending
matchmaking game. A later smoke test needs a safely created pending game, or
can be combined with P4.1c `unstart`, which converts the retained started
fixture into the state required by `/extend`.

Remaining limitations:

- Prefix `PolyGame` conversion remains a short synchronous event-loop lookup.
- Ordinary game writes share one bounded executor and therefore queue behind
  one another; Discord events and ELO jobs use separate execution paths.

Next action: commit P4.1b. Prefer stacking the separately bounded P4.1c
`unstart` unit for one combined beta session unless review finds its Discord
channel reconciliation too broad.

Use typed game IDs and choices. If these commands can race with finalized ELO
state, use the ELO coordinator; otherwise use a per-game claim rather than
blocking all ELO work.

#### P4.1c — Unstart transaction separation

Status: **Implemented**

Branch/base: `codex/p4-1c-unstart-separation`, stacked from P4.1b evidence
checkpoint `af4ef51`.

Commit(s):

- `204ab40` — Separate unstart database and Discord effects.

Objective: preserve the staff prefix workflow while committing the
started-to-pending transition before announcement edits or channel deletion,
and make partial Discord cleanup observable and reconcilable.

Database boundary:

- The ordinary-game worker accepts primitive game/guild/request data, opens a
  worker-local connection, reloads the game, revalidates guild and mutable
  state, and commits `is_pending`, the minimum 24-hour expiration, and the
  audit log in one synchronous transaction.
- The immutable result carries only primitive announcement IDs, mentions, and
  channel targets. No Peewee or Discord object crosses into the worker.
- Discord announcement/channel effects run only after commit. Successfully
  deleted channel IDs are cleared in a second bounded worker-local
  reconciliation transaction; failed or deliberately skipped deletions retain
  their database references and produce a visible warning.
- The existing per-game claim remains held through the database transition,
  Discord effects, and reconciliation. This operation does not interact with
  completed games or use the ELO coordinator.

Slash decision: preserve staff-only prefix `$unstart`. The workflow is ready
for a typed integer adapter, but registration is deferred under D-018's
taxonomy freeze. Under the recommended T-A taxonomy its intended home is
`/match unstart`; no top-level `/unstart` was registered. This is a naming
deferral rather than an accepted reduced-parity slash conversion, so it adds
no compatibility-ledger row.

Implementation evidence:

- Invocation from a game-associated channel remains rejected before worker
  submission, preserving the safety rule against deleting the command's own
  channel.
- Worker validation preserves completed/confirmed and already-pending
  rejection behavior and adds authoritative cross-guild rejection.
- The announcement still renders the legacy display-only
  `GAME CANCELLED` name without persisting that name to the database.
- `channels.delete_game_channel` now reports deletion success, treats an
  already-absent Discord channel as successfully gone, and reports archived,
  permission-blocked, or failed deletions as unsuccessful for reconciliation.
- Database-failure injection proves no announcement or channel effect runs.
  Ordering coverage proves the initial commit precedes Discord deletion and
  reconciliation follows only successful deletion.
- A simulated slow transition leaves an unrelated event-loop timer
  responsive.

Files changed:

- `modules/game_workers.py`
- `modules/administration.py`
- `modules/channels.py`
- `tests/test_game_unstart.py`
- `docs/DATABASE_AND_SLASH_MODERNIZATION.md`

Validation evidence:

- `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest tests.test_game_unstart -v`: nine passed.
- Complete offline suite: 132 passed with eight gated database tests skipped
  as designed.
- Existing gated development-database suite: seven passed and the fixture
  round-trip skipped itself to preserve the retained operator fixture set;
  the gate confirmed `development`, `polytopia_dev`, and `polybot_dev`.
- `git diff --check`: clean before this documentation update.

Beta result: pending. A combined session can test preserved `$unstart 149`
followed by `/extend 149`; `/unstart` itself will not synchronize while the
taxonomy vote remains unresolved. Inspect fixture 149 immediately before use
and record any announcement or channel resources affected.

Remaining limitations:

- Announcement rendering reloads the committed model synchronously on the
  event-loop thread before the Discord edit. The transition and all
  potentially material writes are bounded, but a later read/display DTO unit
  should remove this short post-commit model load.
- Cancellation after the synchronous transition commits can skip some or all
  Discord effects. Retained channel references make failed/skipped deletion
  discoverable, but no persistent reconciliation queue exists yet.
- The command remains prefix-only until D-018 is resolved.

Next action: review and commit this evidence while preserving the separate
taxonomy-document changes. Then obtain a staff taxonomy decision before
adding the `/match unstart` adapter or renaming the current beta slash
surface. P4.1b and P4.1c can still share one approved beta session for
`/extend` plus preserved `$unstart`.

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

### D-012 — Track accepted slash compromises centrally

Status: Accepted

Every slash-conversion unit records behavior omitted from the native path in
the slash compatibility compromise ledger. The record distinguishes a
temporary prefix-covered difference from functionality that would actually be
lost if message-content intent became unavailable. Accepted gaps do not block
a unit when normal usage is covered, but each retains a concrete mitigation
option and can be reprioritized from observed demand.

### D-013 — Batch adjacent slash units into one beta session

Status: Accepted for P2.2 and P3.1

P3.1 is stacked on the locally validated P2.2 checkpoint so the user can
launch and test both command additions in one beta session. The combined
session was accepted, after which the unit boundaries were preserved by
merging P2.2 as `2b77f13` and P3.1 as `5f62998` into
`codex/database-slash-modernization`. T1 followed as `aacace4`.

### D-014 — Keep beta fixtures separate from default data

Status: Accepted

Permanent reference initialization remains in `--add_default_data`.
Disposable beta games use a separate, strictly gated operator command and
existing real development-guild players so native Discord member selectors
remain testable. Database ownership markers, not the ignored local manifest,
control cleanup. Seed and cleanup run only while the beta bot is stopped
because job coordinators do not cross process boundaries.

### D-015 — Retain useful manual development games deliberately

Status: Accepted

Development database hygiene does not require deleting every interactive test
game after each unit. A useful unowned game may be retained across adjacent
modernization units when its ID and purpose are recorded. It is never treated
as harness-owned or assumed to be in its original state; inspect it before
reuse and clean it when stale, confusing, or before a production-oriented
cutover review. Game `118` (`Foobar`) is the first intentionally retained
manual fixture.

### D-016 — Retire the unfinished duplicate-ELO reversal command

Status: Accepted

`reverse_duplicated_elo` was not a usable maintenance interface: it always
returned “command not finished,” while its unreachable implementation lacked
the coordinator and transaction boundaries and contained a broken game-side
reference. P3.2 removes it instead of converting it to slash. Supported
repairs use the serialized recalculation-from-game workflow or, when a full
rebuild is deliberately required, the separately operated command-line
recalculation.

### D-017 — Keep successful competitive mutations public

Status: Accepted

Successful slash-command results that change competitive game state should be
public by default, preserving the transparency and shared audit context of
their prefix equivalents. Permission denials, validation failures, and
database errors should generally remain ephemeral. A command may make success
private only for a recorded privacy or safety reason.

### D-018 — Select slash taxonomy before expanding the public surface

Status: Pending staff vote

Three alternatives are recorded in the slash taxonomy review: domain groups,
one ELO-branded umbrella, and conservative flat commands. No registration
change is authorized by the review. Prefix commands remain stable, and the
selected slash names will be implemented as thin adapters over the existing
shared worker/application paths. The vote result, final spellings, and any
temporary beta aliases must be recorded before implementation.

## Progress log

### 2026-07-29 — Slash taxonomy review prepared

- Inventoried the complete in-scope repository-backed surface: 83 explicit prefix
  handlers, customized framework help, 10 current native registrations,
  aliases, and the optional Bullet family. Excluded the seven-command legacy
  API cog from the modernization taxonomy and backlog.
- Added `docs/SLASH_COMMAND_TAXONOMY_REVIEW.md` with a disposition and
  recommended native home for every existing command handler, including
  explicit operator-only and retain/retire classifications.
- Proposed three staff-vote alternatives: domain groups (recommended), one
  application umbrella, and systematic flat commands.
- Confirmed that grouped slash wrappers can preserve the existing prefix
  names; no transaction worker or permission behavior needs to change solely
  for a rename.
- Froze further slash naming decisions after P4.1b until staff select a
  taxonomy and approve final spellings.
- Made no command-registration, synchronization, beta-runtime, database, or
  production change.

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
- Launched checkpoint `89e5710` under the verified development profile and
  synchronized six commands, including `/newgame`, only to development guild
  `478571892832206869`.
- No production, dependency, or schema action was performed.
- Next: run the P2.2 beta smoke matrix, clean up fixtures, stop the beta
  process, and integrate the accepted unit into the accumulation branch.

### 2026-07-29 — Slash compatibility compromise ledger established

- Added a required running ledger for native parity gaps and their
  message-intent impact.
- Recorded C-001: initial `/newgame` omits more than two sides, more than four
  players per side, and requester inference.
- Accepted the gap because `newgame` is rare and practical usage is
  overwhelmingly even, two-sided games already covered by the typed slash
  interface.
- Recorded an optional future `/game custom` interaction draft using native
  member selectors and explicit review/confirmation; it is not required for
  P2.2 acceptance.

### 2026-07-29 — P2.2/P3.1 combined beta gate approved

- Stopped the task-owned development beta session cleanly after successful
  synchronization; P2.2 functional acceptance was pending at that checkpoint.
- Approved stacking P3.1 on the validated P2.2 checkpoint so both units can
  use one later beta launch and smoke matrix.
- Kept P2.2 Implemented rather than Complete and retained sequential
  accumulation-branch integration after combined acceptance.

### 2026-07-29 — P3.1 ELO maintenance UX implemented

- Created stacked branch `codex/p3-1-elo-maintenance-ux` from P2.2 checkpoint
  `4a7fba6`.
- Added owner-only, explicitly confirmed `/recalc-games-from` and
  staff-visible ephemeral `/elo-job-status`.
- Preserved the hidden owner prefix recalculation command and routed both
  mutation entry points through the existing coordinator/worker.
- Removed the prefix path's event-loop Peewee game lookup; worker-local
  validation remains authoritative.
- Added worker connection/transaction/rollback tests plus native
  registration, permissions, confirmation, defer, conflict, validation, and
  status tests.
- Passed 28 focused tests, 97 complete offline tests with seven gated skips,
  and all seven explicitly gated development-database tests.
- Recorded implementation checkpoint `1bebce6`; no new slash compatibility
  compromise was introduced.
- No beta launch or P3.1 command synchronization was performed.
- Next: user runs one combined P2.2/P3.1 beta sync and smoke matrix, then the
  accepted unit branches merge sequentially into accumulation.

### 2026-07-29 — Development beta fixture harness implemented

- Created `codex/dev-beta-fixture-harness` from P3.1 checkpoint `013bab2`.
- Recorded implementation checkpoint `4551bec`.
- Added strictly gated, idempotent `seed`, `status`, and confirmed `cleanup`
  tooling plus an operator runbook.
- Added eight focused offline tests and a real PostgreSQL round trip that
  preserves unowned games and cleans its temporary users.
- Passed 106 offline tests with eight gated skips and all eight gated
  development-database tests.
- Removed two zero-game temporary users left by the first test-finalizer
  failure; no failed-run fixture games remained.
- Seeded games `115`, `116`, and `117` with the prior Nelluk and
  `testaccount12174` beta participants; an idempotency rerun preserved those
  IDs.
- No beta launch, Discord synchronization, dependency, schema, production, or
  service operation was performed.
- Next: commit this checkpoint and use the seeded games in the combined
  P2.2/P3.1 beta session.

### 2026-07-29 — Fixture-backed beta procedure updated

- Verified the harness status gate against `polytopia_dev` / `polybot_dev`
  while the beta was stopped: games `115`, `116`, and `117` remain owned and
  reference Nelluk plus `testaccount12174`.
- Expanded `docs/DEVELOPMENT_BETA_FIXTURES.md` into the authoritative combined
  P2.2/P3.1 procedure.
- Assigned game `117` as the confirmed recalculation target and documented
  false-confirmation, owner denial, active/idle status, and timing-dependent
  conflict checks.
- Reduced the required `/newgame` live matrix to ranked Mobile and unranked
  Steam 1v1s; 2v2 is optional when four distinct development members are
  available because option structure is covered offline.
- Kept interactive game IDs outside ownership-gated harness cleanup and
  required an explicit cleanup-or-retention decision for each.
- Required beta shutdown before owned status/cleanup and a final empty-owned
  status check.

### 2026-07-29 — Combined P2.2/P3.1 beta accepted

- The user reported the tested commands appeared correct.
- Development logs confirmed all eight expected guild commands synchronized,
  ranked Mobile `/newgame` created game `118`, and recalculation from owned
  confirmed game `117` completed.
- Recorded P2.2 and P3.1 as Beta-validated. Unranked Steam/2v2 creation,
  active-conflict timing, and non-owner denial were not independently visible
  in logs and remain covered by offline tests rather than overclaimed as live
  evidence.
- Confirmed the beta process was stopped.
- Ran the gated, confirmed harness cleanup; owned games `115`-`117` were
  removed, and a final gated status check showed no owned fixtures.
- Interactive game `118` has no deletion record and remains outside harness
  authority.
- The user chose to retain game `118` for later modified-command testing; it
  is documented as manual development data rather than an integration
  blocker.
- Next: integrate P2.2, P3.1, and T1 sequentially into accumulation.

### 2026-07-29 — P2.2, P3.1, and T1 integrated

- Retained interactive game `118` as a documented manual development fixture;
  it remains outside harness ownership and is not an integration blocker.
- Merged P2.2, P3.1, and T1 sequentially into
  `codex/database-slash-modernization` as `2b77f13`, `5f62998`, and
  `aacace4`.
- Passed the complete offline suite: 106 tests passed and eight gated
  database tests skipped as designed.
- Passed all eight development-database integration tests after the existing
  gate confirmed `development`, `polytopia_dev`, and `polybot_dev`.
- Marked P2, P3.1, and T1 Complete on their intended accumulation branch.
- No beta launch, Discord synchronization, push, PR, production checkout,
  production database, or production service operation was performed.
- Next: P3.2, a bounded ELO-maintenance consistency review of CLI full
  recalculation, cancellation documentation, and the disabled
  `reverse_duplicated_elo` path.

### 2026-07-29 — P3.2 ELO maintenance consistency implemented

- Created `codex/p3-2-elo-maintenance-consistency` from accumulation
  checkpoint `55425a5`.
- Gave standalone `bot.py --recalc_elo` an explicit process-local Peewee
  connection lifecycle around its existing serialized synchronous
  transaction.
- Documented coordinator scope, cancellation/shutdown semantics, supported
  recalculation paths, and operator separation requirements.
- Retired the hidden unfinished `reverse_duplicated_elo` prefix command; no
  slash replacement or compatibility compromise was introduced.
- Recorded implementation checkpoint `63c9378`.
- Passed three focused tests and the complete offline suite: 108 passed with
  eight gated database tests skipped as designed.
- Passed all eight gated development-database tests after confirming
  `development`, `polytopia_dev`, and `polybot_dev`.
- No beta launch, command synchronization, production operation, dependency
  change, or schema change was performed.
- Next: review and integrate P3.2 into the accumulation branch, then start
  P4.1 staff state corrections.

### 2026-07-29 — P3.2 integrated

- Consolidated the maintenance guidance into this durable roadmap and removed
  the isolated `docs/ELO_MAINTENANCE.md` file in checkpoint `294b4aa`.
- Merged P3.2 into `codex/database-slash-modernization` as `41bd614`.
- Marked P3 Complete on its intended accumulation branch.
- Selected paired `rankset`/`rankunset` modernization as P4.1a; `unstart` and
  `extend` remain separate later units.
- Next: create P4.1a from `41bd614`.

### 2026-07-29 — P4.1a ranked-state correction implemented

- Created `codex/p4-1a-ranked-state-correction` from accumulation checkpoint
  `f215bae`.
- Preserved prefix `rankset` and `rankunset` and added typed staff-only
  `/set-ranked`.
- Moved ranked-state validation, mutation, and audit logging into one bounded
  worker-local transaction using primitive IDs.
- Kept the per-game claim through post-commit Discord notification and
  guaranteed cleanup.
- Passed seven focused tests, the complete 115-test offline suite with eight
  gated skips, and all eight gated development-database tests.
- Recorded implementation checkpoints `3e1f395` and `d2526b4`.
- No beta launch, synchronization, production operation, dependency change,
  or schema change was performed.
- Next: obtain separate approval for a fixture-backed beta sync/smoke test.

### 2026-07-29 — P4.1a beta accepted and transparency policy corrected

- Seeded owned development fixtures `149`-`151` while the beta was stopped.
- Verified the development profile selected the beta application,
  `polytopia_dev`, guild `478571892832206869`, and disabled background
  tasks/API/Bullet integration.
- Synchronized nine development-guild commands, including `/set-ranked`.
- The user accepted `/set-ranked`, `rankset`, and `rankunset` behavior.
- Changed successful `/set-ranked` output from ephemeral to public at the
  user's request; permission, validation, and database-error output remains
  ephemeral. The user waived a second live retest.
- Passed seven focused tests and the complete 115-test offline suite with
  eight gated skips after the visibility adjustment.
- Stopped the beta cleanly. Retained owned game `149` incomplete/unranked,
  `150` unconfirmed/ranked, and `151` confirmed/ranked for later units.
- Next: integrate P4.1a into the accumulation branch.

### 2026-07-29 — P4.1a integrated

- Merged the beta-accepted ranked-state unit into
  `codex/database-slash-modernization` as `5888c02`.
- Marked P4.1a Complete on its intended accumulation branch.
- Retained owned fixtures `149`-`151` for the next command units; their
  recorded post-test states remain authoritative until inspected again.
- Next: P4.1b pending-game extension, followed separately by the more
  destructive `unstart` workflow.

### 2026-07-29 — P4.1b pending-game extension implemented

- Created `codex/p4-1b-pending-game-extension` from accumulation checkpoint
  `62ea671`.
- Preserved `$extend` and added staff-only typed `/extend game_id` with
  public successful output.
- Moved deadline validation, mutation, and audit logging into one bounded
  worker-local transaction using primitive inputs and a per-game claim.
- Preserved both future-deadline and expired-deadline calculation behavior.
- Passed eight focused tests and the complete 123-test offline suite with
  eight gated skips.
- The gated development-database suite passed seven tests; its fixture
  round-trip safely skipped to preserve the retained operator fixture set.
- No beta launch, synchronization, production operation, dependency change,
  or schema change was performed.
- Recorded implementation checkpoint `c0945a3`.
- Next: commit P4.1b and review P4.1c `unstart` as a separately bounded unit
  suitable for the same later beta session.

### 2026-07-29 — P4.1c unstart separation implemented

- Created `codex/p4-1c-unstart-separation`, stacked from P4.1b evidence
  checkpoint `af4ef51`.
- Preserved staff prefix `$unstart` and its game-channel invocation guard.
- Moved mutable-state validation, pending/expiration mutation, and audit
  logging into one bounded worker-local transaction.
- Moved announcement editing and channel deletion after commit, then
  reconciled only confirmed deletions in a second bounded transaction.
- Deferred the typed slash adapter under D-018; recommended placement remains
  `/match unstart` if staff select T-A.
- Passed nine focused tests and the complete offline suite: 132 passed with
  eight gated skips.
- Passed seven gated development-database tests; the fixture round-trip
  skipped to preserve operator fixtures after confirming `development`,
  `polytopia_dev`, and `polybot_dev`.
- Recorded implementation checkpoint `204ab40`. No beta launch, command
  synchronization, production operation, dependency change, or schema change
  was performed.
- Next: commit the evidence, resolve the taxonomy vote, and use one separately
  approved beta session for `/extend` plus `$unstart`.

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
