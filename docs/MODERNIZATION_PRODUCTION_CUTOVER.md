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

- Checkout: `/home/nelluk/PolyBot39`
- Service: `polytopia.service`
- Canonical unit source: `deploy/systemd/polytopia.service`
- Locked runtime: `.venv/bin/python`, CPython 3.12, `uv.lock`, production
  dependencies only
- Environment: exact `POLYBOT_ENV=production`
- Database: `polytopia2`; role from the reviewed production profile and live
  `current_user`
- Discord application: `484067640302764042`
- Initial native canary guild: PolyChampions, `478571892832206869`
- Initial native capabilities: `core_user`, `team`, `league`, `house`, `squad`
- All configured production guilds: `tools_support` exposing only `/staffhelp`
- Initially omitted capabilities: `operator`, `elo_maintenance`, and
  `beta_testing`
- HTTP API: disabled and inactive
- Legacy prefixes: retained wherever the compatibility ledger says `retain`
- Global application-command tree: must be empty; this repository has no
  authority or tooling to mutate it

The additive timezone columns are the only schema change. The old whole-hour
field and identity fields remain untouched; there is no backfill or destructive
schema rollback.

## Required approval and release record

Before the maintenance window, prepare one reviewed, non-secret release record
that contains all of the following exact values and evidence:

- 40-character release commit and rollback commit;
- proof both commits are reviewed ancestors of the approved production branch;
- `uv.lock` digest and the canonical systemd-unit digest;
- expected redacted runtime identity, configured database role, allowlisted
  guild IDs, and disabled API state;
- the exact ignored production capability assignment and a diff showing only
  `/staffhelp` in every configured guild plus the approved PolyChampions
  canary roots;
- proof every configured guild's `staff_help_channel` and first
  `helper_roles` entry resolve to the reviewed private relay destination and
  configured helper role;
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
- `psql_db = polytopia2` and the reviewed nonempty role;
- `background_tasks_enabled = true`, `api_enabled = false`, and the existing
  reviewed Bullet policy;
- only reviewed production guilds are allowlisted;
- PolyChampions `478571892832206869` receives exactly
  `('core_user', 'team', 'league', 'house', 'squad')` for the initial canary;
- `application_command_all_guild_capabilities` is exactly
  `('tools_support',)`, exposing only `/staffhelp` in every allowlisted guild;
- every allowlisted guild has a valid `staff_help_channel` and nonempty first
  `helper_roles` entry;
- `operator`, `elo_maintenance`, and `beta_testing` are not assigned; and
- the configured prefix, image root, and log root remain production values.

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
4. Run the application-command desired-state plan offline with exact
   production selection and the release record's comma-separated list of all
   allowlisted production guild IDs:

   ```bash
   POLYBOT_ENV=production \
   .venv/bin/python scripts/manage_application_commands.py \
     --environment production \
     --mode plan \
     --guild-ids REPLACE_WITH_APPROVED_PRODUCTION_GUILD_IDS
   ```

   Review exact create/update/unchanged/remove roots. Every selected guild must
   receive only `/staffhelp`, except PolyChampions, which also receives the
   approved user-canary roots. The desired roots must match the release record.
5. Verify the approved release and rollback commits, lockfile, unit, canary
   drop-in, migration tool, and command manager are present in the reviewed
   Git history. Do not update the production checkout yet.
6. Verify the routine backup state and obtain the separate approval for a fresh
   pre-stop backup. Validate custom-format dumps with `pg_restore --list` and
   image archives with `tar -tzf`; timestamps, sizes, and paths must match the
   release record.
7. Confirm every configured production staff-help channel and first Helper role
   still resolves exactly. Do not proceed if any guild would accept the command
   without a working private destination and controlled role mention.

Do not announce completion or invite testing at this stage.

## Maintenance sequence

The order below is mandatory: backup, stop, prove one-writer shutdown, deploy
reviewed source without starting it, apply and verify additive schema, run the
task-disabled canary, cleanly stop it, start the canonical service, then handle
the Discord command tree as a separate gate.

### 1. Capture start state and fresh backup

After the exact production-read and backup approvals, record the clean
checkout commit, service PID/state/restart count/effective command, API state,
disk space, redacted runtime identity, and latest logs. Run the reviewed backup
workflow and validate every required artifact. Stop before downtime if the
checkout is dirty, the commit is unexpected, the service is unhealthy, the API
is active, or backup validation is incomplete.

The reviewed backup source must belong to the clean release history and match
the deployed owner-only script byte for byte. The invoked reporting exporter
and Python path must also be the reviewed release files. A cutover backup
requires exit status zero; a reporting-partial or lock-busy result is not an
acceptable recovery point. The bounded command sequence is:

```bash
test "$(git rev-parse HEAD)" = "$POLYBOT_ROLLBACK_SHA"
git status --short --branch
cmp --silent scripts/backup_db.sh /home/nelluk/backup_db.sh
/home/nelluk/backup_db.sh
/usr/bin/pg_restore --list /home/nelluk/polytopia_full_backup.sqlc >/dev/null
/usr/bin/tar -tzf "/home/nelluk/backups/polytopia_images-$(date +%A).tar.gz" >/dev/null
```

### 2. Stop the production writer and prove it is absent

After the exact lifecycle approval:

```bash
sudo systemctl stop polytopia.service
systemctl show polytopia.service \
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

Fetch without merging, prove the approved release is the exact reviewed remote
commit and descends from the rollback commit, then update only by the reviewed
fast-forward path. Require:

```bash
test "$(git symbolic-ref --short HEAD)" = master
git status --short --branch
git rev-parse HEAD
git fetch origin master
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

Prepare the private operator-backup release manifest only after the checkout
and locked interpreter match the approved release. The first command is a
read-only plan. Installing the ignored manifest is a distinct production-file
write approval; it does not run a backup or connect to PostgreSQL. The final
command rereads the private manifest and revalidates the clean exact checkout,
tracked shell and exporter, owner-only deployed shell, and actual interpreter
identity. Any symlink, tracked change, digest mismatch, wrong checkpoint, or
unexpected owner/mode blocks the Discord-triggered backup before process spawn.

```bash
POLYBOT_ENV=production .venv/bin/python \
  scripts/manage_production_backup_release.py \
  --checkpoint "$POLYBOT_RELEASE_SHA"
POLYBOT_ENV=production .venv/bin/python \
  scripts/manage_production_backup_release.py \
  --checkpoint "$POLYBOT_RELEASE_SHA" \
  --apply \
  --confirm P9-M3-PRODUCTION-BACKUP-RELEASE-APPLY
POLYBOT_ENV=production .venv/bin/python \
  scripts/manage_production_backup_release.py \
  --checkpoint "$POLYBOT_RELEASE_SHA" \
  --validate
```

The manifest is non-secret provenance at
`/home/nelluk/PolyBot39/.operator-backup-release.json`, mode `0600`. Archive
its JSON with the reviewed release record. Regenerate it after any approved
release or rollback that changes the checkpoint, backup shell, reporting
exporter, or interpreter. Never hand-edit it to bypass a validation failure.

### 4. Apply and verify the additive schema

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

Immediately rerun `--verify`. Do not start any modernization model code unless
verify returns success for both exact columns. Any identity, metadata, lock,
DDL, or verification failure leaves the service stopped and follows the abort
matrix below. Never use `DROP COLUMN` as rollback.

### 5. Run the reviewed task-disabled process canary

Install the tracked
`deploy/systemd/polytopia-modernization-canary.conf` as a temporary systemd
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
sudo install -d -m 0755 /etc/systemd/system/polytopia.service.d
sudo install -m 0644 \
  deploy/systemd/polytopia-modernization-canary.conf \
  /etc/systemd/system/polytopia.service.d/modernization-canary.conf
cmp --silent \
  deploy/systemd/polytopia-modernization-canary.conf \
  /etc/systemd/system/polytopia.service.d/modernization-canary.conf
sudo systemctl daemon-reload
systemctl cat polytopia.service --no-pager
sudo systemctl start polytopia.service
```

### 6. Cleanly stop the canary and start the canonical service

Stop the task-disabled canary. Require `MainPID=0`, remove only the temporary
modernization canary drop-in, reload systemd, and prove the effective unit
matches `deploy/systemd/polytopia.service` and no longer contains
`--skip_tasks` or `Restart=no`.

```bash
sudo systemctl stop polytopia.service
sudo rm -f \
  /etc/systemd/system/polytopia.service.d/modernization-canary.conf
sudo systemctl daemon-reload
cmp --silent \
  deploy/systemd/polytopia.service \
  /etc/systemd/system/polytopia.service
systemctl cat polytopia.service --no-pager
sudo systemctl start polytopia.service
```

Start the canonical service. Verify exact release checkpoint, production
identity, stable PID, zero restart churn, schema, guild allowlist, retained
prefix response, API inactivity, and background-task health. Observe at least
five minutes and through one bounded cycle of each enabled recurring task.
Contain/reconciliation logging must remain healthy. A failed recurring item
must not terminate its later cycle.

### 7. Inspect and apply all-guild staff help plus the PolyChampions canary

Do this only after retained-prefix production health is established and under
separate Discord inspection/apply approvals. Stop the production service so a
second management client does not overlap the running bot.

Run `--mode inspect` for the exact approved production guild-ID list. It must
report an empty global tree and each selected guild's exact diff. A nonempty
global tree is a hard stop requiring a separately designed cleanup; never
bypass the guard.

Apply only after the reviewed inspect result, using every exact gate:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/manage_application_commands.py \
  --environment production \
  --mode apply \
  --guild-ids REPLACE_WITH_APPROVED_PRODUCTION_GUILD_IDS \
  --confirm-environment production \
  --confirm-guild-ids REPLACE_WITH_APPROVED_PRODUCTION_GUILD_IDS \
  --confirm-scope guild \
  --confirm-no-global-sync
```

Immediately inspect again and require convergence for every selected guild
plus an empty global tree. Non-PolyChampions guilds may gain only `/staffhelp`;
PolyChampions may also gain the reviewed canary roots. No unlisted guild may
change.

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
before returning to the canonical service. If the rollback retains the
manifest-aware backup command, regenerate and validate its private provenance
against `POLYBOT_ROLLBACK_SHA` before start. An older rollback may ignore the
manifest. Do not touch the command tree.

### Failure after schema apply but before command apply

Keep both additive timezone columns. Stop failed release code, restore the
reviewed rollback code/configuration and locked Python 3.12 environment, run a
task-disabled identity/read canary, then restore the canonical service. The
rollback code may ignore the additive columns. Do not restore PostgreSQL and do
not drop columns.

### Failure after command apply

First restore service health with the reviewed code/config disposition. For a
native-surface-only failure, restore both the reviewed PolyChampions assignment
and all-guild `tools_support` assignment to the rollback desired state, then
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
one safe `/staffhelp` relay check per configured guild. Roll back the narrowest
independent layer that is unhealthy. Expansion to another user-command
capability, prefix retirement, API activation, backfill, or destructive cleanup
is a new unit with separate approval.
