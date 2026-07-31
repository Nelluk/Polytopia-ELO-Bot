# Database Access and Slash Command Modernization

Last updated: 2026-07-31

Status: Active

Current branch at last update: `codex/p7-9-game-detail-workspace`

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
- Standing authorization granted on 2026-07-30 keeps the development beta bot
  running by default. After a significant completed and validated work unit,
  restart only that development process from the intended checkpoint so it
  picks up code changes and performs its normal development-guild-only
  synchronization. Stop it temporarily when a gated fixture operation or
  other documented safety procedure requires the bot to be offline.
- The standing beta authorization does not cover production, a global command
  synchronization, a different guild/profile, enabling background tasks or
  the API, dependency installation, or materially broader live testing.
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
- Modernizing or converting the legacy API, Bullet tournament, or anti-scam
  modules. They may remain operational and receive narrowly necessary
  maintenance until a separately approved retirement decision.

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
4. Evaluate whether secondary options belong in a Components v2 interaction
   after invocation. Prefer a short task-oriented command plus an interactive
   result when several options select views, filters, pages, or iterative
   edits rather than defining the command's primary target or effect.
5. Defer immediately before potentially slow work.
6. Keep permission checks equivalent between prefix and slash paths.
7. Make error visibility deliberate; permission or validation errors should
   generally be ephemeral for slash users.
   Successful competitive-state mutations should generally be public so the
   native interface preserves the transparency of the corresponding prefix
   command. Deviations require a recorded privacy or safety reason.
8. Add registration tests for both interfaces.
9. Record any native-interface compromise in the compatibility ledger below,
   including user impact, acceptance, and a possible mitigation. Do not let
   parity gaps live only in task commentary.

Do not rename the five beta-validated pilot slash commands without a separate
compatibility and deprecation decision.

### Components v2 interaction standard

Desktop and mobile beta testing of P7.5 established Components v2 as the
preferred presentation layer for interaction-rich workflows. Slash commands
should state the task and collect inputs necessary to identify its target.
Options that merely change presentation or let the user explore the result
should normally move into the response.

Prefer Components v2 for:

- multiple useful views or filter combinations over one result domain;
- pagination, requester-rank jumps, search refinement, and cached refreshes;
- iterative drafts, previews, and review/confirmation steps;
- focused read-or-edit attributes whose current value should be visible;
- public results with requester-only controls;
- staff workflows that benefit from preview, explicit confirmation, and
  visible job state.

Keep direct slash options for:

- identifiers and targets required to know what operation to perform;
- one-step mutations such as join, leave, win, confirm, extend, and delete;
- a small number of safety-critical choices that should be visible in the
  submitted command;
- accessibility or automation paths where an interactive draft adds no value.

A conversion unit must now classify each proposed option as **essential
invocation input** or **interactive refinement**. It should not reproduce a
large prefix argument matrix as a large slash option matrix merely because
Discord permits it.

Components v2 messages cannot mix ordinary content or embeds, cannot be
downgraded after the v2 message flag is set, and have component/count/layout
limits. Every implementation must therefore provide a complete v2 renderer,
mobile/desktop beta evidence for novel layouts, bounded state, deliberate
timeout behavior, and an explicit rerun path after expiration.

The preferred visibility model remains public competitive results with
requester-only controls. Unauthorized controls fail ephemerally without
changing the public message. Database loads use existing bounded workers;
page changes and other snapshot-only navigation must not query the database.

### Slash taxonomy review

Status: **Taxonomy v2.2 proposed; attribute-command, component-first,
show/ping/logs naming, and legacy-module exclusion rules accepted; unified
player workspace proposed; registration changes pending review**

The accepted architecture remains T-A domain roots with one user-facing
`/game` domain across open, pending, started, and completed states. On
2026-07-30 the user reopened the spelling and internal-grouping review before
the first synchronization of checkpoint `63af179`.

The repository-wide v2.2 proposal and conversion dispositions are maintained
in `docs/SLASH_COMMAND_TAXONOMY_REVIEW.md`. It covers 78 active-target
explicit prefix handlers, the customized help command, commands needing
interaction redesign, and commands that should remain operator-only. The
legacy API cog, five-command Bullet family/results listener, and command-free
anti-scam listener remain excluded.

The current locally implemented native surface is:

- `/game record`, `/game win`, `/game unwin`, `/game delete`;
- `/game confirm`, `/game unconfirmed`, `/game set-ranked`;
- `/game extend`, `/game unstart`;
- `/elo recalculate`, `/elo status`;
- `/leaderboard players`, `/leaderboard activity`,
  `/leaderboard squads`, and temporary `/lb2`.

The prior top-level names were synchronized only to the development guild.
None reached production. The approved migration therefore removes them
cleanly rather than registering compatibility aliases. The never-synchronized
`/match` group is also absent. The unified `/game` tree was later synchronized
and exercised only in the development guild during P7 testing. Taxonomy v2.2
has not been registered, and a later approved development sync must verify
that renamed or removed guild commands are pruned.

#### Taxonomy v2.2 — component-first journey groups

The revision keeps common actions directly under `/game`:

- `/game open|join|leave|start|record|show|search|players|win|logs|ping`.

Less-common operations use one additional conceptual group:

- `/game result undo|confirm|auto-confirm`;
- `/game manage kick|extend|unstart|delete`.

The proposal deliberately uses `/game record` for the existing `newgame`
workflow, `/game players` for `getnames`, and
`/game search status:unconfirmed` for the current unconfirmed-game list. It
does not add generic `get` or `set` groups: useful attributes become focused
read-or-edit commands such as
`/game name|map|tribe|notes|side|ranked` and
`/team emoji|image|server|name|house|tier`. Omitting the replacement value
reads the current setting; supplying one edits it with command-specific
permission checks; clearing uses an explicit option.

The accepted Components v2 rule now changes the interaction shape without
changing those domain homes. Slash invocation supplies only the task and an
essential target that cannot be inferred. Filters, optional attributes,
long-form authoring, multiple uploads, previews, confirmation, and pagination
move into a requester-controlled workspace when they are not necessary to
identify the operation.

Additional staff/user feedback incorporated in v2.2:

- `/game show` and `/player show` remain the explicit detail commands.
  `/player show` defaults to the requester; `/game show` uses an inferred
  game only when context is unambiguous and otherwise requests a game ID.
- `/game ping` opens an interactive composer instead of exposing message,
  scope, attachments, and confirmation as slash options. It must
  support multiple uploads and a high, explicit aggregate text limit through
  bounded multi-message delivery; it cannot promise unlimited Discord
  messages.
- `/player register` collects one canonical Polytopia name. The slash surface
  no longer distinguishes mobile name, Steam name, and legacy friend code;
  existing stored values require a separate migration decision.
- the established top-level `/staffhelp` name remains. It opens a structured
  modal/workspace rather than becoming `/support request`.
- `/player show` is proposed as one shared Components v2 workspace for
  profile, rating, recent/incomplete/completed/season-game, and result views.
  Existing `$player`/`$elo`/`$rank` and simple single-player game-list
  commands can deep-link its initial section without creating slash aliases.

The proposed `/game` root has nineteen immediate children, including its two
subcommand groups, leaving six slots below Discord's 25-child limit. The same
system-wide rules apply to `/player`, `/team`, `/squad`, `/leaderboard`,
`/league`, `/house`, `/elo`, `/tools`, `/about`, and
top-level `/staffhelp`.

The current registrations have not reached production, so an approved revision
can be applied cleanly without slash compatibility aliases. No registration
change should occur until the user accepts or revises v2.2. Prefix interfaces,
permissions, worker boundaries, and transaction behavior remain unaffected.

## Slash compatibility compromise ledger

This is the running record of behavior that a native interaction does not
cover. A gap may be accepted when the affected command is rare or the native
path covers normal usage. Prefix availability during the transition means a
listed gap is not necessarily a current loss; the **message-intent impact**
column states what would become unavailable if prefix processing could no
longer be retained.

| ID / command | Native coverage | Accepted compromise and message-intent impact | Possible future mitigation | Status |
|---|---|---|---|---|
| C-001 `/newgame` | `/game record` accepts one roster string using the established `vs` grammar, infers arbitrary/unequal side sizes, preserves the one-opponent requester shortcut, and requires an interaction preview before creation. Edit sides provides native member selection plus add/remove-side controls. | The former two-sided 1v1–4v4 and raw-text-edit limits are resolved without message-content intent. Initial text tokens can still be ambiguous when users do not supply mentions, but the parsed draft can be corrected with native selectors. Per-game Mobile/Steam input was deliberately removed because full cross-play makes it obsolete. | If initial parsing remains troublesome, allow `/game record` to open an empty guided draft without requiring a seed roster. | Shape and edit gaps resolved by P2.3; initial parser usability remains for beta evaluation |
| C-002 `/player show` and player-card prefixes | The shared workspace preserves identity, canonical name, current/peak/all-time local/global ratings, records, ranks, timezone, team/squad context, and paged game sections/filters. | The legacy generated rating-history image, requester head-to-head follow-up, trophies, favorite-tribe summary, and pre-Moonrise miscellaneous statistics are not yet displayed after the prefix commands deep-link the workspace. These become unavailable from those commands even while message intent remains enabled. | Add a bounded analytics/details section and media attachment renderer after observing which legacy details staff/users still value; keep graph generation outside the event loop. | Open for beta/user review in P7.7 |

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
- P4.1b/P4.1c accumulation merge: `31c84d7`
- P4.1d initial `/match` checkpoint: `416ca30`
- unified taxonomy approval checkpoint: `951460a`
- unified native registration checkpoint: `63af179`
- P7.5 Components v2 leaderboard checkpoint: `c4b34df`
- P7.5 development-guild sync checkpoint: `96c6981`
- T1 fixture-harness implementation checkpoint: `4551bec`
- T1 roadmap-evidence checkpoint: `d6e826b`
- T1 accumulation merge: `aacace4`
- pilot beta acceptance: all five original application commands reported
  working
- combined development sync: all eight expected commands synchronized to
  guild `478571892832206869`; P2.2 and P3.1 accepted by the user
- complete offline suite: 142 tests passed, with eight gated database tests
  skipped as designed
- gated `polytopia_dev` suite: seven tests passed and one operator-fixture
  round trip skipped under the required `development` / `polytopia_dev` /
  `polybot_dev` checks
- live-test game fixture: game `61` was deleted successfully
- optional cleanup: unused `Team.id=9`, `Phase7 Test Team`, remains in
  `polytopia_dev` with zero players and zero game sides
- retained P7.5 showcase: 24 owned players (`163`-`186`) and 48 owned games
  (`200`-`247`), with gated status/confirmed-cleanup tooling
- P7.9 implementation checkpoint: `24a435b` with Tier 2 parity correction
  `22023f4` on `codex/p7-9-game-detail-workspace`, based on `16fc6565`

Current unit: **P7.9 Implemented; beta acceptance pending.** P7.6, P7.7, and
P7.8 are integrated into `codex/database-slash-modernization` after functional
beta smoke testing. P7.9 remains on its dedicated branch for Sol's complete
Tier 2 review and D-026 beta gate; it is not integrated or marked Complete.
Taxonomy v2.2 as a whole remains pending final approval; `/game show` is the
D-025-approved detail path and `/game search` remains the accepted,
noncontroversial discovery path.

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

### Planning and execution worktrees

Sol planning/oversight and Luna implementation tasks use the protocol in
`docs/MODERNIZATION_COLLABORATION_WORKFLOW.md`:

- `/home/nelluk/PolyBot39-dev` remains the planning/integration checkout;
- `/home/nelluk/PolyBot39-dev/.worktrees/luna` is the isolated execution
  checkout;
- Luna creates one bounded unit branch from the exact clean accumulation
  checkpoint supplied by Sol;
- Sol remains read-only while Luna implements and reviews the complete unit
  at its integration gate rather than reviewing every intermediate commit;
- review depth is risk-tiered, with design review before Tier 3 mutation,
  coordination, schema, security, or production units.

Worktrees isolate files, indexes, and checked-out branches. They do not
isolate runtime processes, database state, Discord registrations, or Git refs,
and they do not broaden operational authority.

## Compatibility and production-canary strategy

Modernization preserves one database/application implementation while allowing
invocation and presentation to evolve at different rates.

1. **Semantic compatibility is mandatory.** Prefix and slash paths preserve
   permissions, validation, database effects, transactions, audit
   attribution, coordinator use, and post-commit Discord ordering.
2. **Invocation compatibility remains during transition.** Prefix names and
   aliases stay registered as thin adapters over the same bounded service used
   by native commands. A hybrid decorator is optional and used only when its
   grammar and slash placement map cleanly.
3. **Presentation compatibility may intentionally change.** Components v2 may
   replace embeds, reactions, pagination, or option-heavy output. Material
   omissions or changed workflows are recorded in the compatibility ledger
   and beta-tested on desktop/mobile.
4. **Classic presentation is exceptional and temporary.** A high-use or
   high-risk workflow may temporarily keep a separate legacy renderer, but it
   consumes the same DTO/service and has an explicit removal condition. There
   must never be separate classic and modern mutation implementations.

Do not run production and beta bot processes as concurrent writers to the
same database. ELO coordination, per-game claims, component state, fixture
coordination, and reconciliation are process-local. A second process could
bypass serialization, duplicate prefix responses/listeners, or perform
conflicting Discord effects.

The intended production observation path is instead a **single-process guild
canary** after P9 approval:

- deploy the modernized production bot once;
- keep legacy prefixes enabled;
- register/enable the new native/component interface initially only in the
  approved PolyChampions guild through explicit capability policy;
- route both interfaces through the same in-process services, coordinators,
  and workers;
- observe and expand to other guilds only after acceptance;
- deprecate prefixes later using compatibility and usage evidence.

Suggested policy concepts are `native_components_enabled_guilds`,
`native_mutations_enabled_guilds`, and `legacy_prefix_enabled`. Their exact
configuration and default-deny behavior belong to a separate pre-P9 unit;
this decision does not authorize production deployment or synchronization.

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
| P7 | In progress | Read-heavy game, player, and leaderboard commands | Bounded read path and responsive slash queries |
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

Next action: P2.3 supersedes the fixed P2.2 option matrix. Inspect retained
manual fixture `118` before any later reuse.

Exit criteria:

- no Discord await inside the creation transaction;
- worker-local connection and primitive inputs;
- transaction and post-commit fault tests;
- prefix behavior preserved;
- slash decision implemented or explicitly deferred;
- approved beta test if an application command is added.

### P2.3 — Flexible `/game record` roster and cross-play cleanup

Status: **Complete**

Branch/base: `codex/p2-3-game-record-roster` from P7.5 Components v2
checkpoint `dd20e9b`.

Objective: replace the fixed two-sided `/game create` option matrix with the
taxonomy-proposed `/game record`, using one roster string and a Components v2
review gate before the existing transactional worker.

Selected interface:

- required exact game name;
- required roster string using `player ... vs player ...` grammar;
- optional ranked Boolean;
- no platform option;
- requester-only parsed preview with Edit sides, Confirm record, and Cancel;
- confirmation alone enters the existing prefix validation, bounded worker,
  transaction, and post-commit Discord effects.

The parser supports arbitrary side counts and unequal sizes subject to the
existing guild/game rules. A single roster token retains the requester-versus-
opponent shortcut. Mentions are recommended; quoted tokens are accepted, but
ambiguous text names remain a documented usability limitation.

Mobile/Steam is obsolete for new native recording because Polytopia now has
full cross-play. The legacy database Boolean remains temporarily populated
with its canonical compatibility value so this unit does not combine command
UX with a schema/history migration. Existing Steam prefix aliases remain
registered during transition but no longer create a distinct platform type.
Broader removal of platform-specific open-game filters, stored names, emojis,
and ping aliases is a separate compatibility unit.

Required evidence:

- [x] flexible parser coverage, including uneven and multiple sides;
- [x] preview precedes worker submission;
- [x] requester-only Edit/Confirm/Cancel controls;
- [x] no slash platform option and canonical compatibility storage;
- [x] prefix aliases preserved;
- [x] rollback, responsiveness, and post-commit tests remain green;
- [x] complete offline suite;
- [x] gated development-database suite;
- [x] development beta restart and development-guild sync;
- [x] live preview/edit/cancel/confirm smoke acceptance.

Implementation evidence:

- `modules/game_record_views.py` parses the legacy `vs` grammar with quoted
  tokens, rejects incomplete/ambiguous side syntax, renders a Components v2
  preview, and owns requester-only native side/member editing plus
  Confirm/Cancel controls.
- `/game record` accepts only `game_name`, `roster`, and optional `ranked`;
  the former platform and ten fixed member options are absent.
- Preview member resolution and existing registration/channel/participation
  checks happen before confirmation. The prefix callback and bounded P2.1
  worker are not invoked until Confirm.
- The record invocation and parsed preview are public for competitive
  transparency. Only the requester can use its Edit/Confirm/Cancel controls;
  unauthorized control attempts remain ephemeral.
- The shared prefix/slash resolver preserves permissions and the one-opponent
  shortcut. Prefix aliases remain registered.
- All new recorded games use the legacy `is_mobile=True` compatibility value,
  including requests invoked through a retained Steam alias.
- The gated real-schema test creates three sides, verifies three lineups, and
  rolls the complete graph back.

Validation:

- Focused: `POLYBOT_ENV=development MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest tests.test_newgame_worker -v` — 18 passed
  after the beta confirmation-context and native side-editor fixes.
- Complete offline: `POLYBOT_ENV=development
  MPLCONFIGDIR=/tmp/polybot-matplotlib .venv/bin/python -m unittest discover
  -v` — 183 passed, 9 gated skips.
- Development database: `POLYBOT_ENV=development
  POLYBOT_RUN_DB_INTEGRATION=1 MPLCONFIGDIR=/tmp/polybot-matplotlib
  .venv/bin/python -m unittest tests.test_database_integration -v` — 8
  passed; one fixture round trip safely skipped to preserve the
  operator-managed set after confirming `polytopia_dev` and `polybot_dev`.
- Compilation and `git diff --check`: passed.

Implementation checkpoints:

- `6af7c92` — flexible roster parser and first preview implementation;
- `b54618e` — component-confirmation fix and native side/member editor.
- `2513028` — beta acceptance evidence.
- `688b9d6` — integration into `codex/database-slash-modernization`.

Runtime evidence: the task-owned beta was stopped cleanly after checkpointing,
then restarted from `da21786` with `POLYBOT_ENV=development` and
`--skip_tasks`. It authenticated as `PolyELO Bot Beta`
(`479029527553638401`) and completed startup/development-guild sync without a
reported error. After the first smoke exposed a component-context failure,
the beta restarted from `b54618e`. The user then reported the corrected
record, confirmation, and native side-editing flow worked well and accepted
the unit. No further live exception appeared in the task-owned beta output.
The beta remains running.

Next action: keep the broader Taxonomy v2.2 status pending final approval,
then select the next bounded unit from the integrated accumulation branch.

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

Status: **Implemented; integrated, beta acceptance pending**

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

Status: **Implemented; integrated, beta acceptance pending**

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

Slash decision: preserve staff-only prefix `$unstart`. D-018 now authorizes
the typed integer adapter at `/match unstart`; implement it in the bounded
domain-group registration unit rather than adding a temporary top-level
`/unstart`. This is sequencing rather than reduced native parity, so it adds
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

Beta result: pending. After the domain-group registration unit, a combined
session can test `/match unstart` followed by `/match extend` while also
checking preserved `$unstart 149`. Inspect fixture 149 immediately before use
and record any announcement or channel resources affected.

Remaining limitations:

- Announcement rendering reloads the committed model synchronously on the
  event-loop thread before the Discord edit. The transition and all
  potentially material writes are bounded, but a later read/display DTO unit
  should remove this short post-commit model load.
- Cancellation after the synchronous transition commits can skip some or all
  Discord effects. Retained channel references make failed/skipped deletion
  discoverable, but no persistent reconciliation queue exists yet.
- The command remains prefix-only until the authorized domain-group
  registration unit adds `/match unstart`.

Next action: integrate the reviewed P4.1b/P4.1c checkpoints into the
accumulation branch, then create a bounded domain-group registration unit for
the current native surface, including `/match extend` and `/match unstart`.
Use one separately approved beta session after that unit.

#### P4.1d — Unified native registration

Status: **Implemented; integrated, beta acceptance pending**

Branch/base: `codex/p4-1d-match-slash-group` from accumulation checkpoint
`31c84d7`, which integrates the P4.1b/P4.1c worker units.

Commit(s):

- `416ca30` — Add match slash commands for unstart and extend.
- `951460a` — Approve unified game slash taxonomy.
- `63af179` — Unify native commands under game and elo.

Objective: apply the approved T-A taxonomy to every native command implemented
through P4.1d and expose P4.1c through a typed native interaction.

Slash interface:

- Register one guild-only `/game` group containing
  `create|win|unwin|delete|confirm|unconfirmed|set-ranked|extend|unstart`.
- Register one guild-only `/elo` group containing `recalculate|status`.
- Remove the development-only top-level native names and the
  never-synchronized `/match` group; no temporary slash aliases are retained.
- Preserve all corresponding prefix commands and aliases unchanged.
- Both successful mutations defer and complete publicly under D-017.
  Permission, validation, per-game conflict, and database failures remain
  ephemeral.

Implementation evidence:

- The games cog owns the `/game` group and its nine typed subcommands; the
  administration cog owns `/elo` and its two maintenance subcommands.
- `/game unstart` performs the staff check before defer, then calls the same
  P4.1c worker/post-commit pipeline as the prefix command.
- The primitive invocation channel ID now crosses into the worker so the
  prefix safety rule is authoritatively revalidated before mutation for slash
  users as well.
- Slash audit logging identifies `/game unstart` rather than attributing the
  transition to the prefix spelling.
- Thin `/game` adapters reuse existing prefix checks/callbacks or delegate to
  the existing administration handlers, avoiding duplicate permissions and
  mutation logic.
- Registration tests prove the only top-level roots are `game` and `elo`,
  their exact eleven subcommands and typed shapes are correct, obsolete
  top-level names and `match` are absent, and prefix commands/aliases remain.
- No native behavior is omitted, so no compatibility-ledger row is required.

Files changed:

- `modules/administration.py`
- `modules/games.py`
- `modules/game_workers.py`
- `tests/test_game_extension.py`
- `tests/test_game_unstart.py`
- `tests/test_elo_jobs.py`
- `tests/test_newgame_worker.py`
- `tests/test_ranked_state.py`
- `tests/test_slash_taxonomy.py`
- `modules/dev_fixtures.py`
- `scripts/manage_dev_fixtures.py`
- `tests/test_dev_fixtures.py`
- `docs/DEVELOPMENT_BETA_FIXTURES.md`
- `docs/DATABASE_AND_SLASH_MODERNIZATION.md`

Validation evidence:

- Focused taxonomy and affected command suite: 74 passed.
- Complete offline suite: 142 passed with eight gated database tests skipped
  as designed.
- Existing gated development-database suite: seven passed and the fixture
  round-trip skipped itself to preserve operator-managed games `149`-`151`;
  the gate confirmed `development`, `polytopia_dev`, and `polybot_dev`.
- Gated fixture status confirmed game `149` ready/incomplete/unranked, game
  `150` unconfirmed/ranked, and game `151` confirmed/ranked while no beta
  process was running.
- Fixture status now reports pending state and expiration, so the post-smoke
  database state can be verified without an ad hoc query.
- `git diff --check`: clean before this documentation update.

Beta result: pending taxonomy v2 approval and then separate
launch/synchronization approval. The existing smoke matrix in
`docs/DEVELOPMENT_BETA_FIXTURES.md` still describes checkpoint `63af179` and
must be updated if v2 paths are accepted before beta testing.

Remaining limitations:

- P4.1c's short post-commit announcement model reload and non-persistent
  reconciliation limitation remain.
- The fixture-backed smoke test does not normally exercise real announcement
  editing or channel deletion; focused fault/ordering tests cover those
  boundaries unless a separately recorded disposable interactive game is
  used.
- The branch name retains the superseded `match` wording; the commit and
  command tree are authoritative. Renaming the local branch is unnecessary
  churn before integration.

Next action: obtain review of taxonomy v2. If accepted, implement a narrow
registration-only P4.1d follow-up, update the smoke runbook and tests, and
then request separate approval to launch and synchronize the beta bot. Do not
integrate P4.1d into accumulation until its live behavior is accepted.

### P4.2 — Game metadata

Candidates:

- `rename`
- `setmap`
- `settribe`
- `gamenotes`

Recommended slash shape:

- typed game ID;
- focused `/game name|map|tribe|notes` commands that display the current value
  when no replacement is supplied;
- optional replacement values that enter the existing permission-equivalent
  mutation path;
- an explicit `clear` option where clearing is supported;
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

- `/game open`
- `/game join game_id side`
- `/game leave game_id`
- `/game kick game_id member`
- `/game start game_id name`

The lifecycle shares `/game` with tracked-game commands. Consolidate game
lists/history under `/game search` and enforce the 25-child group limit before
each new registration.

## P6 — Registration and player preferences

Status: **Planned**

Candidate scope:

- `setname`
- `steamname` and `setcode` compatibility/data review
- `getname`
- `settime`
- squad naming and similar low-risk profile writes

Goals:

- use typed Discord member inputs for staff overrides;
- place lookup/create/update/log operations in one worker transaction;
- keep role changes and direct messages post-commit;
- add `/player register` with one canonical Polytopia name and
  `/player timezone` while preserving prefix aliases during transition;
- show the canonical name in `/player show` and offer an authorized edit
  control rather than adding a separate name/code lookup command;
- inventory existing mobile-name, Steam-name, and legacy-code values and
  define deterministic migration/conflict behavior before removing or
  overwriting any stored field;
- avoid exposing sensitive identifiers in public error messages.

`/player register` should open a small modal/workspace rather than ask users
to choose platform or name type as slash options. This phase is a suitable
proving ground for a reusable non-ELO write executor if P2 and P4 demonstrate
common infrastructure.

## P7 — Read-heavy commands and analytics

Status: **Implemented; pending beta acceptance**

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

### P7.1 — Player leaderboard read and pagination foundation

Status: **Implemented; integrated, beta acceptance pending**

Branch/base: `codex/p7-1-player-leaderboard`, stacked from published P4.1d
review checkpoint `b45e3ea`. The leaderboard taxonomy is not disputed, but
this unit must remain independently reviewable and must not imply acceptance
or integration of P4.1d's still-reviewed game-command spellings.

Objective: preserve the complete `$lb` behavior while moving its query into a
bounded worker-local read service and adding typed
`/leaderboard players`.

In scope:

- Preserve `$lb`, `$leaderboard`, `$leaderboards`, `$lbglobal`, and `$lbg`.
- Represent the existing freely combinable dimensions as typed options:
  - `scope: local|global`;
  - `rating: current|peak`;
  - `era: current|all-time`;
  - `population: active|all`.
- Preserve the current fewer-than-ten fallback pending a later rules decision.
- Return immutable primitive leaderboard rows and page metadata.
- Use a bounded read executor separate from the ELO and ordinary-write
  executors.
- Give each worker job its own Peewee connection.
- Add public component pagination for slash results while preserving prefix
  reaction pagination during transition.
- Defer the slash interaction before worker submission.

Out of scope:

- `/leaderboard activity`, teams, squads, or role-filtered leaderboards.
- Converting legacy `$lbteamjr`; it remains prefix-only and receives no slash
  equivalent.
- Changing ELO formulas, eligibility rules, fallback membership, or prefix
  aliases.
- Beta launch or command synchronization without separate approval.

Tests required:

- [x] complete option-matrix mapping
- [x] worker-local connection and primitive result boundary
- [x] deterministic ranking and page boundaries
- [x] slow-query event-loop responsiveness
- [x] bounded concurrent reads
- [x] slash registration, typed options, immediate defer, and component pages
- [x] prefix aliases and legacy filter combinations preserved
- [x] complete offline suite
- [x] gated development-database suite

Implementation evidence:

- `modules/leaderboard_workers.py` owns a dedicated two-thread read executor,
  opens and closes each worker-local Peewee connection, and returns frozen
  primitive rows capped at the existing 2,000-row display limit.
- `modules/leaderboard_views.py` renders immutable ten-row pages with
  requester-controlled First/Previous/Next/Last buttons. Results and page
  changes remain public, matching the shared visibility of the prefix
  leaderboard.
- `$lb` and all four aliases now use the shared bounded service while retaining
  reaction pagination and every legacy filter combination.
- `/leaderboard players` exposes all sixteen combinations through four typed
  choices and defers publicly before the database read.
- The existing model eligibility query and its fewer-than-ten fallback remain
  unchanged; this unit delegates selection to that model method rather than
  redefining the rule.
- No compatibility-ledger entry is required: all `$lb` result dimensions are
  represented natively, the prefix aliases remain available, and slash
  autocomplete makes a separate `/lb` registration unnecessary.

Validation:

- Focused leaderboard and taxonomy suite: 13 passed.
- Complete offline suite: 151 passed, with 9 explicitly gated database tests
  skipped.
- Gated development-database suite: 8 passed and 1 fixture round-trip skipped
  to preserve the existing operator-managed fixtures. The gate confirmed
  `POLYBOT_ENV=development`, database `polytopia_dev`, and role
  `polybot_dev`; the real-schema leaderboard worker test passed.
- Syntax compilation and `git diff --check`: passed.

Limitations:

- Only the existing player ELO leaderboard is included. Activity, team,
  squad, and role-filtered rankings retain their prefix interfaces for later
  units.
- The immutable snapshot preserves the existing 2,000-row cap. Its footer
  distinguishes the full ranked count from the loaded/displayable row count.
- Button state is process-local and expires after 120 seconds; the public
  rendered page remains visible afterward.

P7.1 is committed at `6774d31` with roadmap evidence at `cd65d24`. Its beta
acceptance is intentionally batched with P7.2/P7.3. Integration remains
sequenced behind the unresolved P4.1d review checkpoint on which these
branches are stacked.

### P7.2 — Player activity leaderboard

Status: **Implemented; integrated, beta acceptance pending**

Branch/base: `codex/p7-2-3-activity-squad-leaderboards`, stacked from P7.1
checkpoint `cd65d24`.

Objective: preserve the two distinct `$lbrecent` activity views while adding
typed `/leaderboard activity` and moving all database work into the bounded
leaderboard read service.

Native choices deliberately describe the complete legacy view rather than
pretending time and scope are independently combinable:

- `server-30-days` — this guild's player participation over the last 30 days;
- `global-all-time` — cross-guild Discord-member participation over all
  non-pending recorded games.

In scope:

- Preserve `$lbrecent`, `$recent`, `$active`, and `$lbactivealltime`.
- Return immutable primitive rows with existing limits of 500 server rows and
  1,000 global rows.
- Reuse public requester-controlled component pagination.
- Preserve current ordering, ELO display, team emoji behavior, and activity
  counts.

### P7.3 — Squad leaderboard

Status: **Implemented; integrated, beta acceptance pending**

Branch/base: shared P7.2/P7.3 branch above.

Objective: preserve `$lbsquad [alltime]` while adding typed
`/leaderboard squads period:current|all-time` and replacing its default
executor/database closure with the bounded leaderboard read service.

In scope:

- Preserve `$lbsquad` and `$squadlb`.
- Preserve the current 365-day eligibility cutoff and `alltime` override.
- Preserve the model's adaptive minimum-games eligibility rule.
- Return immutable squad ID, name, member names/emojis, ELO, and record values.
- Reuse public requester-controlled component pagination.

Shared validation requirements:

- [x] exact prefix-alias/view mapping
- [x] typed slash registration and immediate public defer
- [x] worker-local connection and immutable primitive DTO boundary
- [x] deterministic activity and squad page rendering
- [x] bounded concurrency and event-loop responsiveness
- [x] existing model eligibility and row limits preserved
- [x] complete offline suite
- [x] gated development-database suite

Out of scope:

- Team leaderboard graph generation and Discord role-derived member counts.
- Role-filtered leaderboard sorting/export behavior.
- Changing activity or squad eligibility rules.
- Beta launch or command synchronization without separate approval.

Implementation evidence:

- Implementation checkpoint: `6f6ca1c`.
- Both prefix commands now submit immutable requests to the same dedicated
  two-thread leaderboard executor introduced in P7.1.
- Activity preserves its original two non-combinable query shapes and removes
  two unused per-row record queries that never affected displayed output.
- Squad snapshots contain only primitive IDs, names, emoji strings, ELO, and
  record counts; no live Peewee model reaches Discord rendering.
- `/leaderboard activity` uses human-readable **This server — past 30 days**
  and **Global — all time** choices.
- `/leaderboard squads` uses **Current eligibility** and **All time** choices.
- Both slash commands defer publicly, reuse their prefix command checks, and
  render public requester-controlled component pages.
- The shared view base now supplies identical first/previous/next/last,
  unauthorized-user denial, and timeout behavior for all three leaderboard
  types.
- No compatibility-ledger entry is required: every displayed legacy view,
  alias, filter, limit, and eligibility rule remains available.

Validation:

- Focused leaderboard/taxonomy suite: 22 passed.
- Complete offline suite: 160 passed, with 9 explicitly gated database tests
  skipped.
- Gated development-database suite: 8 passed and 1 fixture round-trip skipped
  to preserve the existing operator-managed fixtures. The gate confirmed
  `POLYBOT_ENV=development`, database `polytopia_dev`, and role
  `polybot_dev`; real player, activity, and squad queries all passed.
- Syntax compilation and `git diff --check`: passed.

Limitations:

- Team leaderboard and role-filtered leaderboard work remains deferred as
  recorded above.
- Native pagination holds an immutable process-local snapshot for 120 seconds;
  users rerun the command for refreshed rankings.
- The global all-time activity view retains the current non-pending-game
  definition, while the server 30-day view retains its broader dated-game
  definition. This unit labels that difference but does not change it.

Next action: create implementation and roadmap checkpoints, then obtain
separate approval for one development-guild sync and combined beta test of
`/leaderboard players`, `/leaderboard activity`, and
`/leaderboard squads`.

### P7.4 — Shared leaderboard page-jump modal

Status: **Implemented; integrated, beta acceptance pending**

Branch/base: `codex/p7-4-leaderboard-jump-modal`, stacked from P7.2/P7.3
checkpoint `bb15fa8`.

Objective: make large immutable leaderboard snapshots practical to navigate
without adding command names or database work.

In scope:

- Replace the disabled page-count indicator with an active **Page X/Y** button.
- Open a numeric page-jump modal from that button.
- Validate non-numeric and out-of-range input ephemerally.
- Update the existing public leaderboard message on valid submission.
- Keep controls requester-only and disable them at the existing timeout.
- Apply the same behavior to player, activity, and squad leaderboards.
- Perform no query, model access, or snapshot refresh during page jumps.

Tests required:

- [x] button opens the modal for the requester
- [x] valid first, middle, and last page submissions update public output
- [x] non-numeric and out-of-range submissions are rejected ephemerally
- [x] unauthorized users remain unable to open controls
- [x] timeout disables the page-jump button with the other controls
- [x] existing paginator and complete offline suites remain green

Out of scope:

- Persistent paginator state across bot restarts.
- Refreshing database results from the modal.
- New leaderboard commands or taxonomy decisions.
- Beta launch or command synchronization without separate approval.

Implementation evidence:

- Implementation checkpoint: `fa61510`.
- The shared page indicator is now an active **Page X/Y** button that opens
  `JumpToPageModal` for player, activity, and squad results.
- The modal uses discord.py 2.7's modern `Label` plus numeric `TextInput`
  component rather than the deprecated directly labelled text-input shape.
- Valid submissions update the original public message through the existing
  immutable renderer; no query, worker submission, or model access occurs.
- Invalid, unauthorized, and expired submissions respond ephemerally without
  changing the public page.
- Timeout disables the jump button with every other component.

Validation:

- Jump-modal tests: 5 passed.
- Combined leaderboard tests: 22 passed.
- Complete offline suite: 165 passed, with 9 explicitly gated database tests
  skipped.
- Syntax compilation and `git diff --check`: passed.
- Development-database integration was not rerun because P7.4 changes only
  in-memory Discord component behavior and performs no database operation.

Next action: create implementation and roadmap checkpoints, then include page
jumps in the separately approved combined leaderboard beta smoke test.

### P7.5 — Experimental Components v2 player leaderboard

Status: **Complete**

Branch/base: `codex/p7-5-lb2-components-v2`, stacked from P7.4 checkpoint
`ba717de`.

Objective: test whether Discord Components v2 can make the common player
leaderboard flow more discoverable and reduce slash-option overload without
changing the accepted `/leaderboard players` interface.

In scope:

- Add a temporary guild-only `/lb2` command with no slash options.
- Render the public result entirely through a Components v2 `LayoutView`,
  `Container`, `TextDisplay`, separators, select menu, and action buttons.
- Default to this server/current ELO/active players.
- Switch among common local/global, current/peak, and current/all-time presets
  inside the message.
- Toggle active versus all players inside the message.
- Support previous/next, numeric page jump, and requester-rank navigation.
- Keep page/rank changes on the immutable snapshot; load alternate presets
  lazily through the existing bounded worker and cache them in the view.
- Keep controls requester-only while leaving the displayed result public.
- Seed a separately owned 24-player, 48-game showcase in `polytopia_dev`
  through the existing fixture CLI and unchanged profile/live-identity gates.

Out of scope:

- Replacing `/leaderboard players` or deciding the production command name.
- Adding activity, squad, or team views to the experiment.
- Persistent interaction state across restarts.
- Any production synchronization, production database access, schema change,
  or dependency change.

Fixture safety:

- Showcase profiles use an exact reserved Discord-ID range and exact generated
  player/member names.
- Showcase games use a separate exact notes marker and generated-name set.
- Status, seed, and confirmed cleanup independently recheck development
  profile, live database `polytopia_dev`, role `polybot_dev`, guild, and every
  ownership marker.
- The existing reusable beta games and real development users are not owned by
  this fixture set.

Evidence so far:

- Locked discord.py 2.7.1 supplies `LayoutView`, `Container`, `TextDisplay`,
  `Separator`, `ActionRow`, modern modal `Label`, selects, and buttons; no
  dependency change was needed.
- Focused Components v2, fixture, player-leaderboard, and taxonomy tests:
  31 passed.
- Complete offline suite: 174 passed with 9 gated database skips.
- Existing gated development-database suite: 8 passed with 1 intentional
  operator-fixture-preserving skip.
- Initial real seed created player IDs `163`-`186` and game IDs `200`-`247`.
  A model title-case normalization mismatch was caught by the idempotency
  check, corrected without weakening ownership, and the second seed then
  returned the same IDs.
- The real bounded local leaderboard worker returned 26 rows, including all
  24 showcase profiles with four recent ranked games each.

Next action: integrate the accepted P7.5 checkpoint in sequence, retain its
showcase fixtures, and implement the P7.6 toolkit/promotion unit separately.

Beta launch and acceptance evidence:

- Implementation checkpoint: `c4b34df`.
- Development preflight reconfirmed beta application `479029527553638401`,
  database `polytopia_dev`, the one approved guild `478571892832206869`, and
  background tasks/API/Bullet disabled.
- The bot authenticated as **PolyELO Bot Beta** and Discord accepted exactly
  four guild roots: `game`, `leaderboard`, `lb2`, and `elo`.
- The user tested `/lb2` on desktop and mobile and reported it strictly
  superior to the existing player leaderboard.
- The task-owned beta stopped cleanly after acceptance.
- All 24 showcase profiles and 48 games are intentionally retained for later
  leaderboard/component testing.

### P7.6 — Reusable Components v2 toolkit and leaderboard promotion

Status: **Complete; integrated into the accumulation branch**

Branch/base: `codex/p7-6-components-toolkit` from accumulation checkpoint
`39d09f6`.

Objective: turn the accepted P7.5 experiment into a small reusable interaction
foundation and make it the production-intended player leaderboard without
prematurely generalizing every Discord view.

In scope:

- Extract only the patterns proven by P7.5:
  - complete Components v2 container/text rendering;
  - public-result/requester-control authorization;
  - standard loading, ephemeral error, timeout, and expired-rerun behavior;
  - cached async view switching;
  - previous/next, numeric page jump, and requester-rank navigation;
  - recursive component disabling at timeout.
- Keep immutable DTOs and bounded worker loaders outside the presentation
  toolkit; the toolkit must not know Peewee or open database connections.
- Promote the accepted no-option interaction to `/leaderboard players`.
- Remove temporary `/lb2` before production synchronization; it is a test
  registration, not a long-term alias.
- Retain the legacy prefix `$lb` grammar during transition.
- Preserve all sixteen player-leaderboard combinations through an
  **Advanced filters** interaction over scope, rating, era, and population;
  common presets remain one-click choices and the initial slash command stays
  option-free.
- Decide whether direct-linkable presets are valuable enough for one optional
  `view` choice; do not restore the four-dimensional slash option matrix.
- Apply the shared primitives to at least one additional existing leaderboard
  view (activity or squads) so the abstraction is proven by two consumers.
- Add serialization/component-count, desktop/mobile layout checklist,
  authorization, cache, load-failure, timeout, and restart-expiration tests.

Out of scope:

- Converting unrelated commands while the taxonomy is still under review.
- Persisting arbitrary live view objects or database snapshots across bot
  restarts.
- A universal UI framework for every command.

Exit criteria:

- `/leaderboard players` delivers the accepted Components v2 experience with
  no temporary `/lb2` registration;
- every legacy `$lb` dimension remains reachable through the common presets
  or Advanced filters interaction, so no compatibility-ledger gap is added;
- shared toolkit code has at least two real consumers and no database imports;
- ordinary prefix behavior, bounded reads, public transparency, and event-loop
  responsiveness remain green;
- complete offline and gated database suites pass;
- a separately approved development-guild beta confirms the promoted path.

Next action: P7.5 is integrated. Implement P7.6 as a separate bounded unit,
then use its proven primitives in P7.7.

Implementation evidence:

- `modules/components_v2.py` contains only presentation/state primitives:
  requester authorization, page slicing/counting, cached async snapshot
  loading, numeric page jumps, recursive timeout disabling, and explicit
  rerun guidance. It imports neither Peewee nor database models.
- `/leaderboard players` is now the accepted no-option public workspace.
  Temporary `/lb2` is absent.
- Common presets remain one-click choices. A separate advanced selector
  exposes all sixteen scope/rating/era/population combinations represented by
  the legacy `$lb` grammar.
- `$lb`, `$leaderboard`, `$leaderboards`, `$lbglobal`, and `$lbg` remain
  registered and retain their existing filter parser and bounded worker.
- Player pages, requester-rank jumps, and cached preset/filter navigation do
  not requery the database. Only an uncached filter selection invokes the
  existing bounded leaderboard loader.
- `/leaderboard activity` is the second real toolkit consumer and now renders
  its immutable worker result through the same public/requester-controlled
  Components v2 page primitives.
- Serialization stays below the tested Components v2 count limit; timeout
  disables nested controls, unauthorized and expired interactions fail
  ephemerally, and expired page submissions direct users to rerun the command.

Validation evidence:

- Focused leaderboard/taxonomy suite: 30 passed.
- Complete offline suite: 185 passed with 9 explicitly gated database tests
  skipped.
- Existing gated development-database suite: 8 passed and 1 operator-fixture
  round trip skipped after confirming `development`, `polytopia_dev`, and
  role `polybot_dev`.
- Compilation and `git diff --check`: passed.

Compatibility: no ledger entry is required. Every legacy player-leaderboard
dimension remains reachable, all prefix aliases remain, and slash command
completion makes a separate `/lb` or `/lb2` application-command alias
unnecessary.

Remaining limitations:

- Activity gains the shared v2 presentation but retains its direct slash
  `view` choice; redesigning that already clear two-view interface was not
  needed to prove the toolkit.
- Cached view state remains process-local and intentionally expires. A bot
  restart requires rerunning the command.
- Squad leaderboard retains the earlier component paginator; migrating every
  existing view was explicitly outside this bounded extraction.

Commit(s):

- `981fa0f` — Promote Components v2 leaderboard toolkit.
- `e77e69b` — Merge P7.6 into
  `codex/database-slash-modernization`.

Beta result: D-026 restart from `981fa0f` stopped the prior task-owned beta
cleanly, authenticated as **PolyELO Bot Beta** (`479029527553638401`), and
completed development startup/synchronization without a reported error.
Functional desktop/mobile smoke acceptance of the promoted command remains
pending.

Next action: smoke the promoted player and activity leaderboards during the
combined P7.6/P7.7 beta session.

### P7.7 — Unified player profile and game-history workspace

Status: **Complete; integrated into the accumulation branch**

Branch/base: `codex/p7-7-player-workspace` from P7.6 accumulation merge.

Objective: replace overlapping player-card and single-player game-list
presentations with one Components v2 workspace while preserving prefix
commands as direct links to the relevant initial section.

Canonical native entry:

- `/player show member:[optional]`, defaulting to the requester;
- no slash `/elo PLAYER` alias;
- no required slash `section` option unless later usage demonstrates a real
  direct-link need.

The workspace opens on **Overview** and provides component navigation among
profile, current/peak/local/global/all-time ratings, recent games, incomplete
games, completed games with all/win/loss filters, season games, team/squad
context, and permitted profile actions.

Transition behavior:

- `$player`, `$elo`, and `$rank` open Overview/ratings;
- `$incomplete` opens incomplete games;
- `$complete` and `$completed` open completed games;
- `$wins` opens completed games filtered to wins;
- `$loss` and `$losses` open completed games filtered to losses;
- `$allgames PLAYER` opens all/recent games only when exactly one player
  resolves.

Complex multi-player, team, title/notes, game-size, or `all` searches remain
the `/game search` workflow. The workspaces may share immutable game-row DTOs,
paging, and rendering primitives from P7.6, but retain separate bounded query
services and semantics.

Implementation requirements:

- use worker-local connections and immutable snapshots for database-backed
  reads;
- keep snapshot-only tab changes and pagination off the database;
- preserve public competitive-result transparency with requester-only
  controls and ephemeral authorization failures;
- retain existing permission boundaries for profile edits;
- test initial-section routing for every prefix entry, pagination,
  season/result filters, cache/load failures, timeouts, restart expiration,
  responsiveness, and requester-default behavior;
- obtain desktop/mobile beta acceptance before integration.

This unit follows P7.6 and does not depend on final approval of unrelated
game/team taxonomy spellings.

Implementation evidence:

- Added optional-member `/player show`; omission targets the requester and
  the command has no `section` option or `/elo` slash alias.
- One public Components v2 workspace opens on Overview and provides Ratings,
  Recent, Incomplete, Completed, Season, and Team & squads sections.
  Completed games refine to all/wins/losses; season games refine by recorded
  season.
- Overview includes the canonical Polytopia name, team, timezone, current
  local/global rating and records. Ratings adds current/peak/local/global and
  permanent all-time values and ranks.
- A two-thread bounded player-read service owns each Peewee connection and
  returns one frozen profile plus immutable game-row snapshot capped at 500
  games. It is separate from leaderboard and future `/game search` query
  services.
- All section, filter, page, and page-jump navigation uses that immutable
  snapshot and performs no database query. Timeout recursively disables
  controls and tells users to rerun after expiration/restart.
- `$player`, `$elo`, and `$rank` deep-link Overview; the hidden
  `$player ... alltime` modifier deep-links Ratings, where current and
  permanent values are both present.
- `$incomplete` deep-links Incomplete; `$complete`/`$completed` deep-link
  Completed; `$wins` and `$loss`/`$losses` select the appropriate Completed
  result filter.
- `$allgames PLAYER` opens Recent only when the complete input resolves to
  exactly one player. Multi-player, team, title/notes, game-size, `all`, and
  otherwise complex inputs continue through the separate legacy game-search
  path pending `/game search`.
- Existing bot-channel and registered-member command checks remain attached.
  Results are public, controls are requester-only, and failures/unauthorized
  interactions are ephemeral.
- Profile actions are shown only to the target or staff and currently direct
  users to the existing permission-checked prefix edit commands; native
  mutations remain P6 work.

Validation evidence:

- Focused player-workspace/taxonomy suite: 15 passed.
- Complete offline suite: 196 passed with 10 explicitly gated database tests
  skipped.
- Existing gated development-database suite: 9 passed and 1
  operator-fixture-preserving skip after confirming `development`,
  `polytopia_dev`, and `polybot_dev`; the new real-schema player snapshot
  read passed.
- Compilation and `git diff --check`: passed.

Compatibility implications:

- The former prefix player card's generated ELO-history image, requester
  head-to-head follow-up, trophies, favorite-tribe summary, and pre-Moonrise
  miscellaneous statistics are not yet represented in the new workspace.
  Core identity, rating, record, rank, team, timezone, and game-history data
  are preserved. This gap is recorded in the compatibility ledger rather than
  silently treated as parity.
- The player service returns at most 500 game rows per snapshot. The UI
  discloses and paginates the loaded snapshot; more complex/unbounded history
  belongs in `/game search` or an export.

Commit(s):

- `58c8224` — Add unified player profile workspace.
- `c00f3ca` — Merge P7.7 into
  `codex/database-slash-modernization`.

Beta result: D-026 restart from `58c8224` stopped the prior P7.6 beta cleanly,
authenticated as **PolyELO Bot Beta** (`479029527553638401`), and completed
development startup/synchronization without a reported error. The beta
remains the intended default runtime. The user accepted the combined P7.6/P7.7
smoke as a sufficient proof of concept on 2026-07-30.

Next action: P7.7 is integrated. Implement and beta-smoke the bounded
`/game search` workspace. C-002 analytics restoration is explicitly deferred
until usage demonstrates which legacy details justify a separate bounded
unit.

### P7.8 — Unified game-search workspace

Status: **Complete; integrated into the accumulation branch**

Branch/base: `codex/p7-8-game-search-workspace` from the P7.7 accumulation
merge.

Objective: add the accepted `/game search query:[optional]` Components v2
workspace and move complex prefix game searches to a separate bounded,
worker-local read service without changing their grammar.

Interface and behavior:

- the optional query accepts the legacy mix of Discord mentions, players,
  teams, uppercase title/notes terms, and arbitrary side shapes such as
  `1v1v1`;
- the public result provides requester-only status, outcome, common-size,
  pagination, and page-jump controls;
- `status:unconfirmed` is omitted for ordinary users and independently
  staff-checked in the worker;
- outcome uses the first resolved player, or first team when no player
  resolves, matching the legacy `$wins`/`$losses` rule;
- cached pages and previously loaded filter combinations perform no database
  read; new filter combinations use the separate bounded search executor;
- `$allgames`, complex `$incomplete`/`$complete`, and complex
  `$wins`/`$losses` retain their argument grammar and deep-link the matching
  initial workspace filter;
- bare prefix forms still default to the requester, while the explicit `all`
  token remains unscoped;
- one-player incomplete results retain their game-side channel link.

Implementation evidence:

- added frozen request/key/result/game-row DTOs in
  `modules/game_search_workers.py`;
- added a two-thread bounded executor with a worker-local Peewee connection
  and a 500-row result cap;
- added `modules/game_search_views.py` using only the database-independent
  P7.6 Components toolkit;
- added `/game search` as one optional string option and kept `/player show`
  and game search on independent read services;
- removed the complex prefix path's unbounded default-executor closure and
  unmanaged connection.

Validation evidence:

- focused game-search/taxonomy suite: 21 passed;
- complete offline suite: 213 passed with 11 explicitly gated database tests
  skipped;
- gated development-database suite: 10 passed and one
  operator-fixture-preserving skip after confirming `development`,
  `polytopia_dev`, and `polybot_dev`; the real-schema game-search read passed;
- compilation and `git diff --check`: passed.

Commit(s):

- `d6bebcd` — Add unified game search workspace.
- `79f5185` — Merge P7.8 into
  `codex/database-slash-modernization`.

Compatibility implications:

- prefix parsing and result scope are preserved, but presentation changes from
  reaction pagination to public Components v2 controls;
- slash exposes common size choices while arbitrary and unequal side shapes
  remain available through the query grammar;
- results are capped at 500 rows per immutable snapshot and disclose
  truncation; broader exports remain separate;
- C-002 player-card analytics remain explicitly deferred and are not part of
  this unit.

Next action: review and beta-validate the separately branched P7.9 unified
game-detail workspace before selecting the smallest independently testable P8
team/house or administration unit whose slash path is no longer ambiguous.

Beta result: D-026 launch from `d6bebcd` authenticated as **PolyELO Bot
Beta** (`479029527553638401`) and synchronized exactly the `game`,
`leaderboard`, `player`, and `elo` roots to development guild
`478571892832206869`. A narrow preflight process match missed an older beta
started from P7.7. Live testing exposed duplicate prefix replies and
`CommandNotFound` errors from that stale process. Host-wide process inspection
identified both exact development `bot.py --skip_tasks` PIDs; the older
process stopped cleanly with SIGINT and only the current P7.8 beta remains.
The user then accepted `/game search`, its filters/navigation, and the
representative prefix deep links as working.

### P7.9 — Unified game-detail workspace

Status: **Implemented; beta acceptance pending; not integrated**

Branch/base: `codex/p7-9-game-detail-workspace` from the exact clean
`codex/database-slash-modernization` checkpoint `16fc6565dccddc4341c8925b1667beba041c7384`.

Objective: implement the D-025-approved public `/game show game_id:[optional
integer]` detail workspace and route numeric `$game GAME_ID` / `$match GAME_ID`
through the same bounded read and presentation path without touching the
unsettled team/house taxonomy.

Interface and behavior:

- `/game show` accepts exactly one optional integer `game_id`. With no ID, the
  worker uses `Game.by_channel_id` only when the current channel has one
  associated game; no match and multiple matches return an ephemeral request
  for an explicit ID.
- Numeric `$game` and its preserved `$match` alias open the same public
  workspace. Nonnumeric `$game`/`$match` input still delegates to the existing
  game-search workflow. Prefix failures remain public; slash failures are
  ephemeral, including timeout and expired-control reruns.
- The initial public Components v2 card covers game name, status/result,
  ranked state, size/platform, dates/deadline, map, notes, season metadata,
  series summary, host, sides, tribes, ELO labels, and relevant game/side
  channel links. Secondary requester-controlled sections expose players/sides,
  status/dates, attributes, and channels without another database read.
- Cross-guild explicit reads remain visible to the same public audience with a
  source-guild compatibility banner. Pending cross-guild reads are withheld
  before a snapshot is returned, matching legacy `$game` behavior. Nonpending
  cross-guild cards use plain database names only: source member mentions,
  role mentions, and game/side channel links are not resolved. The native
  `/game` group remains guild-only, while `/game show` adds no new bot-channel
  or registration check beyond the legacy numeric prefix behavior.
- Pending snapshots preserve the legacy open-game join guidance, configured
  per-guild prefix, platform/friend-name values, full-game creator start/codes
  guidance, and balanced draft order as immutable primitives. Prefix
  configuration is supplied by the event-loop/display adapter, never the DB
  worker.
- No mutation was added. Controls are requester-only, unauthorized and
  expired interactions are ephemeral, and the result itself remains public.

Implementation and boundary evidence:

- `modules/game_detail_workers.py` adds a two-thread bounded executor, frozen
  primitive request/snapshot/side/lineup/draft DTOs, worker-local Peewee
  connection ownership, channel inference, meaningful invalid/not-found
  errors, pending operational metadata, and an optional frozen series summary.
- `modules/game_detail_views.py` uses the P7.6 Components v2 toolkit. It
  resolves Discord members, roles, channels, guild labels, and local/remote
  winning-player/team imagery outside the worker. It applies the legacy
  cross-guild privacy boundary and renders pending join/start/codes/draft
  guidance. Local team files are reattached when a section is edited so
  `attachment://` media remains valid.
- `modules/games.py` provides the shared slash/prefix adapter and 20-second
  bounded read timeout. `tests/test_game_detail_workspace.py` covers the
  registration shape, routing, channel inference outcomes, visibility,
  worker lifecycle, event-loop responsiveness, immutable navigation, media,
  timeout, expiry, and Components v2 limits.
- `tests/test_database_integration.py` adds one read-only real-schema worker
  check under the existing strict development database gate.

Validation evidence:

- focused game-detail suite: 24 passed;
- complete offline suite: 238 passed with 12 explicitly gated database tests
  skipped;
- compilation and `git diff --check`: passed;
- runtime preflight selected `POLYBOT_ENV=development`, `polytopia_dev`,
  `polybot_dev`, development guild `478571892832206869`, and disabled
  background tasks/API;
- gated development suite: 12 passed and one retained operator-fixture skip;
  the new real-schema game-detail worker read passed under the unchanged gate.

Commit:

- `24a435b` — Add unified game detail workspace.
- `22023f4` — Restore game detail parity boundaries.
- `60989a5` — Record the Tier 2 parity-correction handoff.

Compatibility implications:

- No new compatibility-ledger row is required: the bounded snapshot preserves
  the material `Game.embed` card fields, optional two-side series summary,
  pending join/start/codes/draft guidance, and local/remote winning imagery
  while moving the public presentation to Components v2. The old cross-guild
  summary-plus-message remains a public compatibility notice, pending cards
  are withheld cross-guild, and nonpending cards do not resolve source-only
  member/role/channel identifiers.
- Audit logs and mutation/permitted-action controls remain separate from this
  read-only unit; no database logic is duplicated for them. Pending-game join
  and lifecycle mutations continue through their existing permission-checked
  prefix/native commands.

Beta result: **D-026 launch/sync succeeded; exactly one corrected development
beta is currently running; interactive smoke pending.** A sandboxed process
check incorrectly reported no beta because it could not see sibling Codex
task/PTY sessions. Sol's escalated host-wide process view then found
production PID `1534787` (untouched), old Luna beta PID `1784646`, and a
transient duplicate PID `1788948`. Only duplicate `1788948` was immediately
stopped; old beta `1784646` was then cleanly stopped because it had loaded
pre-correction code. That brief duplicate/cleanup episode is operational
evidence, not a successful steady state. Corrected branch HEAD `60989a5` was
launched as exactly one development beta, now PID `1790485`, with cwd in the
managed P7.9 worktree. It authenticated as **PolyELO Bot Beta**
(`479029527553638401`) and synced exactly four application-command roots
(`game`, `leaderboard`, `player`, `elo`) only to guild `478571892832206869`.
Interactive Discord/mobile/desktop smoke remains pending. Production
processes, checkouts, services, and databases were not operated on.

Known limitations and next action:

- The workspace does not add a game-log section or new mutation buttons; those
  remain later bounded units/paths. If optional historical series data cannot
  be read, the primary immutable card still renders without that optional
  line. Cross-guild pending games intentionally expose only the association
  error, matching the legacy privacy boundary.
- Have Sol or an available Discord client perform the explicit-ID,
  channel-inference, numeric-prefix/search, and desktop/mobile smoke against
  the single beta, then return the exact handoff packet to Sol for Tier 2
  review. Keep the beta/fixtures/process state within D-026 and do not
  integrate this branch from Luna.

## P8 — League and remaining administration workflows

Status: **Planned**

Candidate domains:

- team and house administration;
- drafts, trades, promotions, and auction operations;
- migrations and player deletion;
- purge/background maintenance;
- exports and backup-command review;
- no API, Bullet, or anti-scam work unless a separate decision reactivates or
  retires those legacy modules.

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

Before deployment, implement and test the single-process guild capability
policy described in D-031. The initial production observation stage should:

- keep legacy prefix commands enabled;
- expose new native/component commands only in the explicitly approved
  PolyChampions canary guild;
- use the same production bot process and in-process services for both
  interfaces;
- verify that no beta or second bot process connects as another writer to
  `polytopia2`;
- define an immediate configuration/code rollback that removes the canary
  native surface without removing prefix access.

Do not use a beta process connected to `polytopia2` as the canary mechanism.

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

Essential invocation inputs:

Interactive refinements / Components v2 decision:

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
- [ ] Components v2 serialization/count/authorization/timeout, when relevant
- [ ] desktop and mobile layout smoke test for a novel interaction
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
| Components v2 state | Controls authorize correctly, cached navigation avoids unnecessary reads, load failures preserve coherent state, and timeout/expiry has a rerun path |
| Components v2 layout | Payload serializes within Discord limits and novel layouts receive desktop/mobile beta evidence |
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
Message-content commands are thin transition adapters only: preserve their
names, permissions, argument resolution, and initial workspace mapping, then
delegate to the same bounded service and Components v2 presentation used by
slash commands. Do not add prefix-only UI, pagination, database execution
paths, or new features. Limit prefix work to narrow compatibility regressions
while message-content intent remains available; removing these adapters later
must not affect the slash services, DTOs, or renderers.

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
was initially rejected because the available design assumed free-text member
entry, which would discard Discord's native member selection and recreate the
ambiguity of prefix parsing. Discord and discord.py 2.7.1 now support native
user selects and additional typed components inside modals. D-022 therefore
reopens a modal/component-driven custom draft as a viable future mitigation
without changing the accepted initial bounded interface.

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

Status: Accepted

T-A domain groups are the working architecture for native development.
Prefix commands remain stable, and grouped slash commands are thin adapters
over existing shared worker/application paths. Because the surface has not
reached production, checkpoint `63af179` migrates the current beta
registrations cleanly without preserving top-level aliases. Any later
taxonomy change requires an explicit compatibility decision.

### D-019 — Use one game namespace across lifecycle states

Status: Accepted

Open, joinable, pending, started, completed, ranked, and unranked records are
all “games” in normal user language. The revised T-A taxonomy therefore
removes the user-facing `/match` root and places matchmaking lifecycle actions
under `/game`. Legacy code/model names and prefix aliases may continue to say
“match” internally during migration.

The unified legacy inventory has 28 capability rows. Typed `/game search`
filters consolidate `allgames`, `incomplete`, `wins`, and the joinable `games`
list; typed `/game ping` scope consolidates `ping` and `pingall`. This yields
at most 24 named child commands if every remaining candidate ships, below
Discord's 25-child limit. P4.1d's unsynchronized `/match extend` and
`/match unstart` were replaced locally before synchronization. The user
approved this structure on 2026-07-30 before any `/match` command was
synchronized. This was the original flat-child capacity calculation;
taxonomy v2.2's accepted attribute-command refinement instead proposes
nineteen immediate `/game` children, including two subcommand groups.

### D-020 — Use component-first journey paths and semantic subgroups

Status: Proposed; awaiting user/staff review

Taxonomy v2.2 retains the accepted domain-root architecture and unified
`/game` vocabulary but optimizes the tree for common user flows. Direct
commands cover open/join/leave/start, recording, viewing, searching, player
names, and reporting a winner. Uncommon result corrections use
`/game result ...`; uncommon lifecycle operations use `/game manage ...`.

The proposal prefers `/game record` over `/game create`,
`/game players` over `/game player-names`, and
`/game search status:unconfirmed` over `/game unconfirmed`. It rejects a
generic `/game get ...` group because outcome-oriented read names are shorter
and clearer. D-021's accepted attribute-command rule further removes a generic
`set` group where the individual value is useful to inspect. D-023 further
keeps invocation short by moving exploratory choices into Components v2.

Checkpoint `63af179` remains unchanged while this decision is reviewed. If
accepted before production, apply the rename cleanly in the registration
layer without production compatibility aliases, update registration tests and
the beta runbook, and verify that the approved development-guild sync prunes
the older beta-only paths.

### D-021 — Let useful attribute commands read or edit

Status: Accepted

For a property that users reasonably inspect on its own, use the property as
the command: `/team emoji`, `/team image`, `/team server`, `/game map`,
`/game notes`, `/house image`, and similar paths. Omitting the replacement
value displays the current setting. Supplying a value performs the
permission-checked edit. Clearing uses an explicit `clear` option because an
omitted value already means “view.”

The target may be optional only when it can be inferred unambiguously;
otherwise use a typed/autocompleted target. Read and mutation permissions are
evaluated separately, and a current value is exposed only when safe for the
requester. This pattern does not apply to actions such as win, join, confirm,
delete, or unstart.

This accepted refinement replaces v2's generic game/team/house/squad `set`
subgroups. The overall v2.2 taxonomy remains under review, and no command code
has changed.

### D-022 — Use modal components for multi-field interaction design

Status: Proposed; awaiting user/staff review and command-specific units

The locked discord.py 2.7.1 environment exposes Discord's newer modal
components, including native user/role/channel selectors, file uploads, radio
groups, checkboxes, text inputs, labels, and explanatory text. Modals are
therefore no longer limited to ambiguous free-form text.

Prefer modals when a workflow collects several related fields, long text, or
an attachment. Prefer direct slash options for short one-step actions, and
prefer message buttons/selects for pagination, iterative editing, previews,
and destructive confirmation.

Best candidates are:

- an arbitrary-side `/game record` draft with native member selectors,
  add/edit-side controls, review, and confirmation;
- `/game notes` editing;
- `/player register`;
- team/house creation and image upload;
- `/staffhelp`;
- longer game notifications with optional uploads.

Modal submission is a fresh interaction. Collect inputs before work, then
defer the modal submission before any bounded worker/database operation.
Permissions, primitive thread boundaries, synchronous transactions, and
post-commit Discord effects remain unchanged. Draft state should be
short-lived and in-memory initially unless restart persistence becomes a
demonstrated requirement.

### D-023 — Prefer Components v2 workspaces over slash option matrices

Status: Accepted

Desktop and mobile beta testing found the no-option P7.5 `/lb2` workspace
strictly preferable to the four-option `/leaderboard players` response.
Slash commands should identify the task and essential target; message
components should handle exploratory filters, alternate views, pagination,
cached refreshes, and iterative review.

This is a design preference, not a requirement to make every response
interactive. Simple one-step mutations retain direct typed inputs.
Database-backed view changes continue through bounded workers, while
snapshot-only navigation remains database-free. Public competitive results
retain requester-only controls and ephemeral authorization failures.

P7.6 will extract only the component behavior proven in P7.5, require at least
two real consumers, promote the accepted UI to `/leaderboard players`, and
remove temporary `/lb2` before production. Later units must classify proposed
slash options as essential invocation inputs or interactive refinements.

### D-024 — Incorporate component-first taxonomy feedback

Status: Accepted as command-specific direction; overall taxonomy v2.2 remains
under review

Four user/staff decisions refine the system-wide proposal:

1. `/game ping` is an interactive composer. The invocation accepts only an
   optional game target when channel inference cannot identify it. Audience,
   scope, long-form text, uploads, preview, and confirmation are
   interactive refinements. The composer must accept multiple attachments.
   Discord currently permits up to 4,000 characters in one modal text input
   and up to 10 files in one File Upload component, so the design uses
   repeatable text sections and bounded multi-message delivery rather than
   claiming an unlimited message. These limits must be rechecked against
   Discord's
   [component reference](https://docs.discord.com/developers/components/reference)
   during implementation.
2. `/player register` uses one canonical Polytopia name. The native interface
   does not distinguish mobile name, Steam name, or legacy friend code.
   Existing prefix aliases and stored fields remain until a separately tested
   migration resolves records that contain conflicting values.
3. The optional Bullet surface originally proposed `/bullet log`. D-029
   supersedes this item: Bullet is now legacy and receives no active slash or
   component conversion.
4. The familiar top-level `/staffhelp` name is preserved. It opens a
   structured modal/workspace for game reference, long description, and
   multiple uploads; `/support request` is removed from the proposal.

These decisions simplify invocation without changing permission boundaries.
Notification delivery, player-field migration, and staff-help routing each
remain separate bounded implementation units.

### D-025 — Keep explicit show commands and established ping/logs vocabulary

Status: Accepted as command-specific naming; overall taxonomy v2.2 remains
under review

Keep `/game show` and `/player show` as the explicit detail commands. Discord
command groups do not provide a default no-subcommand action, so the native
surface accepts the extra `show` word instead of creating a misleading
alternative. `/player show` defaults to the requester. `/game show` may infer
the current game only from unambiguous context; otherwise it requests a game
ID or selection.

Use `/game ping`, not `/game notify`, for the interactive notification
composer. Use `/game logs`, not `/game history`, for permission-aware game
audit records. These names preserve established user vocabulary and map
directly to the current `$ping`/`$pingall` and `$logs` commands without
changing the proposed Components v2 interaction design.

This is a naming decision inside the proposal, not final approval of Taxonomy
v2.2. No registration change is authorized by this decision.

### D-026 — Keep the development beta running between work units

Status: Accepted

The development beta is now expected to be running by default. After a
significant code unit is completed, validated, and checkpointed, restart only
the beta process from that intended checkpoint so runtime code and the
development-guild command tree are current. A routine beta restart includes
the existing development-guild-only synchronization performed at startup.

Before launching or restarting, verify the development environment, beta
application identity, `polytopia_dev` profile, disabled background tasks/API,
configured development guild, current branch/worktree, and absence or exact
identity of every existing development `bot.py --skip_tasks` process. The
process view must be host-wide and capable of seeing sibling Codex task/PTY
sessions; a sandboxed `ps` result is not sufficient evidence that no beta is
running. Match the process command independently of whether its Python path
is absolute, compare working directories, start times, and ancestry, identify
production processes separately, and confirm exactly one beta remains after
restart. A brief development duplicate and its immediate cleanup is
operational evidence to record, not an accepted steady state. Never stop a
production process while cleaning up a development duplicate. Do not rely
only on the current task's attached terminal session. Keep the bot stopped
while fixture seed/cleanup tooling requires exclusive access.

This standing authorization does not apply to production operations, global
command synchronization, other guilds or runtime profiles, dependency
installation, or materially broader live tests.

### D-027 — Retire per-game platform distinctions under cross-play

Status: Accepted

Mobile and Steam now have full Polytopia cross-play, so new slash commands
must not ask for a per-game platform. As related commands are modernized,
remove platform-specific native choices and treat `newsteamgame`,
`newsteamgameunranked`, `pingmobile`, `pingsteam`, platform filters, display
emoji, and dual stored-name behavior as legacy compatibility surfaces.

P2.3 removes the platform option from `/game record` and stores the existing
database Boolean at its canonical compatibility value. The Boolean and
historical rows remain until a separately gated schema/data cleanup can
retire all readers safely. Prefix aliases remain registered during transition
but must not imply a meaningful platform difference in newly recorded games.

### D-028 — Use a roster string plus component review for `/game record`

Status: Accepted, implemented, integrated, and beta-validated

`/game record` takes the exact game name, one roster string using the familiar
`vs` grammar, and an optional ranked flag. It parses arbitrary and unequal
sides, resolves members, and presents a requester-only Components v2 preview.
Only Confirm submits the existing validation/worker/post-commit pipeline;
Edit sides uses a native Discord member selector and add/remove-side controls,
while Cancel makes no database or Discord change.

This staged design restores the prefix command's flexible shapes without
message-content intent and avoids a large fixed slash option matrix. Mentions
remain the safest initial input; the native side editor corrects the parsed
draft without exposing raw mention strings.

### D-029 — Keep Bullet and anti-scam as legacy maintenance-only modules

Status: Accepted

The Bullet tournament cog and anti-scam listener are not active migration,
slash-conversion, Components v2, or modernization targets. This decision also
supersedes D-024's proposed `/bullet log` workflow. The legacy API cog remains
excluded under the same planning policy.

Legacy classification does not disable or decommission current behavior.
These modules may remain loaded until a separately approved retirement unit
decides whether and how to remove them. While retained, narrowly necessary
operational, security, privacy/retention, and dependency-compatibility fixes
remain in scope. Broader redesign, new native registrations, or feature
expansion require a new explicit decision.

### D-030 — Unify player detail and simple game lists in one workspace

Status: Accepted, implemented, and beta-smoke accepted in P7.7

Use `/player show member:[optional]` as the one native entry to an interactive
player workspace. It defaults to the requester and opens Overview. Components
then navigate ratings, recent/incomplete/completed/season games, result
filters, teams/squads, and permitted profile actions.

Preserve `$player`/`$elo`/`$rank`, `$incomplete`,
`$complete`/`$completed`, `$wins`, `$loss`/`$losses`, and the single-player
form of `$allgames` as deep links to matching initial sections. Do not create
slash aliases for this historical vocabulary. Keep complex search semantics
under `/game search`.

P7.7 records the bounded implementation and evidence requirements. Shared
presentation primitives may come from P7.6, but player-detail and game-search
database services remain distinct.

### D-031 — Use one writer process and a guild-scoped production canary

Status: Accepted

Do not run the beta and production bots concurrently against `polytopia2`.
Current ELO coordination, per-game claims, component state, fixture
coordination, and reconciliation are process-local. Two writer processes
could bypass serialization, duplicate prefix/listener handling, or perform
conflicting Discord effects.

Preserve compatibility in three layers:

- semantic parity is mandatory for permissions, validation, mutations,
  transactions, audit attribution, coordinator use, and post-commit effects;
- prefix names/aliases remain thin invocation adapters over the same bounded
  services as native commands;
- Components v2 may intentionally change presentation when the compatibility
  ledger records material omissions and beta evidence covers the new flow.

A temporary classic renderer is allowed only for a justified high-use or
high-risk transition, must consume the same DTO/service, and needs a removal
condition. Separate classic and modern mutation implementations are
prohibited.

After P9 approval, production observation uses one production bot process:
legacy prefixes remain enabled while new native/component capabilities are
initially enabled only for the approved PolyChampions guild. Expansion and
prefix deprecation are later evidence-based decisions. The capability-policy
implementation and rollback are a separate pre-P9 unit.

### D-032 — Split Sol oversight and Luna execution with worktree isolation

Status: Accepted

Use Sol-Medium for roadmap reconciliation, bounded-unit planning, prompts,
and integration review. Use Luna-Max experimentally for implementation,
self-review, tests, and the unit handoff. Sol normally reviews once per
bounded branch at its integration gate rather than every Luna commit.

Review depth is risk-tiered:

- Tier 1 documentation/test/isolated presentation work receives a lightweight
  integration review;
- Tier 2 read workers, Components workspaces, registration, visibility, and
  permissions receive a complete branch review;
- Tier 3 mutations, destructive operations, coordination, schema/data,
  security/privacy, or production work receive design review before coding
  and a complete final review.

The primary checkout and Luna execution worktree, ownership rules, unit
lifecycle, prompt header, and required handoff packet are authoritative in
`docs/MODERNIZATION_COLLABORATION_WORKFLOW.md`. Worktrees prevent branch/index
collisions but do not isolate host processes, database state, Discord, or Git
refs and do not grant operational authority.

## Progress log

### 2026-07-31 — P7.9 Tier 2 parity corrections

- Withheld pending cross-guild game-detail cards before returning a snapshot;
  retained nonpending cross-guild public detail while suppressing source
  member/role/channel Discord identifiers.
- Preserved pending open-game join guidance and full-game start, codes, and
  balanced-draft guidance in immutable worker DTOs, with the configured prefix
  resolved only on the event-loop/display side.
- Added focused privacy and pending-parity coverage. The corrected branch now
  passes 24 focused tests, 238 offline tests with 12 gated skips, and 12 gated
  development tests with one retained-fixture skip.
- At the correction handoff, the prior D-026 beta had exited. Interactive
  Discord smoke remained pending.

### 2026-07-31 — P7.9 corrected D-026 beta evidence refreshed

- A sandboxed `ps` check incorrectly reported no beta because it could not see
  sibling Codex task/PTY sessions; it was not sufficient host-wide evidence.
- Sol's escalated host-wide process view found production PID `1534787`
  (untouched), old Luna beta PID `1784646`, and transient duplicate PID
  `1788948`. Only duplicate `1788948` was immediately stopped; old beta
  `1784646` was then cleanly stopped because it held pre-correction code. The
  brief duplicate/cleanup episode is recorded as operational evidence, not a
  successful steady state.
- Corrected branch HEAD `60989a5` was launched as exactly one development beta,
  now PID `1790485` in the managed P7.9 worktree. It authenticated as
  **PolyELO Bot Beta** (`479029527553638401`) and synced exactly four roots
  (`game`, `leaderboard`, `player`, `elo`) only to development guild
  `478571892832206869`.
- Interactive Discord/mobile/desktop smoke remains pending. Production
  processes, checkouts, services, and databases were not operated on.

### 2026-07-31 — P7.9 game-detail workspace implemented

- Added the D-025-approved `/game show game_id:[optional integer]` public
  Components v2 workspace with unambiguous current-channel inference and
  explicit-ID fallback errors.
- Routed numeric `$game`/`$match` through the same immutable worker snapshot;
  preserved nonnumeric prefix delegation to game search and public-prefix /
  ephemeral-slash failure visibility.
- Added a bounded worker-local read service and event-loop-only Discord display
  resolution, including series metadata and safe local/remote game imagery.
- Added 19 focused tests, a strict gated real-schema test, and updated the
  taxonomy implementation-state notes. The complete offline suite passed 233
  tests with 12 gated skips.
- Recorded the app-managed Codex task worktree as the preferred Luna execution
  path; the manually prepared `.worktrees/luna` checkout is fallback only.
- Passed the unchanged gated `polytopia_dev` suite: 12 tests passed and one
  retained operator-fixture set was skipped; the real-schema game-detail read
  passed.
- Launched one D-026 development beta from the managed worktree as
  **PolyELO Bot Beta** and verified host-wide exactly-one process state. Live
  Discord client/mobile/desktop smoke remains pending because this headless
  execution task has no client surface.
- Beta acceptance and accumulation-branch integration remain pending Sol's
  Tier 2 review and interactive D-026 smoke gate.

### 2026-07-31 — Compatibility canary and Sol/Luna workflow accepted

- Rejected concurrent beta/production writers against one database while job
  coordination and reconciliation remain process-local.
- Adopted semantic parity, retained prefix invocation, and intentionally
  evolvable Components v2 presentation as separate compatibility layers.
- Chose a future single-production-process, PolyChampions-guild canary with
  legacy prefixes retained instead of a beta process on `polytopia2`.
- Added a risk-tiered Sol planning/review and Luna execution workflow with one
  isolated reusable development worktree and unit-level integration reviews.
- Recorded that worktree isolation does not isolate runtime processes or
  broaden database, Discord, production, push, or merge authority.

### 2026-07-30 — P7.8 beta smoke accepted

- Accepted `/game search`, its status/outcome/size/page controls, and
  representative complex prefix deep links.
- Confirmed that the observed duplicate prefix panels came from two beta
  processes rather than command fall-through.
- Approved P7.8 for accumulation-branch integration.
- Reaffirmed prefix/message-content commands as low-investment migration
  adapters rather than a continuing feature surface.

### 2026-07-30 — P7.8 game-search workspace implemented

- Added option-light `/game search` with interactive status, outcome, size,
  and page controls.
- Preserved complex legacy player/team/title/notes/size parsing and prefix
  initial-state mappings in a separate bounded worker-local query service.
- Kept public results, requester-only controls, ephemeral failures, immutable
  cached navigation, and the 500-row disclosure policy.
- Added focused and real-schema gated coverage; beta acceptance remains.
- Launched the D-026 development beta from the implementation checkpoint and
  confirmed synchronization only to the configured development guild.
- Diagnosed duplicated prefix panels as two simultaneous beta processes,
  stopped only the stale P7.7 process, and strengthened D-026's host-wide
  duplicate-process verification.

### 2026-07-30 — P7.7 beta smoke accepted

- Accepted the unified `/player show` workspace and promoted leaderboard as a
  sufficient Components v2 proof of concept.
- Approved P7.7 for integration into the accumulation branch.
- Kept C-002 visible in the compatibility ledger while deferring its analytics
  and media restoration to a later evidence-driven unit.
- Selected the bounded `/game search` workspace as the next code unit.

### 2026-07-30 — P7.7 unified player workspace implemented

- Added optional-member `/player show`, defaulting to the requester, with one
  public Components v2 Overview/Ratings/game-history/team workspace.
- Routed the overlapping player and simple single-player prefix commands to
  their documented initial sections while leaving complex queries in the
  separate game-search workflow.
- Added a bounded worker-local immutable player snapshot and database-free
  section/filter/page navigation.
- Passed 15 focused tests, 196 offline tests with 10 gated skips, and 9 gated
  development-database tests with one fixture-preserving skip.
- Recorded the remaining legacy player-card analytics gap as C-002.

### 2026-07-30 — P7.6 Components v2 toolkit promoted

- Extracted database-agnostic requester authorization, cached loading,
  pagination, expiry, and timeout primitives proven by P7.5.
- Promoted the accepted no-option experience to `/leaderboard players`,
  exposed all sixteen legacy `$lb` combinations interactively, and removed
  temporary `/lb2`.
- Applied the toolkit to player and activity leaderboards.
- Passed 30 focused tests, 185 offline tests with 9 gated skips, and 8 gated
  development-database tests with one fixture-preserving skip.
- Integrated the unit before branching P7.7.

### 2026-07-30 — Legacy exclusions and unified player workspace proposed

- Marked the five-command Bullet tournament cog/results listener and the
  command-free anti-scam listener as legacy maintenance-only modules.
- Superseded the earlier `/bullet log` proposal without disabling either
  legacy runtime feature; the API cog remains excluded.
- Reconciled the proposal with the current v2.2 accumulation-branch taxonomy
  rather than an older side-branch draft.
- Proposed one `/player show` Components v2 workspace for profile, ratings,
  recent/incomplete/completed/season games, and result filters.
- Recorded existing single-player prefix commands as initial-section deep
  links while keeping complex queries in `/game search`.
- Added P7.7 after the reusable P7.6 toolkit unit.
- Made documentation-only changes; command registrations, runtime,
  synchronization, databases, fixtures, and production remained untouched.

### 2026-07-30 — P2.3 and stacked component work integrated

- Verified `codex/p2-3-game-record-roster` was a clean linear descendant of
  the accumulation branch and contained the reviewed P4.1d-through-P7.5
  dependency stack.
- Merged it into `codex/database-slash-modernization` with explicit merge
  checkpoint `688b9d6`.
- Verified the merged tree is identical to the beta-validated P2.3 branch and
  `git diff --check` passes.
- Marked P2.3 and beta-validated P7.5 Complete. Earlier stacked units that
  still lack explicit beta acceptance are recorded as integrated but remain
  Implemented.
- Left Taxonomy v2.2 pending final approval and kept the development beta
  running.

### 2026-07-30 — `/game record` beta accepted

- The user retested the corrected workflow and reported that it worked well.
- Accepted the roster parser, preview/confirmation flow, and native
  side/member editor.
- Observed no further live exception in the task-owned beta output.
- Marked P2.3 Beta-validated; final integration and the broader Taxonomy v2.2
  decision remain separate.
- Left the development beta running under D-026.

### 2026-07-30 — `/game record` beta findings fixed

- Live confirmation exposed that component interactions have no application
  command data and cannot be passed to `Context.from_interaction`.
- Confirmed from the traceback that both failed attempts stopped before the
  worker and made no game/database changes.
- Retained the original slash context through the short-lived preview and
  used it for the existing prefix/worker pipeline on confirmation.
- Replaced the raw mention-string Edit modal with a native side editor:
  choose a side, replace its players through Discord's user selector, add or
  remove sides, and return to review.
- Made the finished preview report completion or unexpected failure instead
  of remaining indefinitely at “Creating the game…”.
- Passed 18 focused and 183 complete offline tests with nine gated skips.
- The existing gated database result remains valid because this correction
  changes interaction/context handling only; no worker or schema behavior
  changed.

### 2026-07-30 — Flexible `/game record` implemented

- Replaced the fixed two-sided `/game create` option matrix with
  `/game record game_name roster ranked`.
- Added arbitrary/unequal-side parsing, the requester shortcut, and a
  requester-controlled Components v2 preview with Edit/Confirm/Cancel.
- Kept worker submission behind Confirm and retained the P2.1 synchronous
  transaction/post-commit boundary.
- Removed platform from the slash interface and treated retained Steam prefix
  aliases as cross-play compatibility names rather than a distinct game type.
- Passed 16 focused tests, the 181-test offline suite with nine gated skips,
  and eight gated `polytopia_dev` tests with one safe fixture skip.
- Recorded implementation checkpoint `6af7c92`; later beta acceptance is
  recorded above and P2.3 is Complete.

### 2026-07-30 — `/game record` beta restarted and synchronized

- Stopped only the standing task-owned development beta cleanly.
- Restarted from roadmap checkpoint `da21786` with the development profile and
  background tasks disabled.
- Confirmed authentication as `PolyELO Bot Beta`
  (`479029527553638401`) and startup/synchronization without a reported error.
- Left the beta running under D-026 for functional
  preview/edit/cancel/confirm acceptance, which was subsequently reported and
  integrated.

### 2026-07-30 — Standing development-beta runtime policy accepted

- Launched the beta from checkpoint `5f01aba` with
  `POLYBOT_ENV=development` and `--skip_tasks`.
- Confirmed authentication as `PolyELO Bot Beta`
  (`479029527553638401`) and successful startup.
- Accepted a running-by-default policy with restart after significant,
  validated work units so code and development-guild registrations stay
  current.
- Kept production, global synchronization, alternate profiles/guilds,
  background tasks, API enablement, dependency changes, and broader live
  tests outside this standing authorization.

### 2026-07-30 — Show, ping, and logs naming accepted

- Retained `/game show` and `/player show` as explicit detail commands.
- Recorded requester-default behavior for `/player show` and
  context-sensitive inference for `/game show`.
- Renamed the proposed `/game notify` composer to `/game ping`.
- Renamed the proposed `/game history` audit view to `/game logs`.
- Left final approval of Taxonomy v2.2 pending and made no registration,
  runtime, synchronization, database, fixture, or production change.

### 2026-07-30 — Component-first taxonomy v2.2 proposed

- Applied the accepted P7.5 interaction lesson across the full taxonomy:
  slash commands identify the task and essential target, while optional
  filters, long-form input, uploads, previews, and paging move into
  Components v2 workspaces.
- Added an invocation-versus-interaction matrix for the major game, player,
  team, leaderboard, Bullet, and staff-help journeys.
- Redesigned the game notification workflow as a previewed composer with
  multiple uploads (subsequently named `/game ping` by D-025),
  repeatable text sections, and bounded multi-message delivery. Recorded
  Discord's concrete per-input/file limits instead of promising unlimited
  messages.
- Collapsed mobile name, Steam name, and legacy friend code into one canonical
  native Polytopia-name concept while requiring a separate stored-data
  migration decision.
- Proposed participant-facing `/bullet log`; D-029 later superseded this
  proposal when Bullet was classified as legacy.
- Preserved the established top-level `/staffhelp` name and removed
  `/support request` from the proposal.
- Made documentation-only changes; command registrations, beta runtime,
  Discord synchronization, databases, fixtures, and production remained
  untouched.

### 2026-07-30 — Components v2 interaction preference accepted

- The user accepted `/lb2` after desktop and mobile testing and described it
  as strictly superior to the regular player leaderboard.
- Stopped the task-owned beta cleanly after testing.
- Retained all 24 owned showcase profiles and 48 games for future component
  and leaderboard work.
- Accepted D-023: task/target inputs stay on slash commands while exploratory
  filters, views, pages, edits, previews, and confirmation should normally
  move into Components v2.
- Added P7.6 for a bounded reusable toolkit, promotion to
  `/leaderboard players`, removal of temporary `/lb2`, and a second real
  toolkit consumer.
- Made Components v2 suitability and option classification part of every
  later slash-conversion review without changing unresolved taxonomy names.

### 2026-07-30 — Components v2 leaderboard showcase implemented

- Created `codex/p7-5-lb2-components-v2` from the green P7.4 checkpoint.
- Added temporary no-option `/lb2` as a public Components v2 experiment,
  without changing `/leaderboard players` or settling the broader taxonomy.
- Used a `LayoutView` container, markdown text displays, preset select,
  active/all toggle, requester-only paging, numeric page modal, and **My
  rank** navigation.
- Kept page and rank changes database-free; alternate views use the existing
  bounded two-thread leaderboard worker and are cached per message.
- Extended immutable player rows with primitive Discord IDs solely for
  requester-rank lookup.
- Added separately gated/idempotent leaderboard fixture status, seed, and
  confirmed-cleanup operations.
- Seeded 24 owned profiles and 48 ranked games into `polytopia_dev`; verified
  an idempotent rerun retained player IDs `163`-`186` and game IDs `200`-`247`.
- Passed 31 focused tests, 174 complete offline tests with 9 gated skips, and
  8 gated database tests with 1 operator-fixture-preserving skip.
- No dependency, schema, production, service, or production database change
  occurred.
- Committed the implementation as `c4b34df`, then launched the explicitly
  approved development profile. Discord synchronized `game`, `leaderboard`,
  temporary `lb2`, and `elo` only to guild `478571892832206869`; the user
  subsequently accepted the UI on desktop and mobile.

### 2026-07-30 — Shared leaderboard page-jump modal implemented

- Replaced the disabled page indicator with a requester-controlled
  **Page X/Y** button across player, activity, and squad leaderboards.
- Added a modern modal `Label` and numeric `TextInput` with ephemeral
  non-numeric, range, authorization, and expiry validation.
- Kept valid jumps public and database-free by rendering the existing
  immutable snapshot.
- Passed 5 focused modal tests, 22 combined leaderboard tests, and the complete
  165-test offline suite with 9 gated skips.
- Did not run database integration because the unit has no database path, and
  did not launch or synchronize the beta bot.

### 2026-07-30 — Activity and squad leaderboards implemented

- Added `/leaderboard activity` with explicit server-30-days and
  global-all-time views, preserving `$lbrecent`, `$recent`, `$active`, and
  `$lbactivealltime`.
- Added `/leaderboard squads` with current/all-time eligibility, preserving
  `$lbsquad` and `$squadlb`.
- Routed both prefix and slash reads through the P7.1 two-thread
  worker-local leaderboard executor.
- Generalized public requester-controlled component pagination across player,
  activity, and squad leaderboard snapshots.
- Preserved activity query definitions and row limits plus the squad model's
  adaptive minimum-games rule.
- Passed 22 focused tests, 160 offline tests with 9 gated skips, and 8 gated
  `polytopia_dev` tests with 1 intentional fixture-preservation skip.
- Did not launch the beta bot, synchronize Discord commands, modify fixture
  ownership, or perform production work.

### 2026-07-30 — Player leaderboard matrix and native pagination implemented

- Audited `$lb` as four independently combinable dimensions, documented all
  sixteen combinations, and retained `$leaderboard`, `$leaderboards`,
  `$lbglobal`, and `$lbg`.
- Recorded `$lbteamjr` as legacy prefix-only functionality with no planned
  slash conversion.
- Added the canonical `/leaderboard players` path with typed scope, rating,
  era, and population choices.
- Moved both prefix and slash player-leaderboard reads to a dedicated bounded
  worker-local service returning immutable primitive snapshots.
- Added public requester-controlled component pagination while preserving
  prefix reaction pagination.
- Passed 13 focused tests, 151 offline tests with 9 gated skips, and 8 gated
  `polytopia_dev` tests with 1 intentional fixture-preservation skip.
- Did not launch the beta bot, synchronize Discord commands, modify fixture
  ownership, or perform production work.

### 2026-07-30 — Modernization review checkpoints published

- Committed the current taxonomy v2.1, attribute-command, and modal/component
  discussion as `40bc816`.
- Published `codex/database-slash-modernization` as the stable accumulation
  checkpoint through integrated P4.1b/P4.1c.
- Published `codex/p4-1d-match-slash-group` as the current review branch
  containing P4.1d registration code and the still-unapproved taxonomy v2.1
  proposal.
- Passed the complete offline suite: 142 tests passed with eight gated
  development-database tests skipped as designed.
- Opened no pull request and performed no merge, beta launch, Discord
  synchronization, database operation, or production action.

### 2026-07-30 — Taxonomy review paused and modal opportunities recorded

- Paused new registration implementation while staff continue reviewing
  taxonomy v2.1, registration scope, management-guild placement, and possible
  future web administration.
- Confirmed the locked discord.py 2.7.1 environment supports typed selectors,
  file uploads, radio groups, checkboxes, and text inside modals.
- Reopened an arbitrary-side game-recording draft as a viable future C-001
  mitigation because native user selection can now remain inside the modal
  workflow.
- Recorded focused candidates for modal/component UX without authorizing code,
  beta launch, synchronization, database work, or a web-architecture pivot.

### 2026-07-30 — Attribute-focused read/edit rule accepted

- Accepted `/team emoji|image|server|name|house|tier` as focused commands
  instead of placing them under `/team set`.
- Defined omission as a read, a supplied replacement as a permission-checked
  edit, and explicit `clear:true` as removal.
- Applied the same rule where useful to
  `/game name|map|tribe|notes|side|ranked`, `/squad name`,
  `/house name|image`, and `/player timezone`.
- Recalculated the proposed `/game` root at nineteen immediate children,
  leaving six slots under Discord's 25-child limit.
- Left the rest of taxonomy v2 under review and made no code, beta, Discord,
  database, or production change.

### 2026-07-30 — Journey-first system taxonomy v2 proposed

- Revisited the then-complete 83-handler taxonomy in response to user and
  staff feedback; D-029 later removed the five Bullet handlers from the active
  modernization target, leaving 78.
- Proposed `/game record`, `/game players`, and
  `/game search status:unconfirmed`.
- Kept common actions directly discoverable while grouping result corrections
  under `/game result` and uncommon lifecycle administration under
  `/game manage`. D-021 later refined metadata into focused read/edit commands.
- Applied the same direct-action/property/maintenance conventions across
  player, team, squad, leaderboard, league, house, ELO, the then-optional
  Bullet proposal, tools, about, and support domains. D-029 later excluded
  Bullet.
- Recorded that a generic `get` subgroup is less usable than explicit
  `/game show|search|players|logs` paths.
- Left checkpoint `63af179`, the beta runtime, Discord synchronization, and
  databases untouched. User/staff review is the next action.

### 2026-07-30 — Unified game taxonomy proposed

- Revised T-A so users see one `/game` domain for open, pending, started,
  completed, and corrected games; `/match` remains only legacy internal or
  prefix terminology.
- Proposed `/game open|join|leave|kick|start|unstart|extend` alongside the
  existing tracked-game operations.
- Consolidated overlapping list/history behaviors into typed `/game search`
  filters and `ping`/`pingall` into one scoped command, keeping the full
  candidate group at no more than 24 children against Discord's 25-child
  limit.
- Placed the unsynchronized P4.1d `/match` beta procedure on naming hold.
- Made no command-code, synchronization, runtime, database, production, or
  service change; user review is the next action.

### 2026-07-30 — Unified game taxonomy approved

- The user approved D-019 and requested that all native commands implemented
  in the prior pilot units conform before the next beta sync.
- Authorized clean migration of the development-only native surface to
  `/game create|win|unwin|delete|confirm|unconfirmed|set-ranked|extend|unstart`
  and `/elo recalculate|status`.
- Prefix commands and aliases remain unchanged. Old top-level slash names and
  the unsynchronized `/match` group receive no compatibility aliases because
  none has reached production.
- No Discord synchronization or beta launch occurred at approval time.

### 2026-07-30 — Existing native commands unified

- Migrated all native commands implemented through P4.1d to the approved
  `/game` and `/elo` roots in checkpoint `63af179`.
- Registered `/game create|win|unwin|delete|confirm|unconfirmed|set-ranked`,
  `/game extend|unstart`, and `/elo recalculate|status`; removed the
  development-only top-level native names and the never-synchronized
  `/match` root.
- Preserved all prefix commands and aliases and reused their existing checks,
  callbacks, workers, and post-commit behavior through thin adapters.
- Added exact-tree, typed-option, delegation, prefix-registration, and
  permission-check reuse coverage. The affected focused suite passed 74
  tests; the full offline suite passed 142 with eight gated skips; the gated
  database suite passed seven with one operator-fixture-preserving skip.
- Updated the fixture-backed beta procedure for all eleven subcommands. No
  beta launch or Discord synchronization occurred; that remains the next
  separately approved action.

### 2026-07-29 — Slash taxonomy review prepared

- Inventoried the then-complete in-scope repository-backed surface: 83
  explicit prefix handlers, customized framework help, 10 current native
  registrations, aliases, and the optional Bullet family. D-029 later
  excluded the five Bullet handlers, leaving 78 active targets, and also
  excluded the listener-only anti-scam module. The seven-command legacy API
  cog was already excluded.
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

### 2026-07-29 — Domain taxonomy provisionally selected

- Authorized T-A domain groups as the working development taxonomy without
  waiting for the staff vote.
- Removed the naming freeze for new slash work; the documented T-A spellings
  are now the defaults.
- Kept prefix names and aliases stable and kept registration changes separate
  from transaction-worker units.
- Selected a bounded current-surface registration migration as the next unit
  after P4.1b/P4.1c integration, followed by one separately approved beta
  synchronization and smoke session.
- Retained the ability to revise the slash registration layer before
  production if staff unexpectedly choose another taxonomy.

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
- Initially deferred the typed slash adapter under D-018; the subsequent
  provisional T-A decision authorizes `/match unstart` in the bounded
  domain-group registration unit.
- Passed nine focused tests and the complete offline suite: 132 passed with
  eight gated skips.
- Passed seven gated development-database tests; the fixture round-trip
  skipped to preserve operator fixtures after confirming `development`,
  `polytopia_dev`, and `polybot_dev`.
- Recorded implementation checkpoint `204ab40`. No beta launch, command
  synchronization, production operation, dependency change, or schema change
  was performed.
- Next: integrate P4.1b/P4.1c, implement the bounded domain-group registration
  unit, and use one separately approved beta session for `/match extend`,
  `/match unstart`, and preserved prefix behavior.

### 2026-07-29 — P4.1d match slash group implemented

- Integrated P4.1b/P4.1c into `codex/database-slash-modernization` as
  `31c84d7`, preserving their implementation checkpoints.
- Created `codex/p4-1d-match-slash-group` from that accumulation checkpoint.
- Added guild-only `/match unstart game_id` and moved the unsynchronized
  `/extend` adapter to `/match extend game_id`; prefix commands remain.
- Added worker-side invocation-channel revalidation for slash/prefix parity.
- Passed 21 focused command tests, the complete offline suite with 137 passes
  and
  eight gated skips, and seven gated development-database tests with the
  operator-fixture round trip safely skipped.
- Confirmed the beta was stopped and fixtures `149`-`151` remained available;
  enhanced status with pending/expiration fields and recorded the exact live
  smoke sequence in the fixture runbook.
- Recorded implementation checkpoint `416ca30`. No beta launch, Discord
  synchronization, production operation, dependency change, schema change,
  push, or PR was performed.
- Next: commit the evidence and request separate approval for the documented
  development beta sync/smoke session.

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
