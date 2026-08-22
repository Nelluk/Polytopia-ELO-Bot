# Database/slash modernization production cutover and rollback

Status: reviewed procedure only; not standing production authorization

This is the production operations authority for the database/slash
modernization. `docs/PRODUCTION_CUTOVER.md` is a historical record of the
completed Python 3.12 dependency upgrade and must not govern this release.

Every production read, backup, checkout update, service action, database
verify/apply, Discord inspection/apply, and announcement remains separately
approval-gated. Repository review alone authorizes none of them. Stop on any
unexpected identity, checkpoint, dirty tree, writer, schema, command tree,
backup, test, or health result. Never improvise around a failed gate.

## Fixed boundaries

- Checkout: `/srv/polyelo/PolyBot39`
- Service: `polyelo.service`, running as service account `polyelo`
- Canonical unit source: `deploy/systemd/polyelo.service`
- Reviewed backup wrapper source: `deploy/polyelo-backup`; installed executable
  `/srv/polyelo/bin/polyelo-backup`, owned by root
- Backup state and artifact root: `/srv/polyelo/backups`
- Canary drop-in source: `deploy/systemd/polyelo-modernization-canary.conf`
- Locked runtime: `.venv/bin/python`, CPython 3.12, `uv.lock`, production
  dependencies only
- Environment: exact `POLYBOT_ENV=production`
- Database: `polytopia2`; role from the reviewed production profile and live
  `current_user`
- Discord application: `484067640302764042`
- Native synchronization targets: Main `283436219780825088` and
  PolyChampions `447883341463814144` only
- Main capabilities: `tools_support`, exposing only `/staffhelp`
- PolyChampions capabilities: `core_user`, `team`, `league`, `house`, `squad`,
  `tools_support`, and the subsequently owner-enabled `operator`
- Every other live allowlisted guild receives no native capability assignment
  and is not inspected or synchronized during this cutover
- Product bug/improvement feedback route: beta guild `478571892832206869`,
  channel `480078679930830849`, with source server/channel metadata and no
  role mention
- Initially omitted capabilities: `operator`, `elo_maintenance`, and
  `beta_testing`; `operator` was subsequently enabled only for PolyChampions
  through the owner-approved static-production bootstrap
- HTTP API: disabled and inactive
- Legacy prefixes: retained wherever the compatibility ledger says `retain`
- Global application-command tree: must be empty; this repository has no
  authority or tooling to mutate it

The reviewed production migration tooling covers two additive timezone columns
and the additive `public.player.badges` column. Apply and verify every
release-required backward-compatible expansion before switching application
code in the same maintenance window. The old whole-hour field and identity
fields remain untouched; there is no data migration or backfill, destructive
schema rollback, or separate soak period for columns the rollback code does
not read.

## Required approval and release record

Before the maintenance window, prepare one reviewed, non-secret release record
that contains all of the following exact values and evidence:

- 40-character release commit and exact immediate pre-cutover rollback commit;
- proof both commits are reviewed ancestors of the approved production branch;
- `uv.lock` digest and the canonical systemd-unit digest;
- expected redacted runtime identity, configured database role, allowlisted
  guild IDs, and disabled API state;
- the exact ignored production capability assignment, with no all-guild
  capabilities, `/staffhelp` only in Main, and the approved PolyChampions
  canary roots plus `/staffhelp`;
- proof Main and PolyChampions have their exact reviewed `staff_help_channel`
  and first `helper_roles` entry;
- proof the bot-level `polyelo_feedback_route` resolves to the reviewed private
  maintainer channel in an allowlisted guild, with no role mention;
- fresh R-002 evidence bound to the release commit: complete offline suite,
  stopped-writer development PostgreSQL suite, cutover-critical review, and
  bounded beta matrix;
- reviewed rollback dispositions for code/configuration, additive schema, and
  the PolyChampions command tree; and
- explicit approvals for the production maintenance window, backup, checkout
  update, database verify/apply, service lifecycle, Discord inspect/apply, and
  announcement as distinct operations.

Literal placeholders, abbreviated commits, evidence from another HEAD,
unresolved adversarial-review items, or a missing support-route record are stop
conditions. The release record is evidence, not executable configuration and
must contain no token, password, cookie, private key, or database DSN.

At execution time, copy the two approved full commits into shell variables and
validate them before using them:

```bash
export POLYBOT_RELEASE_SHA=REPLACE_WITH_APPROVED_40_CHARACTER_RELEASE_COMMIT
export POLYBOT_ROLLBACK_SHA=REPLACE_WITH_APPROVED_40_CHARACTER_ROLLBACK_COMMIT
test "${#POLYBOT_RELEASE_SHA}" -eq 40
test "${#POLYBOT_ROLLBACK_SHA}" -eq 40
test "$POLYBOT_RELEASE_SHA" != "$POLYBOT_ROLLBACK_SHA"
```

Do not substitute `HEAD`, `master`, a tag, or an abbreviated hash for either
reviewed checkpoint.

## Production configuration plan

Review the ignored production `config.ini` and `server_settings.py` without
copying secrets into Git, logs, or the release record. The redacted runtime
check must prove:

- `expected_bot_id = 484067640302764042`;
- `psql_db = polytopia2`, the reviewed nonempty role, and either a nonempty
  `psql_password` or the reviewed passwordless default local Unix-socket mode
  (the redacted output must never print a password);
- `background_tasks_enabled = true`, `api_enabled = false`, and the existing
  reviewed Bullet policy;
- the existing live production guild allowlist and every retained legacy
  guild setting remain unchanged;
- Main `283436219780825088` receives exactly `('tools_support',)`;
- PolyChampions `447883341463814144` receives exactly `('core_user', 'team',
  'league', 'house', 'operator', 'squad', 'tools_support')`;
- `application_command_all_guild_capabilities` is exactly empty and no other
  allowlisted guild has a native capability assignment;
- Main and PolyChampions retain their live staff-help channels and first
  helper roles; PolyChampions channel `1327320361200648213` is canonical;
- top-level `polyelo_feedback_route` is exactly beta guild
  `478571892832206869` and channel `480078679930830849`; production feedback
  output includes source server/channel metadata and disables mentions;
- `operator` is assigned only to PolyChampions; `elo_maintenance` and
  `beta_testing` are not assigned; and
- the configured prefix, image root, and log root remain production values.

The production profile must fail before server-settings loading, directory
creation, model import, or connection when `expected_bot_id` is missing or
blank. A missing or blank `psql_password` is accepted only when `psql_host` is
also blank, selecting the reviewed host-local Unix socket with PostgreSQL peer
authentication. TCP production and every development profile still require a
nonempty password. No legacy identity or password literal is an acceptable
substitute.

The model-free startup schema preflight must carry the validated runtime
environment and apply this same authentication rule before connecting. It may
omit the password connection parameter only for the reviewed production local
socket/peer mode. A runtime profile accepted here but rejected by startup
preflight is a release blocker; exact composed regression coverage is required.

Do not enable the inactive API, change database credentials, backfill identity
data, retire another prefix, or add another guild/capability during this
cutover. Configuration changes outside the reviewed redacted diff require a
new release review.

## Pre-maintenance gates — no downtime

Complete these before notifying users of downtime:

1. Freeze and review R-002 at `POLYBOT_RELEASE_SHA`; all offline/development-
   database/beta evidence must name that exact commit.
2. Confirm every valid adversarial-review finding is either resolved by a
   named integrated checkpoint or deliberately blocks the release. B2 does not
   waive open Medium or documentation findings.
3. Review the connection-free migration plan from the release source:

   ```bash
   .venv/bin/python scripts/migrate_player_timezone_production.py
   ```

   It must print only the two schema-qualified additive statements and state
   that no runtime profile, connection, or DDL was used.

   A release containing P12.1 must also contain the reviewed production badge
   migration module, `scripts/migrate_player_badges_production.py`, and
   `docs/PLAYER_BADGES_MIGRATION.md` in its exact source inventory. Review its
   connection-free plan:

   ```bash
   .venv/bin/python scripts/migrate_player_badges_production.py
   ```

   It must print only the schema-qualified additive `public.player.badges`
   statement and state that no runtime profile, connection, or DDL was used.
   The development confirmation/tool is not production authority; do not
   substitute it or run ad-hoc DDL.
4. Run the application-command desired-state plan offline for exactly Main and
   PolyChampions:

   ```bash
   POLYBOT_ENV=production \
   .venv/bin/python scripts/manage_application_commands.py \
     --environment production \
     --mode plan \
     --guild-ids 283436219780825088,447883341463814144
   ```

   Review exact create/update/unchanged/remove roots. Main must receive only
   `/staffhelp`; PolyChampions receives `/staffhelp` plus the approved canary
   roots. No other guild is inspected or changed.
5. Verify the approved release and rollback commits, lockfile, unit, canary
   drop-in, migration tools, command manager, tracked backup program, tracked
   production wrapper, and operator-backup module are present in the reviewed
   Git history. Do not update the production checkout yet. Confirm the fixed
   installed wrapper `/srv/polyelo/bin/polyelo-backup` is a root-controlled,
   non-symlink executable before relying on either scheduled or operator-run
   backups.
6. Verify the routine backup state and obtain the separate approval for a fresh
   pre-stop backup. Validate custom-format dumps with `pg_restore --list` and
   image archives with `tar -tzf`; timestamps, sizes, and paths must match the
   release record.
7. Confirm Main and PolyChampions staff-help channels and first Helper roles
   resolve exactly. Confirm the production bot can send an embed and files to
   beta channel `480078679930830849` without mentions and that its embed names
   the originating server and channel. Do not proceed if either flow lacks its
   working destination.

Do not announce completion or invite testing at this stage.

## Maintenance sequence

The order below is mandatory: backup, stop, prove one-writer shutdown, deploy
reviewed source without starting it, apply and verify the backward-compatible
schema expansion, switch to the reviewed application through the task-disabled
canary, cleanly stop it, start the canonical service, then handle the Discord
command tree as a separate gate. Schema expansion and application switchover
belong to the same maintenance window; the two unused additive columns do not
require a separate observation window.

### 1. Capture start state and fresh backup

After the exact production-read and backup approvals, record the clean
checkout commit, service PID/state/restart count/effective command, API state,
disk space, redacted runtime identity, and latest logs. Run the reviewed backup
workflow and validate every required artifact. Stop before downtime if the
checkout is dirty, the commit is unexpected, the service is unhealthy, the API
is active, or backup validation is incomplete.

The tracked backup program must belong to the clean rollback history. The
wrapper is introduced by the approved release, so its reviewed bytes must be
read from `POLYBOT_RELEASE_SHA`, not from the rollback worktree. Fetch and
freeze the approved remote release before comparing those candidate bytes to
the installed root-owned wrapper. The installed wrapper must invoke the
tracked backup program from the canonical checkout. The reporting exporter
and Python path must also resolve within that checkout. A cutover backup
requires exit status zero; a reporting-partial or lock-busy result is not an
acceptable recovery point. The bounded command sequence is:

```bash
test "$(git rev-parse HEAD)" = "$POLYBOT_ROLLBACK_SHA"
git status --short --branch
git fetch origin master
test "$(git rev-parse origin/master)" = "$POLYBOT_RELEASE_SHA"
git merge-base --is-ancestor "$POLYBOT_ROLLBACK_SHA" "$POLYBOT_RELEASE_SHA"
git show "$POLYBOT_RELEASE_SHA:deploy/polyelo-backup" \
  | cmp --silent - /srv/polyelo/bin/polyelo-backup
/srv/polyelo/bin/polyelo-backup
/usr/bin/pg_restore --list /srv/polyelo/backups/polytopia_full_backup.sqlc >/dev/null
/usr/bin/tar -tzf "/srv/polyelo/backups/polytopia_images-$(date +%A).tar.gz" >/dev/null
```

### 2. Stop the production writer and prove it is absent

After the exact lifecycle approval:

```bash
sudo systemctl stop polyelo.service
systemctl show polyelo.service \
  -p ActiveState -p SubState -p MainPID -p NRestarts --no-pager
```

Require `inactive`, `dead`, and `MainPID=0`. Perform a host-wide process audit
for every `bot.py`, API, ad-hoc worker, backup/export, and sibling checkout.
Classify candidates by command, working directory, user, parent, and start
time. Never stop an unknown or unrelated process merely to make the audit pass.

Using the reviewed production database role, inspect `pg_stat_activity` for
all other `polytopia2` sessions, excluding only the audit session's own
`pg_backend_pid()`. Any unexplained application, backup, export, maintenance,
or idle-in-transaction session blocks DDL. Record the exact clear result.

Set `POLYBOT_DB_ROLE` only from the reviewed redacted configuration record;
never echo its password or DSN. The audit query deliberately omits query text:

```bash
export POLYBOT_DB_ROLE=REPLACE_WITH_REVIEWED_PRODUCTION_ROLE
/usr/bin/psql --no-psqlrc --set=ON_ERROR_STOP=1 \
  --dbname polytopia2 --username "$POLYBOT_DB_ROLE" \
  --command "SELECT pid, usename, application_name, client_addr, state, backend_start, xact_start, query_start FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() ORDER BY pid;"
```

Create and validate the final stopped-writer database and image recovery
artifacts. No new application process may start between this backup and schema
verification.

### 3. Move only to the exact reviewed release

Use the remote ref frozen before downtime, re-prove that the approved release
is the exact reviewed remote commit and descends from the exact live rollback
commit, then update only by the reviewed fast-forward path. The release must
already have been promoted to `origin/master`; do not merge, rebase,
force-push, refetch a moving target after shutdown, or discard a master-only
production fix during cutover. Require:

```bash
test "$(git symbolic-ref --short HEAD)" = master
git status --short --branch
test "$(git rev-parse HEAD)" = "$POLYBOT_ROLLBACK_SHA"
test "$(git rev-parse origin/master)" = "$POLYBOT_RELEASE_SHA"
git merge-base --is-ancestor "$POLYBOT_ROLLBACK_SHA" "$POLYBOT_RELEASE_SHA"
git merge --ff-only "$POLYBOT_RELEASE_SHA"
test "$(git rev-parse HEAD)" = "$POLYBOT_RELEASE_SHA"
git status --short --branch
```

The production tree must remain clean. Synchronizing the locked production
environment is a separate approved action and must use the exact reviewed
lock, CPython 3.12, `--locked`, `--no-dev`, and `--no-python-downloads`. Do not
install or update an unreviewed dependency.

```bash
uv sync --locked --no-dev --python 3.12.13 --no-python-downloads
.venv/bin/python --version
UV_CACHE_DIR=/tmp/polybot39-modernization-uv-cache uv lock --check
POLYBOT_ENV=production .venv/bin/python scripts/check_runtime_config.py
```

Run the redacted runtime check with exact `POLYBOT_ENV=production`. It must not
import models or connect to PostgreSQL or Discord. Do not start the bot yet.

`/operator database backup` deliberately has no release-specific preparation
step. The owner-only private confirmation runs exactly the fixed host wrapper
`/srv/polyelo/bin/polyelo-backup`, with no arguments or shell interpolation.
The command verifies the production runtime identity and requires that wrapper
to remain a root-controlled, non-symlink executable. The wrapper's own lock,
atomic publication, and artifact checks remain authoritative. Normal source
updates and rollbacks therefore do not require a separate command manifest.

### 4. Apply and verify the additive schema

The timezone and badge tools have independent fixed identities and confirmation
tokens. Each verifies only its reviewed columns; both exact verifications must
pass before model code starts.

First run read-only verify under its separate production-access approval:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_timezone_production.py --verify
```

If it reports the exact schema already complete, record that result and do not
force DDL. If it reports only the reviewed missing additions, obtain/confirm
the exact DDL approval and run:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_timezone_production.py \
  --apply \
  --confirm P9-B1-PRODUCTION-TIMEZONE-APPLY
```

Immediately rerun `--verify`. Any identity, metadata, lock, DDL, or
verification failure leaves the service stopped and follows the abort matrix
below. Never use `DROP COLUMN` as rollback.

Then run the separately approved read-only badge verification:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_badges_production.py --verify
```

If it reports the exact schema already complete, record that result and do not
force DDL. If it reports only the reviewed missing column, obtain/confirm the
exact DDL approval and run:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_badges_production.py \
  --apply \
  --confirm P12.1-PRODUCTION-PLAYER-BADGES-APPLY
```

Immediately rerun `--verify`. Do not start modernization model code unless the
timezone tool verifies both columns and the badge tool verifies
`public.player.badges`. Never use `DROP COLUMN` as rollback. Successful exact
verification of all three columns completes the schema compatibility gate.
Proceed directly to the task-disabled application canary; do not add a separate
old-code soak for columns that the rollback code ignores. If application
rollback is later required, retain all three columns as described below.

### 5. Run the reviewed task-disabled process canary

Install the tracked
`deploy/systemd/polyelo-modernization-canary.conf` as a temporary systemd
drop-in only after its digest matches the release record. Inspect the effective
unit before start. It must use the canonical Python 3.12 environment, exact
production profile, `--skip_tasks`, and `Restart=no`.

Start the service and verify:

- stable PID, zero restarts, production checkout, and release checkpoint;
- authenticated application `484067640302764042` before database effects;
- `polytopia2` and the reviewed role;
- expected guild allowlist with no unauthorized guild retained;
- background task loops remain disabled;
- the bounded post-identity startup ban reconciliation completes once;
- API remains inactive and startup performs no command synchronization;
- retained production prefix `guide` responds;
- representative player, team, and local-image reads render without mutation;
  and
- logs contain no traceback, identity/schema/database error, development path,
  wrong prefix, or unexpected extension failure.

Do not create, correct, or delete a production game for smoke testing.

The reviewed installation/start commands are:

```bash
sudo install -d -m 0755 /etc/systemd/system/polyelo.service.d
sudo install -m 0644 \
  deploy/systemd/polyelo-modernization-canary.conf \
  /etc/systemd/system/polyelo.service.d/modernization-canary.conf
cmp --silent \
  deploy/systemd/polyelo-modernization-canary.conf \
  /etc/systemd/system/polyelo.service.d/modernization-canary.conf
sudo systemctl daemon-reload
systemctl cat polyelo.service --no-pager
sudo systemctl start polyelo.service
```

### 6. Cleanly stop the canary and start the canonical service

Stop the task-disabled canary. Require `MainPID=0`, remove only the temporary
modernization canary drop-in, reload systemd, and prove the effective unit
matches `deploy/systemd/polyelo.service` and no longer contains
`--skip_tasks` or `Restart=no`.

```bash
sudo systemctl stop polyelo.service
sudo rm -f \
  /etc/systemd/system/polyelo.service.d/modernization-canary.conf
sudo systemctl daemon-reload
cmp --silent \
  deploy/systemd/polyelo.service \
  /etc/systemd/system/polyelo.service
systemctl cat polyelo.service --no-pager
sudo systemctl start polyelo.service
```

Start the canonical service. Verify exact release checkpoint, production
identity, stable PID, zero restart churn, schema, guild allowlist, retained
prefix response, API inactivity, and background-task health. Observe at least
five minutes and through one bounded cycle of each enabled recurring task.
Contain/reconciliation logging must remain healthy. A failed recurring item
must not terminate its later cycle.

### 7. Inspect and apply Main staff help plus the PolyChampions canary

Do this only after retained-prefix production health is established and under
separate Discord inspection/apply approvals. Stop the production service so a
second management client does not overlap the running bot.

Run `--mode inspect` for exactly Main and PolyChampions. It must report an
empty global tree and each selected guild's exact diff. A nonempty
global tree is a hard stop requiring a separately designed cleanup; never
bypass the guard.

Apply only after the reviewed inspect result, using every exact gate:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/manage_application_commands.py \
  --environment production \
  --mode apply \
  --guild-ids 283436219780825088,447883341463814144 \
  --confirm-environment production \
  --confirm-guild-ids 283436219780825088,447883341463814144 \
  --confirm-scope guild \
  --confirm-no-global-sync
```

Immediately inspect again and require convergence for both selected guilds
plus an empty global tree. Main gains only `/staffhelp`; PolyChampions gains
the reviewed canary roots plus `/staffhelp`. No unlisted guild may change.

Restart the canonical service and repeat identity, checkpoint, PID/restart,
retained-prefix, API, task, and log health checks. Exercise only the approved
bounded canary matrix: retained-prefix parity, one representative read and
write per assigned root using existing safe state, authorization denials, and
public/private visibility. Do not enable omitted capabilities to make the
matrix easier.

### 8. Finish and announce

End planned downtime before notifying testers. Record final code/schema/tree
state and monitoring ownership. A reviewed production announcement may be sent
only after the canonical bot and command tree are healthy and no further
planned service, database, or Discord operation remains. Announcement delivery
is the terminal deployment action.

## Abort and rollback matrix

### Failure before schema apply

Leave the old schema unchanged. Restore the reviewed rollback checkout and
ignored configuration if they were changed, synchronize only its reviewed
locked environment when required, and start it through a task-disabled canary
before returning to the canonical service. The fixed host backup wrapper does
not require release-specific regeneration. Do not touch the command tree.

### Failure after schema apply but before command apply

Keep both additive timezone columns. Stop failed release code, restore the
reviewed rollback code/configuration and locked Python 3.12 environment, run a
task-disabled identity/read canary, then restore the canonical service. The
rollback code may ignore the additive columns. Do not restore PostgreSQL and do
not drop columns.

### Failure after command apply

First restore service health with the reviewed code/config disposition. For a
native-surface-only failure, restore the reviewed Main and PolyChampions
assignments to the rollback desired state, then
stop the bot, plan, inspect, and explicitly apply only the exact affected
guilds with the same confirmations. Re-inspect convergence and the empty global
tree, then restart and verify the rollback's staff-help surface and retained
prefixes. Never clear or synchronize globally.

### Database restore

A database restore is not a modernization rollback. It discards legitimate
post-backup activity and requires a separate incident assessment, exact restore
approval, user-impact decision, and privacy-request replay plan. Additive
columns alone never justify a restore.

### Uncertain external effect

If a backup, DDL transaction, Discord sync, or announcement outcome is
uncertain, stop retrying and reconcile authoritative state first. Do not infer
failure from a client timeout and do not repeat a potentially accepted external
operation merely because local reporting failed.

## Post-cutover observation

For the approved observation window, record service PID/restarts, gateway
health, database/schema errors, recurring-task cycle health, retained-prefix
parity, native permission/visibility results, command-tree convergence, and
one safe local `/staffhelp` relay check per configured guild plus one PolyELO
feedback relay check to the maintainer channel. Roll back the narrowest
independent layer that is unhealthy. Expansion to another user-command
capability, prefix retirement, API activation, backfill, or destructive cleanup
is a new unit with separate approval.
