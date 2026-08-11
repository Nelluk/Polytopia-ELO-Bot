# Modernization adversarial pre-production review

Date: 2026-08-10

Reviewed branch: `codex/database-slash-modernization`

Reviewed checkpoint: `8cb47aa`

Production baseline: `origin/master` at `c35e2f1`

Review mode: read-only. No PostgreSQL, Discord, production data, service,
dependency, sudo, or filesystem mutation was performed during the review.

## 2026-08-11 final-review supplement

The later GitHub-only adversarial review was pinned to
`55eeb84951085ff55bdbe2eeca4a33519b942c4b` and compared with `master` at
`c35e2f1d0011709d233c0aa8afa258602b457635`. It confirmed the matrix below but
found two additional release blockers. The accumulation branch subsequently
advanced to `92702262de60b7e7a73e6ec9d5286ba9d5b54419` only through the tracked
post-modernization-roadmap documentation, so both findings remained valid at
the P9.21 base.

| Finding | Severity | P9.21 disposition |
|---|---|---|
| N1 import/startup schema DDL | High | Resolved by P9.21 checkpoint `e532ce4`: model import performs no connection/DDL, startup verifies the required schema read-only before ban reconciliation, and schema creation exists only in the explicit development bootstrap apply path. |
| N2 production identity/password literal fallback | Medium | Resolved by P9.21 checkpoint `e532ce4`: explicit nonempty production `expected_bot_id` and `psql_password` are required before server-settings or later effects. |

The source findings are corrected, Tier-3 reviewed, integrated, pushed, and
runtime-verified through P9.21 integration checkpoint `0b4d954`. The guarded
beta startup proved the model-free read-only schema preflight precedes ban
reconciliation. The external review itself performed no tests or live
operations; P9.21's durable test, stopped-writer, and beta evidence is recorded
in the roadmap and must later bind to the exact candidate.

## Recommendation

The reviewed source blockers are resolved. B1–B3, H1–H8, M1–M6, L1, and the
later N1–N2 findings are complete through P9.21. The recommended bounded
pre-M7 beta-testability unit was selected as P9.22; its source and stopped-
writer validation are complete and integrated at `d58a2c7`, with beta
deployment pending.
After P9.22 closes, M7 is the final exact-HEAD release-candidate evidence gate.

## Current resolution matrix

| Finding | State | Durable evidence / next owner |
|---|---|---|
| B1 | Resolved | P9.15 production-only migration tooling |
| B2 | Resolved | P9.16 checkpoints `d754beb`, `8ddad6a`; merge `d4a37d0` |
| B3 | Resolved | P9.7a transaction truthfulness and P9.7b immutable publication |
| H1–H2 | Resolved | P9.12 explicit environment and native-start ban parity |
| H3 | Resolved | P9.14 global-tree inspect/apply guard |
| H4 | Resolved | P9.7b–P9.7d immutable result/correction snapshots |
| H5 | Resolved | P9.7e–P9.7g recurring-task workers/reconciliation |
| H6 | Resolved | P9.8, P9.9, and P9.13 retirements/replacements |
| H7 | Resolved | P9.10 repeated-cancellation-safe backup cleanup |
| H8 | Resolved | P9.11 identity-before-startup-effects ordering |
| M1–M5 | Resolved | P9.17 interaction boundaries and P9.18 backup lifecycle/provenance |
| M6 | Resolved | P9.20 direct per-guild production relay and all-guild capability policy |
| M7 | Open by design | R-002 final-HEAD release-candidate evidence |
| L1 | Resolved | P9.19 current-authority reconciliation and model-free consistency regression |

## Blocker

### B1 — The production timezone migration does not exist

Status: **Resolved by P9.15 implementation checkpoint `1c8ffa5`, evidence
checkpoint `c7333aa`, and accumulation merge `8b9ede1`; Tier-3 offline and
stopped-writer development-database validation are green. No production
connection or DDL was attempted.**

- **Location:** `modules/models.py:365`,
  `modules/player_timezone_migration.py:3`,
  `scripts/migrate_player_timezone.py:119`, and
  `docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md:88`.
- **Observable failure:** the model now selects `timezone_offset_minutes` and
  `timezone_offset_cleared`, but the only migration deliberately refuses
  production. Against the unchanged production table, ordinary
  `DiscordMember` reads can fail with undefined-column errors.
- **Why tests do not catch it:** `tests/test_player_timezone_migration.py:84`
  and `tests/test_player_timezone_migration.py:261` positively assert
  production refusal. There is no production plan/apply/verify implementation
  to test.
- **Smallest bounded correction:** add the already-specified production-only
  additive tool: connection-free plan by default, exact
  environment/configured/live database and role checks, exact
  acknowledgement, transactional/idempotent DDL, read-only verification, and
  no destructive rollback.
- **Focused regression:** offline identity/schema mismatch and transaction
  fault injection, plus a separately gated non-production
  apply/verify/idempotency test.
- **Resolution:** the dedicated production tool defaults to a connection-free,
  schema-qualified plan; requires exact environment, configured and live
  database/role identity, and typed acknowledgement before apply; inspects and
  verifies the exact additive shape; uses one transaction with a bounded lock
  timeout; and provides read-only verification with no destructive rollback.

### B2 — There is no modernization cutover and rollback runbook

Status: **Resolved by P9.16 implementation checkpoint `d754beb`, evidence
checkpoint `8ddad6a`, and accumulation merge `d4a37d0`; Tier-3 complete-diff
and offline validation are green. Production execution remains separately
gated.**

- **Location:** `docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md:105`,
  `docs/PRODUCTION_CUTOVER.md:1`, and `docs/PRODUCTION_CUTOVER.md:44`.
- **Observable failure:** the only runbook is the completed Python 3.12 upgrade
  record. It explicitly says there is no schema migration and contains a
  retired rollback. It cannot safely govern modernization schema ordering,
  single-writer proof, task-disabled canary, command-tree changes, or
  independent code/schema/tree rollback.
- **Why tests do not catch it:** `tests/test_deployment_assets.py:23` merely
  asserts that the historical Python upgrade checkpoints and legacy
  interpreter rollback remain present.
- **Smallest bounded correction:** create a modernization-specific runbook
  containing every R-004 requirement and clearly label the old document as
  historical only.
- **Focused regression:** a deployment-asset test enforcing the required
  ordering and rejecting the historical "no schema migration" procedure as
  modernization authority.

### B3 — A committed confirmation can be reported as rolled back

Status: **Resolved by P9.7a/P9.7b transactional audit and immutable
confirmation-publication work.**

- **Location:** transaction commit at `modules/elo_workers.py:333`;
  post-commit reload/publication at `modules/administration.py:178`; Discord
  effect before audit at `modules/games.py:6178`; rollback wording at
  `modules/administration.py:547` and `modules/administration.py:595`; automatic
  path at `modules/administration.py:666`.
- **Observable failure:** `confirm_game()` commits first.
  `post_win_messaging()` then performs Discord updates and only afterward
  writes `GameLog`. If that write or another post-commit query fails, the
  database remains confirmed and Discord may already be changed, while prefix
  and slash handlers say "failed and rolled back" and "No Discord channel
  updates were made." The automatic task can die after the committed mutation.
- **Why tests do not catch it:** `tests/test_elo_jobs.py:1352` mocks the whole
  `_confirm_game_and_post()` helper as failing, so it models only a pre-commit
  failure.
- **Smallest bounded correction:** write the authoritative actor/system audit
  inside the worker transaction. Return a committed immutable publication plan
  and treat subsequent failures explicitly as reconciliation failures, never
  rollbacks.
- **Focused regression:** inject failure after worker commit and after the
  first Discord effect; assert committed state and transactional audit remain,
  and user output says publication requires reconciliation. Separately inject
  audit failure inside the worker and assert rollback with zero Discord
  effects.

## High

### H1 — Unset `POLYBOT_ENV` silently selects production

Status: **Resolved by P9.12 implementation checkpoint `5038282`; Tier-3
review and offline validation are green.**

- **Location:** `runtime_config.py:483` and production defaults at
  `runtime_config.py:71`.
- **Observable failure:** an ad hoc invocation without `POLYBOT_ENV` loads
  production configuration and enables production-default tasks/API/Bullet.
  This contradicts the documented requirement that deployed commands
  explicitly select a profile.
- **Why tests do not catch it:** `tests/test_runtime_config.py:139` positively
  asserts that an unset environment selects production.
- **Smallest bounded correction:** require an explicit, exact `production` or
  `development` value.
- **Focused regression:** unset, blank, and whitespace-only values must fail
  before reading profile files, importing server settings, creating
  directories, or touching a database.
- **Resolution:** the raw environment value must now be exactly `production`
  or `development`. Missing, blank, whitespace-only, padded, and unknown values
  all fail before the first profile or filesystem effect; both tracked service
  units already set exact values.

### H2 — Native start paths bypass the prefix-wide ban gate

Status: **Resolved by P9.12 implementation checkpoint `5038282` and
real-schema correction checkpoint `7660b3e`; Tier-3 review and the stopped-
writer development-database gate are green.**

- **Location:** prefix-only checks at `bot.py:218`; slash entry at
  `modules/games.py:3269`; component entry at
  `modules/game_detail_actions.py:1194`; worker authorization at
  `modules/game_start_workers.py:247`.
- **Observable failure:** application commands and components do not run
  `bot.check`. `/game start` and the public Start component reach the worker
  without checking configured banned IDs or the `ELO Banned` role. The worker
  loads `DiscordMember` only to check registration and ignores persisted
  account/guild ban state. A banned host can therefore commit a start that
  `$start` rejects.
- **Why tests do not catch it:** `tests/test_game_start.py:465` covers
  staff/host authority but has no banned requester case.
- **Smallest bounded correction:** add a shared native-interaction ban adapter
  and revalidate persisted account/guild ban flags in the authoritative start
  worker.
- **Focused regression:** invoke slash and component starts for configured-ID,
  role-banned, account-banned, and guild-player-banned hosts; assert no commit,
  channel creation, public result, or card mutation.
- **Resolution:** `/game start` and the pending-card Start action now share a
  model-free configured-ID/role check and private denial. Both worker phases
  independently reload and reject current account or guild-player ban state,
  including a ban applied between preflight and transaction, before any
  mutation or publication.

### H3 — Guild-only command management never proves the remote global tree is empty

Status: **Resolved by P9.14 implementation checkpoint `8d5a65f`; Tier-2
review, complete offline validation, and live development inspection are
green.**

- **Location:** guild-only inspection at
  `scripts/manage_application_commands.py:278` and remote workflow at
  `scripts/manage_application_commands.py:360`.
- **Observable failure:** inspection and apply can report guild convergence
  while stale global registrations remain. Because corresponding callbacks are
  loaded locally, those registrations can be delivered in every guild,
  bypassing the default-deny capability assignment and PolyChampions-only
  canary boundary.
- **Why tests do not catch it:**
  `tests/test_application_command_management.py:138` deliberately preserves
  `global-only` and treats that as success.
- **Smallest bounded correction:** fetch and display the global command set
  read-only in remote modes, and refuse apply if it is nonempty. Removal can
  remain separately approved.
- **Focused regression:** a nonempty global snapshot must be reported and stop
  apply before any guild sync; an empty global tree preserves current behavior.
- **Resolution:** P9.14 makes both remote modes fetch and report the global
  tree read-only. Guild apply validates that snapshot before its first sync and
  fails closed with the observed root names when it is nonempty. Empty-global
  apply retains the explicit-guild-only behavior. The tool still has no global
  mutation or removal operation.

### H4 — Converted result and correction paths still perform synchronous ORM reloads and carry live models through awaits

Status: **Resolved across P9.7b, P9.7c, and P9.7d; immutable worker-loaded
snapshots now cover confirmation, ordinary win/unwin, and rank/unstart, and
their Discord publishers are model-free.**

- **Location:** ordinary win at `modules/game_win.py:250`; unwin at
  `modules/games.py:4546`; confirm/rank/unstart at
  `modules/administration.py:184`, `modules/administration.py:225`, and
  `modules/administration.py:285`; shared publishers at
  `modules/games.py:6168` and `modules/games.py:6201`.
- **Observable failure:** after worker commit and coordinator cleanup, adapters
  call `Game.load_full_game()` synchronously on the Discord event loop, retain
  the graph across channel/role awaits, and perform later ORM traversal and
  writes. A slow query stalls all interactions; lazy access can query from the
  event-loop connection after unrelated awaits; output can observe newer state
  after the game lock has been released.
- **Why tests do not catch it:** `tests/test_game_win_service.py:421` mocks
  `load_full_game()` and explicitly asserts unlock before post-effects;
  correction tests similarly replace the reload/publisher with mocks.
- **Smallest bounded correction:** worker-load one immutable post-commit
  effect/card snapshot containing only frozen channel, roster, season, mention,
  audit, and role inputs. Publishers must be model-free.
- **Focused regression:** block snapshot loading while an event-loop heartbeat
  runs; assert publishers receive no Peewee instances and cause no ORM calls.
  Allow a conflicting mutation after commit and verify publication remains
  based on the committed snapshot.

### H5 — Enabled production background tasks remain synchronous and non-reconciling

Status: **Resolved across P9.7e, P9.7f, and P9.7g; automatic confirmation,
champion reconciliation, and completed-channel purge now use bounded workers,
authoritative revalidation/reconciliation, cycle containment, and immutable
plans.**

- **Location:** task activation at `modules/games.py:271` and
  `modules/administration.py:131`; channel purge at `modules/games.py:6125`;
  delete-before-save at `modules/models.py:1347`; champion task at
  `modules/achievements.py:19`; auto-confirm selection at
  `modules/administration.py:617`.
- **Observable failure:**
  - completed-game purge runs a broad query on-loop, passes live `Game` objects
    through Discord deletion, and saves channel IDs only after deletion;
    cancellation/DB failure leaves stale IDs;
  - champion reconciliation performs synchronous leaderboard queries, retains
    models across role awaits, and audits after effects; when the global result
    is discarded at default ELO, line 65 dereferences `None` and can terminate
    the coroutine;
  - auto-confirm eligibility is selected on-loop and is not authoritatively
    revalidated inside the confirmation transaction.
- **Why tests do not catch it:** no test references
  `task_purge_game_channels`, `task_set_champion_role`, `confirm_auto`, or
  `task_confirm_auto`; modernized purge tests cover a different incomplete-game
  task.
- **Smallest bounded correction:** worker-owned immutable candidate discovery,
  transactional authoritative revalidation, and explicit post-effect
  reconciliation. Contain cycle exceptions so one record cannot kill a
  recurring task.
- **Focused regression:** slow-query heartbeat tests, stale auto-confirm
  eligibility, Discord-delete-success/DB-reconcile-failure, partial role
  failure, and proof that a later cycle still runs after an exception.

### H6 — Approved operator retirements remain executable, and destructive channel purge is unresolved

**Resolution:** Closed across P9.8, P9.9, and P9.13. P9.8 removes the obsolete
`gtest`, `ptrophies`, and `boost_from` handlers; P9.9 replaces the unsafe manual
purge with owner-only preview/selection/typed-confirmation reconciliation; and
P9.13 replaces the restart aliases with supervised `/operator bot restart`,
owner-only force confirmation, and deliberate exit status 75. Registry tests
require every retired prefix and alias to remain absent.

- **Location:** `$gtest` at `modules/league.py:1401`; `$ptrophies` at
  `modules/administration.py:2500`; `$boost_from` at
  `modules/administration.py:2555`; `$purge_game_channels` at
  `modules/administration.py:393`; accepted decision at
  `docs/DATABASE_AND_SLASH_MODERNIZATION.md:13552`; purge gate at
  `docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md:192`.
- **Observable failure:** `gtest` loads hard-coded game `135855` and invokes
  Nova role logic; `ptrophies` performs direct writes; `boost_from` applies
  roles across guilds before saving the database flag. Purge interleaves direct
  ORM access, public messages, audit writes, and irreversible Discord channel
  deletion with no preview or reconciliation.
- **Why tests do not catch it:** no retirement inventory checks these names;
  safe command discovery still registers `gtest`, `ptrophies`, `boost_from`,
  `boost_from_norole`, and `purge_game_channels`.
- **Smallest bounded correction:** remove the three explicitly approved
  obsolete handlers. Resolve purge separately by retirement or its
  already-specified preview/confirm/worker/reconciliation replacement.
- **Focused regression:** enumerate all prefix commands and aliases and assert
  approved retirements are absent; require every retained operator command to
  map to an explicit recorded disposition.

### H7 — Repeated cancellation can abandon a live backup child and clear coordinator ownership

**Resolution:** Closed by P9.10 implementation checkpoint `fd3ff8d`. Manual
backup cancellation now shields one terminate/reap/both-pipe-drain cleanup task
through repeated cancellation and retains coordinator ownership until that task
finishes. The focused regression blocks termination and output drain
independently, rejects overlap throughout, and proves cleanup precedes
cancellation propagation.

- **Location:** cleanup at `modules/operator_backup.py:400`; coordinator
  clearing at `modules/operator_backup.py:497`.
- **Observable failure:** after the first `CancelledError`, termination and
  stream draining are awaited without protection from a second cancellation. A
  second `cancel()` can interrupt cleanup; the coordinator's `finally` then
  clears `active` while the separately sessioned process group may still run
  and its pipes remain undrained.
- **Why tests do not catch it:** `tests/test_operator_backup.py:190` cancels
  once and its fake signal immediately releases the child.
- **Smallest bounded correction:** make termination, reaping, and pipe draining
  resistant to repeated cancellation and keep coordinator ownership until all
  cleanup completes.
- **Focused regression:** block termination, cancel twice, verify the
  coordinator remains active and rejects another request, then release the
  process and assert it is reaped/drained before cancellation propagates.

### H8 — The task-disabled startup canary writes the database before Discord identity validation

Status: **Resolved by P9.11 implementation checkpoint `27d4a1c`; Tier-3
review and stopped-writer development-database gate are green.**

- **Location:** unconditional ban reconciliation at `bot.py:61`;
  `--skip_tasks` behavior at `bot.py:32`; authenticated application validation
  at `bot.py:296`.
- **Observable failure:** `--skip_tasks` does not prevent startup database
  writes. A wrong Discord token/application can reset and reapply ban state
  before the bot notices the ID mismatch and closes.
- **Why tests do not catch it:** `tests/test_runtime_config.py:479` checks only
  that `run_tasks` becomes false.
- **Smallest bounded correction:** authenticate and validate the bot identity
  before ban reconciliation, then perform reconciliation through a bounded
  connection-owning startup service.
- **Focused regression:** a wrong authenticated bot ID must cause zero ban
  writes or background effects; the expected ID runs reconciliation exactly
  once.
- **Resolution:** ordinary startup now reaches the authenticated client user in
  `setup_hook()` before importing models or utilities. It validates that ID
  first, then runs one bounded immutable ban replacement on a worker-owned
  connection and atomic transaction before enabling any other startup effect.
  Focused ordering/rollback/cancellation coverage and the real-schema gate
  prove the corrected boundary.

## Medium

### M1 — Retained prefix adapters still create event-loop ORM boundaries

Status: **Resolved by P9.17.**

- **Location:** `PolyGame` at `modules/games.py:115`; canonical-name reads at
  `modules/games.py:2841` and `modules/games.py:2902`.
- **Observable risk:** mutation converters synchronously connect/load a live
  `Game` before the authoritative worker repeats the lookup. `$getname` and
  `$getnames` perform synchronous/lazy ORM reads and retain models across
  multiple Discord sends.
- **Why tests do not catch it:** `tests/test_player_registration.py:819` mocks
  the single `getname` ORM read; command tests commonly inject
  `SimpleNamespace` games instead of exercising conversion.
- **Smallest bounded correction:** parse mutation IDs as integers and let
  workers own lookup; add bounded immutable readers for canonical names/draft
  order.
- **Focused regression:** slow converter/name readers with an event-loop
  heartbeat; require primitive-only renderer inputs and zero ORM calls between
  sends.
- **Resolution:** `PolyGame` is now a syntax-only bounded integer converter;
  the existing mutation workers perform the authoritative game lookup and
  mutable-state validation. Retained canonical-name and draft-order commands
  use a dedicated bounded reader with worker-owned connections and frozen
  primitive snapshots. Cancellation drains the worker before release, and
  focused heartbeat/model-free/parity tests cover the retained commands.

### M2 — Backup execution can outlive both its view and Discord interaction token

Status: **Resolved by P9.18.**

P9.18 stops the preview view as soon as confirmation owns the panel, keeps the
busy state immune to its former five-minute timeout, and caps the child at 12
minutes so termination and terminal publication retain a three-minute margin
inside the fresh component interaction's 15-minute token. Progress and the
single terminal result replace the private original panel; no second terminal
followup is sent. Focused tests cover synthetic five-minute timeout, the
12/15-minute margin, success, timeout, failure, and cancellation.

- **Location:** 30-minute process limit at `modules/operator_backup.py:29`;
  five-minute view at `modules/operator_backup_views.py:25`; timeout wording at
  `modules/operator_backup_views.py:137`; final webhook work at
  `modules/operator_backup_views.py:122`; locked library's 15-minute interaction
  expiry at
  `.venv/lib/python3.12/site-packages/discord/interactions.py:423`.
- **Observable risk:** after five minutes the UI can say "expired; run again"
  while the backup remains active. After 15 minutes the swallowed message edit
  and final followup cannot reliably deliver the terminal result.
- **Why tests do not catch it:** `tests/test_operator_backup.py:135` uses
  immediate or millisecond processes and never advances view/token expiry.
- **Smallest bounded correction:** prevent timeout while busy and either bound
  execution below token lifetime with margin or deliver the terminal result
  through an approved durable private destination.
- **Focused regression:** fake both five- and fifteen-minute expiry; assert no
  false rerun guidance and exactly one terminal result through the approved
  route.

### M3 — Backup source validation is not tied to a reviewed release

Status: **Resolved by P9.18; production activation remains separately gated.**

P9.18 requires a private mode-0600 manifest bound to the exact clean checkout
checkpoint and SHA-256 digests of the tracked/deployed backup shell, tracked
reporting exporter, and resolved interpreter. Preflight uses `lstat`, rejects
non-regular/symlink source or deployed scripts and unsafe ownership/modes,
requires both tracked sources in the clean Git index, and proves the exporter
will use the running bot's interpreter before spawn. A separate production-only
plan/apply/validate tool prepares the atomic manifest during an approved
cutover; no manifest was installed in this development unit. Focused tests
cover missing/unpinned provenance, both script symlinks, dirty exporter, wrong
checkpoint, wrong interpreter, and the valid pinned path.

- **Location:** `modules/operator_backup.py:164`; additional executed exporter
  at `scripts/backup_db.sh:19` and `scripts/backup_db.sh:147`.
- **Observable risk:** `Path.stat()` follows symlinks, source ownership/type is
  not independently trusted, and preflight accepts any two identical shell
  scripts. It also executes the checkout's unvalidated reporting
  exporter/interpreter. Matching locally modified files therefore satisfy the
  "reviewed source" claim.
- **Why tests do not catch it:** `tests/test_operator_backup.py:96` creates
  arbitrary matching temporary scripts and considers them trusted; there is no
  symlink, dirty exporter, or pinned-release case.
- **Smallest bounded correction:** reject symlinks with `lstat` and bind the
  shell plus invoked exporter/runtime identity to a reviewed release manifest
  or pinned digests.
- **Focused regression:** matching-but-unpinned scripts, either symlink,
  modified exporter, and wrong checkout checkpoint must fail before spawn.

### M4 — Two public read fallbacks inherit private deferred visibility

Status: **Resolved by P9.17.**

- **Location:** private defer and fallback at `modules/games.py:1010` and
  `modules/games.py:1063`; team-show fallback at `modules/team_show.py:452`;
  prior live evidence at `docs/DATABASE_AND_SLASH_MODERNIZATION.md:18874`.
- **Observable risk:** when `interaction.channel` is unavailable,
  `followup.send(ephemeral=False)` after an ephemeral defer remains private.
  The branch can claim successful public publication although only the
  requester sees it.
- **Why tests do not catch it:** `tests/test_team_show.py:664` and
  `tests/test_team_leaderboard.py:677` always provide a working channel sender.
- **Smallest bounded correction:** resolve/fetch the destination by
  `channel_id` and publish through the channel, otherwise return an explicit
  private publication failure.
- **Focused regression:** use the inheritance-aware fake with no
  `interaction.channel`; assert resolved channel publication or a private
  failure, never a purported public webhook followup.
- **Resolution:** both publishers now require a real channel sender. They use
  the resolved interaction channel first, then the exact `channel_id` through
  the client cache/fetch path. If no sender is available, they retain the
  private acknowledgement and explicitly report that public publication did
  not occur; neither path uses an inherited webhook as a public fallback.

### M5 — Unexpected prefix exceptions leak raw text publicly

Status: **Resolved by P9.17.**

- **Location:** `bot.py:270`.
- **Observable risk:** exception text may contain database details, host paths,
  identifiers, or user-provided values, and is interpolated directly into a
  Discord message.
- **Why tests do not catch it:** there is no observable test for the prefix-wide
  unexpected-error handler.
- **Smallest bounded correction:** retain the full traceback only in server
  logs, add a correlation token, and send a generic public message.
- **Focused regression:** inject an exception containing a secret sentinel; it
  may appear in captured logs but never in Discord output.
- **Resolution:** unexpected retained-prefix failures receive an eight-hex
  correlation reference. The full unwrapped exception and traceback remain in
  server logs, while Discord receives only a generic message and that
  reference, with no raw exception text or automatic user mentions.

### M6 — The production support/privacy fallback is still unspecified

Status: **Resolved by P9.20.**

- **Location:** `docs/DATABASE_AND_SLASH_MODERNIZATION.md:11709`,
  `docs/PRIVACY_READINESS_CHECKLIST.md:14`, and `PRIVACY.md:127`.
- **Observable risk:** production omits the development-only `/staffhelp` flow,
  but the repository records no exact community route, ownership, monitoring,
  or verification evidence for the promised fallback.
- **Why tests do not catch it:** capability tests correctly omit the command but
  no readiness check requires a concrete alternative.
- **Smallest bounded correction:** record and verify the exact support/privacy
  route for each production community, or retain an existing intake until that
  evidence exists.
- **Focused regression:** release-readiness validation must fail if
  `tools_support` is unassigned or any production guild lacks a valid
  `staff_help_channel` and first helper role.
- **Resolution:** one public `/staffhelp` form now selects exactly one backend
  from the explicit runtime profile. Production makes one direct Discord relay
  to the invoking guild's configured staff-help channel, permits a ping only
  for its first configured helper role, writes no JSONL record, and reports
  success only after the send completes. Development retains its durable JSONL
  record-first flow and best-effort fixed beta mirror. The configured helper
  role is the production recipient; no separate Nelluk-owned inbox, owner, or
  polling cadence exists. Production preflight and submit-time resolution fail
  closed when the route is incomplete.

### M7 — No final-HEAD release-candidate evidence exists

Status: **Open by design and owned by R-002 after all preceding corrections.**

- **Location:** required gates at
  `docs/DATABASE_AND_SLASH_MODERNIZATION.md:11699`, audit R-002 at
  `docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md:80`, and audit sequence at
  `docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md:204`.
- **Observable risk:** evidence is per-unit and historical; nothing binds
  complete offline, stopped-writer development PostgreSQL, and bounded beta
  validation to exact candidate `8cb47aa`.
- **Why tests do not catch it:** no readiness manifest or validator compares
  recorded evidence with HEAD.
- **Smallest bounded correction:** after corrections, freeze one candidate and
  record exact-SHA evidence for every required gate.
- **Focused regression:** a readiness validator must reject evidence referring
  to any other checkpoint or omitting a required gate.

## Low / documentation

### L1 — Roadmap and evidence records are internally stale

Status: **Resolved by P9.19.**

- **Original records at review checkpoint `8cb47aa`:**
  - "Last updated" remains 2026-08-09 at
    `docs/DATABASE_AND_SLASH_MODERNIZATION.md:3`, despite later 2026-08-10 work.
  - P9 is `Planned` at `docs/DATABASE_AND_SLASH_MODERNIZATION.md:949`, but
    `In progress; P9.0–P9.6 source units complete` at line 11691.
  - C-012/C-013 still carry resolved stall language at
    `docs/DATABASE_AND_SLASH_MODERNIZATION.md:365`; C-025 remains
    "implementation pending" at line 379.
  - P9.6's next action jumps toward later production execution at
    `docs/DATABASE_AND_SLASH_MODERNIZATION.md:12603`, bypassing unresolved
    R-003/R-004.
  - Taxonomy "current implementation" omits many current roots at
    `docs/SLASH_COMMAND_TAXONOMY_REVIEW.md:1331`.
  - The readiness audit says ten roots at
    `docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md:54`; current source loads
    eleven with `/operator`.
  - `config.ini-EXAMPLE` still names `polytopia` at line 8, while reviewed
    production identity is `polytopia2`.
  - `git diff --check` fails on trailing whitespace at
    `modules/league.py:1400`, contradicting recorded clean-diff evidence.
- **Observable risk:** reviewers and operators cannot identify the true active
  phase, command surface, or next gate reliably.
- **Why tests do not catch it:** no consistency check links summary tables,
  capability/source inventory, compatibility decisions, and current HEAD.
- **Smallest bounded correction:** reconcile the summary, ledgers, taxonomy,
  audit source count, next-action pointer, example configuration, and
  whitespace claim.
- **Focused regression:** one model-free command/compatibility/status
  consistency check plus `git diff --check`.
- **Resolution:** the already-correct date, phase table, example database, and
  whitespace state remain intact. P9.19 reconciles C-012/C-013/C-025, replaces
  the taxonomy's stale current inventory with the exact eleven source roots,
  distinguishes the readiness audit's ten-root historical snapshot from its
  current eleven-root state, corrects P9.6's chronological next action without
  deleting its production carry-forward gate, and updates the active review/
  rollout pointers. A fresh-process model-free loader regression ties those
  current records to source and the selected compatibility/status facts.

## Areas reviewed with no defect found

- HEAD is exactly `8cb47aa`; the worktree was clean at the start of review.
- `origin/master` is `c35e2f1` and is an ancestor of HEAD. The branch is zero
  commits behind and 592 ahead; no current-master divergence remains.
- `pyproject.toml` and `uv.lock` do not differ from `origin/master`.
- Normal startup does not synchronize application commands. Every sync call in
  the manager supplies an explicit guild.
- Capability policy is default-deny and current model-free loading produced
  eleven guild-only roots.
- ELO coordinator completion/cancellation accounting retains ownership until
  its worker future completes.
- Operator player migration/deletion and Tribe mutation workers use frozen
  requests/results, worker-local connections, atomic mutation/audit
  transactions, locks, and authoritative revalidation.
- Modernized incomplete-game purge, started-broadcast reconciliation, and
  sampled pending-game component flows use immutable discovery/state and
  reject stale, duplicate, expired, or requester-mismatched actions.
- Sampled component cleanup treats Discord error `10008` as benign; no separate
  Unknown Message defect was found.
- Apart from the findings above, backup handling correctly refuses
  non-owner/development identities before production-path reads, uses
  asynchronous subprocesses, rejects in-process and host-lock conflicts,
  bounds output, distinguishes partial/busy failures, validates artifact
  freshness, and returns private bounded summaries.
- Feedback attachments are development-only, count/size/type bounded, stored
  under restricted paths, and mirrored with mentions disabled.
- Tracked production/development systemd units explicitly select their
  profiles.

## Validation evidence

The safe offline suite ran with database integration forcibly disabled:

- 1,454 tests executed;
- 61 intentionally skipped;
- one failure and two errors, all caused by the existing `.venv` lacking the
  locked `duckdb` package, including the reporting-export module import;
- dependencies were not installed or synchronized; and
- no gated PostgreSQL tests ran.

That prevents claiming a green final-HEAD offline checkpoint. It does not by
itself establish a repository dependency defect because the lockfile includes
DuckDB and the current environment is unsynchronized.

## Prioritized correction sequence

1. Correct confirmation transaction/publication semantics.
2. Remove live ORM graphs from post-commit publishers and modernize the enabled
   background tasks.
3. Restore native ban parity and authoritative worker checks; fail closed on
   unset profiles and nonempty global command state.
4. Remove approved obsolete prefixes, resolve destructive channel purge, and
   move identity validation before startup writes.
5. Harden backup cancellation, interaction lifetime, and release provenance.
6. Implement the production-only additive migration.
7. Write the modernization cutover/rollback runbook and record the concrete
   production support route.
8. Reconcile roadmap/taxonomy/status records, freeze a new exact candidate, and
   complete its required validation evidence.

The branch should remain a modernization work branch until those corrections
and gates are closed.
