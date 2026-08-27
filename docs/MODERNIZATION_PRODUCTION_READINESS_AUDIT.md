# Modernization production-readiness audit

Date: 2026-08-09

Status: Read-only audit complete, integrated, and pushed (audit `5f69726`,
integration evidence `9b49a83`); production rollout is not yet ready

This audit reconciles the accumulated database/slash modernization work with
the current production baseline. It authorizes no production checkout,
database, service, Discord, or schema action.

## Executive result

The modernization architecture is suitable for a production canary, but the
current accumulation branch is not yet a releasable production checkpoint.
P9.17–P9.20 resolve M1–M6 and L1. Two bounded preparation stages remain:

1. validate one reconciled release candidate offline and through the stopped-
   writer development PostgreSQL gate; and
2. after separate approval, deploy one production process, expose the reviewed
   all-guild `/staffhelp` relay, and enable the remaining guild-scoped native-
   command canary only in PolyChampions.

The dependency/PostgreSQL upgrade completed before this project is not an
outstanding modernization dependency. Its historical rollback instructions
are also not a valid rollback for this release.

## Verified repository state at the audit base

- Audit base: `3947298` on `codex/database-slash-modernization`.
- `origin/master` is seven commits ahead of the audit base. The accumulation
  branch is 559 commits ahead of the common base and its full branch diff is
  277 files, 146,909 insertions, and 5,197 deletions.
- The seven upstream commits are the completed PostgreSQL/dependency cleanup
  series through `33db386`. They add the canonical production systemd unit,
  database setup/cleanup documentation, and image-validation logging.
- A read-only merge-tree preview found textual conflicts in
  `modules/image_storage.py` and `tests/test_image_storage.py`. `README.md` is
  changed on both sides but previewed as an automatic merge. The upstream
  image logging must be ported into the modernization branch's byte-oriented,
  staged/atomic image pipeline rather than accepting either side wholesale.
- `pyproject.toml` and `uv.lock` have no difference between current
  `origin/master` and the accumulation branch. CPython 3.12 and the locked
  dependency environment are already the common baseline.
- The only added model columns are
  `DiscordMember.timezone_offset_minutes SMALLINT NULL` and
  `DiscordMember.timezone_offset_cleared BOOLEAN NOT NULL DEFAULT FALSE`.
  The P6.2 development tool continues to refuse production. P9.15 adds a
  separate production-only plan/apply/verify tool; it has been validated
  offline and against the already-migrated development schema, but has not
  connected to production or run production DDL.
- At the audit base, the model-free command source loaded ten roots: `elo`, `game`, `house`,
  `leaderboard`, `league`, `player`, `squad`, `staffhelp`, `team`, and the
  development-only `whattotest`.
- The explicit command manager is default-plan, guild-only, has no global-sync
  path, and requires exact environment/guild/scope/no-global confirmations
  before apply. Normal bot startup performs no command synchronization.
- P4 through P8 are technically complete. WB1 remains open because the broad
  tester checklist deliberately contains many workflows without wide human
  acceptance. That checklist is useful feedback inventory, but it is not yet
  a bounded release-candidate acceptance gate.

## Current reconciliation

P9.19 reconciles active readiness guidance without replacing the audit's
historical evidence, and P9.20 resolves M6. After the temporary Beta Lab
retirement, the current model-free source roots (11) are: `elo`,
`game`, `guild`, `house`, `leaderboard`, `league`, `operator`, `player`,
`squad`, `staffhelp`, and `team`. P9.2 added `/operator` after
the audit; P10.9 adds narrow same-guild `/guild edit` under the same
default-deny capability without exposing the administrator-default operator
surface to delegated managers. The initial production canary policy still
omits that capability. R-001, R-003, and R-004
are complete; M1–M6 and L1 are resolved. M7/R-002 remains the final gate to
freeze and validate one exact release candidate.

## Canary blockers

### R-001 — Reconcile current `master`

Status: Complete, integrated, and pushed through P9.1 evidence checkpoint
`057b0a5` (merge checkpoint `8ede97b`).

Merge `origin/master` into `codex/database-slash-modernization` in an isolated
unit before any final PR or deployment. Preserve the upstream production
service/database documentation. Resolve image-storage conflicts by retaining
the modernization staging, immutable-byte, size, and atomic-publication
boundaries while adding upstream diagnostic logging and its more informative
dimension error. Run all image/team/house presentation tests and complete
offline discovery.

### R-002 — Build one release candidate

Status: In progress through P9.26. The P9.24 source candidate
`acf706fed8d51a061383e7caba2a4c210ec61981` and RC1 record are historical:
they predate N3–N7 and the post-P11 accumulation source. P9.26 freezes exact
candidate `8e79dc295c024340fd55f9678d507e6e214469b4`, requires every N3–N7
resolution checkpoint, and passes candidate-bound cutover review, 2,110-case
offline discovery, and 79-case stopped-writer development PostgreSQL discovery.
RC2 remains correctly not ready: three bounded-beta items and all three
separately approved redacted production-configuration checks are pending.
P9.27 then found and corrected a container-only Beta Lab control omission,
making RC2 historical for the current accumulation tip. P9.28 repaired the
exposed readiness state under the stopped-writer gate: all five protected
packs are ready at exact source `da7b204`, complete discovery passes 2,116
tests with 98 skips, and the global/12-root guild command tree is unchanged.
The remaining human command, retained-prefix, and visibility matrix must be
resolved before freezing the successor candidate.

After upstream reconciliation, freeze one clean commit and review the cutover-
critical delta. Preserve the per-unit history; do not squash away its durable
evidence merely to make the large final branch look smaller. The review should
use phase/commit inventory plus focused seam review rather than claim that a
single superficial pass over 277 changed files is sufficient.

### R-003 — Add the production timezone migration

Status: Complete through P9.15 implementation checkpoint `1c8ffa5`, evidence
checkpoint `c7333aa`, and accumulation merge `8b9ede1`; production use remains
separately gated.

Create a separate production-operations unit. It must:

- default to a connection-free plan;
- verify `POLYBOT_ENV=production`, the configured database/role, and the live
  PostgreSQL `current_database()` / `current_user` identity;
- require an exact production acknowledgement;
- inspect table/column/type/nullability/default state before DDL;
- add only the two reviewed columns in one transaction and be idempotent;
- refuse destructive rollback during the rollout; and
- include a read-only verify mode and offline fault-injection tests.

The normal rollback is code/config rollback while leaving these harmless
additive columns in place. Dropping populated columns is not an emergency
rollback.

P9.15 implements the required connection-free plan, fixed `polytopia2`
database target, configured/live role checks, exact acknowledgement,
schema-qualified metadata inspection and additive DDL, single-transaction
idempotency, five-second local lock timeout, post-DDL verification, read-only
verify mode, and no destructive rollback command. The complete stopped-writer
development suite passed 69 tests with one preserved-fixture skip; its B1 case
first proved the columns already existed, so the apply path executed no DDL.

### R-003B — Add the production player-badges migration

Status: Implemented and offline-validated at `0ccb002`; production use remains
separately gated.

P12.1 adds one further backward-compatible column,
`public.player.badges`. The production path is separate from the unchanged
development-only tool and provides a connection-free plan, exact
`production` / `polytopia2` configured/live identity checks, read-only verify,
an explicit production acknowledgement, a five-second DDL lock timeout,
single-transaction apply and exact post-verification, idempotency, and no
destructive rollback. It performs no badge population, player-row
transformation, or identity backfill. The focused migration/release/runbook
suite passed 49 tests and complete compliant offline discovery passed 2,239
tests with 92 intentional gated skips. Production verification and apply remain
operations in the approved maintenance sequence, not standing authorization.

### R-004 — Replace the historical cutover procedure

Status: Complete through P9.16 implementation checkpoint `d754beb`, evidence
checkpoint `8ddad6a`, and accumulation merge `d4a37d0`; production execution
remains separately gated.

`docs/PRODUCTION_CUTOVER.md` accurately records the completed Python 3.12 and
PostgreSQL dependency cutover. Its Python 3.9 rollback is explicitly retired.
A new modernization runbook must cover:

- exact Git release and rollback checkpoints;
- the canonical tracked Python 3.12 systemd unit and locked environment;
- production backup verification;
- stop and single-writer proof before DDL;
- additive schema apply/verify before new model code starts;
- a task-disabled process canary, clean stop, and normal service start;
- redacted runtime identity and log checks;
- command-tree plan/inspect/apply as a separate step; and
- independent code, schema, and command-tree rollback dispositions.

No tester announcement should precede an extended maintenance window. Publish
only after the service and command tree are healthy and the requested tester
actions are actually available.

P9.16 adds `docs/MODERNIZATION_PRODUCTION_CUTOVER.md` as the explicit
modernization authority and labels `docs/PRODUCTION_CUTOVER.md` historical
only. The new procedure pins full release/rollback commits, the locked Python
3.12 environment and canonical service, exact backup provenance/validation,
host/process/PostgreSQL single-writer proof, schema-before-model apply/verify,
a tracked task-disabled `Restart=no` canary, clean canonical activation,
separate global-read/guild-only command inspection and apply, terminal
announcement ordering, and independent code/config, additive-schema, command-
tree, database-restore, and uncertain-effect dispositions. It also refuses a
release record with unresolved adversarial findings or no exact production
support/privacy route.

### R-005 — Configure and prove the production command canary

The ignored production server settings must remain default-deny until reviewed.
The recommended first PolyChampions assignment is:

```python
('core_user', 'team', 'league', 'house', 'squad')
```

Assign `tools_support` through the all-allowed-guild capability setting after
every configured production guild's `staff_help_channel` and first
`helper_roles` entry has been reviewed. That exposes `/staffhelp` in every
approved production guild, while the tuple above exposes `game`, `leaderboard`,
`player`, `team`, `league`, `house`, and `squad` only in PolyChampions.
Initially omit:

- `elo_maintenance`: owner/staff maintenance is unnecessary for the user
  canary and can be enabled later;
- `beta_testing`: retained temporarily as a no-root compatibility assignment;
  and
- `operator`: omit it from the initial user canary. P9.2 supersedes the audit's
  earlier operator-only assumption by reserving a separately reviewed,
  Administrator-default `/operator` root with authoritative configured-ID
  checks and explicit all-allowed-guild deployment policy.

Before apply, produce a redacted production runtime check, offline desired
plan, remote guild-only inspection, and exact diff. Apply requires separate
approval and the existing explicit no-global-sync confirmation. Immediately
re-inspect for convergence.

## Items that do not block the first canary

- Production identity backfill is not required. Keep `name_steam`,
  `polytopia_id`, and the old whole-hour timezone field intact. The separately
  approved aggregate-only production identity inventory is required before a
  future backfill or legacy-field retirement, not before additive code/schema
  deployment.
- P9.20 gives the one `/staffhelp` command explicit environment backends.
  Production performs one direct relay to the invoking guild's configured
  staff-help channel, pings only its first configured helper role, and writes
  no development JSONL record. Successful Discord delivery is terminal; there
  is no Nelluk-owned inbox or polling cadence.
- `elo_maintenance` may remain unregistered during the initial canary.
- Bullet, anti-scam, and the legacy API remain outside modernization. Do not
  assign a new native capability for them.
- The broad development `WHAT TO TEST` list does not have to be exhaustively
  signed off. Before release, derive a bounded canary matrix covering startup,
  retained-prefix parity, one representative read and write per assigned root,
  permissions, public/private visibility, and rollback of the command tree.
- The initial user canary omits the separately reviewed `operator` capability.
  P9.2 resolves its authorization/registration infrastructure; each actual
  operator workflow still requires its own bounded implementation and explicit
  capability deployment before retiring the corresponding prefix command.
- Discord permissions apply at the top-level root, not to individual
  subcommands. Ordinary PolyChampions members may therefore see staff-only
  entries inside the assigned `game`, `team`, or `league` roots and receive an
  authoritative private denial when invoking them. This is a known canary UI
  limitation, not an authorization gap. Hiding those entries later requires a
  separately accepted `/admin`-style root/taxonomy change.

## Operator carry-forward recommendation

Handle the operator paths as a separate pre-rollout cleanup after R-001:

- P9.6/P9.8 retire `$backup_db`/`$dbb`, `$gtest`, `$ptrophies`, and both
  `$boost_from` forms under their accepted replacement/retirement decisions;
- prefer guarded systemd operations over `$restart`, `$restart_force`, and
  `$quit`, subject to one explicit owner workflow decision;
- keep the P9.3 owner-only `/operator tribe emoji` replacement and retired
  `$tribe_emoji` prefix as the accepted Tribe disposition;
- preserve account migration/deletion capability only as reviewed,
  confirmation-heavy offline/worker operations; and
- P9.9 resolves `$purge_game_channels`: the interleaved prefix implementation
  is retired and replaced by owner-only `/operator channels purge`, with a
  private bounded preview, exact selection/typed confirmation, worker-owned
  database access, per-target reauthorization, and explicit post-deletion
  reconciliation.

These decisions should be evidence-backed but do not require a native command
for every retained operation.

## Recommended rollout sequence

1. R-001 upstream reconciliation.
2. Operator cleanup decisions that affect the final code checkpoint.
3. R-003 production migration implementation and offline review.
4. R-004 modernization cutover/rollback runbook and production config plan
   (complete through P9.16 merge `d4a37d0`).
5. M6 is complete through P9.20: verify every production guild's configured
   staff-help channel and first helper role in the release record.
6. R-002 release-candidate review, complete offline tests, stopped-beta full
   development PostgreSQL gate, and a bounded beta release matrix.
7. Push/open the final accumulation-to-`master` integration review; merge only
   after explicit approval.
8. In one separately approved maintenance window: verify backup, stop the
   production writer, prove no second writer, apply/verify additive schema,
   deploy the exact code/config/locked environment, run a task-disabled
   process canary, then start the canonical production service.
9. Observe retained prefix behavior before changing the Discord command tree.
10. Plan, inspect, and explicitly apply the reviewed all-guild `tools_support`
   assignment plus the PolyChampions-only capability set; re-inspect
   convergence and publish the targeted canary announcement.
11. If the native surface is unhealthy, clear the canary capability assignment
    and explicitly apply the resulting removal plan while leaving retained
    prefixes and additive columns available.

Production pull/merge, backup, schema DDL, service lifecycle, remote Discord
inspection/apply, and announcements each retain their separate approval
boundaries.

## Audit validation

- Read-only merge-tree preview identified the exact overlapping files.
- Model diff proved exactly two added columns.
- Dependency diff proved no `pyproject.toml` or `uv.lock` divergence.
- The audit-time model-free command load reported ten roots and no database
  connection. The later reviewed P9.2 operator infrastructure adds the
  eleventh current root, `/operator`; initial production canary policy still
  omits that capability.
- Focused readiness suite passed **114 tests** across command policy/management,
  runtime configuration, deployment assets, dependency compatibility,
  timezone migration safety, beta operations, and beta readiness.
- Complete offline discovery passed **1,392 tests with 58 intentional
  database-gated skips**.
- No production checkout, database, service, Discord, beta, schema, fixture,
  dependency, or sudo action occurred.

## Next action

Complete P9.26/M7-R-002 against the exact refreshed candidate. Its RC2 record
must verify N3–N7 as well as the prior findings and retain each production
guild's reviewed `staff_help_channel` and first helper role. Production
configuration access and execution remain separately approval-gated.
