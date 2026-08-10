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
Five bounded preparation units remain:

1. merge the current `origin/master` into the accumulation branch and resolve
   the known image-storage conflicts;
2. provide a production-only, additive migration for the two canonical
   timezone columns;
3. write and review a modernization-specific cutover and rollback runbook;
4. validate one reconciled release candidate offline and through the stopped-
   writer development PostgreSQL gate; and
5. after separate approval, deploy one production process and enable a
   guild-scoped native-command canary only in PolyChampions.

The dependency/PostgreSQL upgrade completed before this project is not an
outstanding modernization dependency. Its historical rollback instructions
are also not a valid rollback for this release.

## Verified repository state

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
  The checked-in P6.2 migration tool deliberately refuses production, so a
  production migration does not yet exist.
- The model-free command source loads ten roots: `elo`, `game`, `house`,
  `leaderboard`, `league`, `player`, `squad`, `staffhelp`, `team`, and the
  development-only `whattotest`.
- The explicit command manager is default-plan, guild-only, has no global-sync
  path, and requires exact environment/guild/scope/no-global confirmations
  before apply. Normal bot startup performs no command synchronization.
- P4 through P8 are technically complete. WB1 remains open because the broad
  tester checklist deliberately contains many workflows without wide human
  acceptance. That checklist is useful feedback inventory, but it is not yet
  a bounded release-candidate acceptance gate.

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

After upstream reconciliation, freeze one clean commit and review the cutover-
critical delta. Preserve the per-unit history; do not squash away its durable
evidence merely to make the large final branch look smaller. The review should
use phase/commit inventory plus focused seam review rather than claim that a
single superficial pass over 277 changed files is sufficient.

### R-003 — Add the production timezone migration

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

### R-004 — Replace the historical cutover procedure

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

### R-005 — Configure and prove the production command canary

The ignored production server settings must remain default-deny until reviewed.
The recommended first PolyChampions assignment is:

```python
('core_user', 'team', 'league', 'house', 'squad')
```

That exposes `game`, `leaderboard`, `player`, `team`, `league`, `house`, and
`squad` only in the approved guild. Initially omit:

- `elo_maintenance`: owner/staff maintenance is unnecessary for the user
  canary and can be enabled later;
- `tools_support`: `/staffhelp` still has a development-only authoritative
  store;
- `beta_testing`: `/whattotest` is development-only; and
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
- `/staffhelp` does not block other command roots when `tools_support` is
  omitted. Production users retain the currently deployed human support route
  until a production-safe intake/retention design is approved.
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
4. R-004 modernization cutover/rollback runbook and production config plan.
5. R-002 release-candidate review, complete offline tests, stopped-beta full
   development PostgreSQL gate, and a bounded beta release matrix.
6. Push/open the final accumulation-to-`master` integration review; merge only
   after explicit approval.
7. In one separately approved maintenance window: verify backup, stop the
   production writer, prove no second writer, apply/verify additive schema,
   deploy the exact code/config/locked environment, run a task-disabled
   process canary, then start the canonical production service.
8. Observe retained prefix behavior before changing the Discord command tree.
9. Plan, inspect, and explicitly apply the PolyChampions-only capability set;
   re-inspect convergence and publish the targeted canary announcement.
10. If the native surface is unhealthy, clear the canary capability assignment
    and explicitly apply the resulting removal plan while leaving retained
    prefixes and additive columns available.

Production pull/merge, backup, schema DDL, service lifecycle, remote Discord
inspection/apply, and announcements each retain their separate approval
boundaries.

## Audit validation

- Read-only merge-tree preview identified the exact overlapping files.
- Model diff proved exactly two added columns.
- Dependency diff proved no `pyproject.toml` or `uv.lock` divergence.
- Model-free command load reported ten roots and no database connection.
- Focused readiness suite passed **114 tests** across command policy/management,
  runtime configuration, deployment assets, dependency compatibility,
  timezone migration safety, beta operations, and beta readiness.
- Complete offline discovery passed **1,392 tests with 58 intentional
  database-gated skips**.
- No production checkout, database, service, Discord, beta, schema, fixture,
  dependency, or sudo action occurred.

## Next action

Make the operator carry-forward decisions that affect the release candidate,
then start the separate production timezone migration. Do not combine operator
cleanup with production DDL.
