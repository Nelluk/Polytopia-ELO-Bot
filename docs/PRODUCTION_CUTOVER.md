# Python 3.12 production cutover and rollback

This runbook records the reviewed and completed move of the production PolyELO
bot from its legacy Python 3.9 virtual environment to the uv-managed Python
3.12 environment. It also remains the recovery reference for this deployment;
it is not standing authorization for a future deployment or rollback.

## Completion record

- Pull request 136 was merged as
  `75b24b5e79e997477014aa979d87dc5f6d162bc5`.
- The production cutover completed successfully on 2026-07-28.
- A task-disabled Python 3.12 canary passed startup and command smoke checks
  before background tasks were enabled.
- The final process passed a five-minute stability window with no restart,
  traceback, unauthorized guild, database error, or extension failure.
- The permanent systemd drop-in matched the reviewed tracked file exactly.
- `polyapi.service` remained disabled and inactive.
- PostgreSQL was not upgraded, restored, or reconfigured.
- The pre-cutover and stopped-service recovery artifacts are stored in the
  private directory
  `/home/nelluk/backups/polybot-cutover-20260728T173159-0400`.
- The separately approved post-cutover cleanup completed on 2026-08-04. It
  installed the complete tracked systemd unit, removed the Python 3.9
  environment, retained the private archive, and caused no bot restart.

## Scope and fixed boundaries

- Production checkout: `/home/nelluk/PolyBot39`
- Reviewed pre-upgrade commit: `43b3425`
- Reviewed merge commit:
  `75b24b5e79e997477014aa979d87dc5f6d162bc5`
- Upgrade branch: `dev-dependency-upgrade`
- Production service: `polytopia.service`
- Production database: `polytopia2`
- Production Discord bot ID: `484067640302764042`
- Production image root: `/home/nelluk/PolyBot39/data/images`
- Production log root: `/home/nelluk/PolyBot39/logs`
- Legacy interpreter retained through the settling period, then retired on
  2026-08-04: `/home/nelluk/PolyBot39/bin/python3` (Python 3.9.20)
- New interpreter after sync:
  `/home/nelluk/PolyBot39/.venv/bin/python` (Python 3.12.13)

The PostgreSQL server is not upgraded by this runbook. There is no application
schema migration and no reason to restore the database during a normal code
rollback.

`polyapi.service` is disabled and inactive. Keep it inactive. Its old
Python 3.9 `uvicorn.workers.UvicornWorker` command was never the reviewed
Gunicorn 26 configuration and is unavailable after post-cutover cleanup, so
starting it requires a separate API approval and Python 3.12 service update.

## Reviewed state

- The merged upgrade contains eight focused commits after `43b3425`.
- The merged upgrade changes 35 files with 4,337 insertions and 185 deletions.
- The production checkout is clean at `43b3425`.
- `polytopia.service` is active and runs:

  ```text
  /home/nelluk/PolyBot39/bin/python3 /home/nelluk/PolyBot39/bot.py
  ```

- The production configuration passes the new redacted loader without
  importing models or connecting to PostgreSQL or Discord.
- The current backup job runs at 01:00, 09:00, and 17:00. It creates a full
  custom-format PostgreSQL dump and a weekday-rotated local-image archive.
- The latest audited filesystem has 4.9 GB free. The tested development
  `.venv` uses about 437 MB and the managed Python uses about 110 MB.
- uv 0.11.32 and managed CPython 3.12.13 are already installed for user
  `nelluk`.

## Final runtime

`pyproject.toml` requires Python `>=3.12,<3.13`. The direct production
dependencies are:

```text
discord.py==2.7.1
fastapi==0.139.2
google-auth==2.56.2
gspread-asyncio==2.0.0
gunicorn==26.0.0
httptools==0.8.0
matplotlib==3.11.1
pandas==3.0.5
peewee==3.19.0
pillow==12.3.0
psycopg2-binary==2.9.12
pydantic==2.13.4
requests==2.34.2
scipy==1.18.0
uvicorn==0.51.0
uvloop==0.22.1
```

The lock resolves 80 packages. `pip-audit` is a development dependency and is
excluded from production with `--no-dev`.

Astral's current
[uv CLI documentation](https://docs.astral.sh/uv/reference/cli/) says
`--locked` requires the lockfile to be up to date, while `--frozen` merely
uses an existing lockfile without that check. Production therefore uses:

```bash
uv sync --locked --no-dev --python 3.12.13
```

## Configuration preparation

Before the service restart, edit the ignored production `config.ini` and add
these explicit non-secret policy values under `[DEFAULT]`:

```ini
expected_bot_id = 484067640302764042
background_tasks_enabled = true
api_enabled = false
bullet_enabled = true
image_root = data/images
log_root = logs
```

Do not change `discord_key`, `psql_user`, `psql_db`, `owner_id`, or any
database authentication value during this cutover. Blank PostgreSQL host and
port values preserve the current local Unix-socket connection. The
`spreadsheet_creds.json` file must remain present because the production
Bullet extension stays enabled.

During the cutover, the service drop-in then tracked at
`deploy/systemd/polytopia.service.d/upgrade.conf` added the explicit production
profile and changed only the interpreter used by `polytopia.service`.

Post-cutover cleanup replaces that temporary override with the complete unit
tracked at `deploy/systemd/polytopia.service`. The reviewed cleanup procedure
is in `docs/POST_UPGRADE_CLEANUP.md`. It completed on 2026-08-04: the complete
unit is installed under `/etc`, the installed drop-in and non-package-owned
`/lib` unit are absent, and the legacy Python environment is retired.

## Approval gate

The commands below are the completed procedure and a future recovery
reference. Everything below changes or exercises production. Do not repeat any
part of it until Nelluk has explicitly approved the exact production action
and an acceptable maintenance window.

## Cutover commands

### 1. Record and inspect the starting state

```bash
export POLYBOT_ROOT=/home/nelluk/PolyBot39
export POLYBOT_ROLLBACK_COMMIT=43b3425
export POLYBOT_UPGRADE_COMMIT=75b24b5e79e997477014aa979d87dc5f6d162bc5
cd "$POLYBOT_ROOT"
git status --short --branch
git rev-parse HEAD
systemctl show polytopia.service \
  -p ActiveState -p SubState -p MainPID -p ExecStart --no-pager
systemctl show polyapi.service -p ActiveState -p SubState --no-pager
df -h /
```

Stop if the production checkout is dirty, the bot is not active, the API is
active, the current commit is unexpected, or disk space is materially below
the reviewed value.

### 2. Preserve ignored state and create fresh backups

This step connects to the production database and writes backup files. The
deployed `/home/nelluk/backup_db.sh` must match the reviewed source at
`scripts/backup_db.sh`:

```bash
export POLYBOT_CUTOVER_ARCHIVE=/home/nelluk/backups/polybot-cutover-$(date +%Y%m%dT%H%M%S%z)
install -d -m 0700 "$POLYBOT_CUTOVER_ARCHIVE"
install -m 0600 /home/nelluk/PolyBot39/config.ini \
  "$POLYBOT_CUTOVER_ARCHIVE/config.ini"
install -m 0600 /home/nelluk/PolyBot39/server_settings.py \
  "$POLYBOT_CUTOVER_ARCHIVE/server_settings.py"
install -m 0600 /home/nelluk/PolyBot39/spreadsheet_creds.json \
  "$POLYBOT_CUTOVER_ARCHIVE/spreadsheet_creds.json"

/home/nelluk/backup_db.sh
stat /home/nelluk/polytopia_full_backup.sqlc
ls -lh /home/nelluk/backups/polytopia_bak-*.sqlc
ls -lh /home/nelluk/backups/polytopia_images-*.tar.gz
/usr/bin/pg_restore --list /home/nelluk/polytopia_full_backup.sqlc >/dev/null
```

Confirm that the full dump and current weekday image archive have fresh
timestamps and nonzero sizes. Do not continue after any backup or validation
error. The hardened backup script writes and validates temporary files before
atomically replacing the published backup paths, and refuses overlapping runs.

### 3. Fast-forward to the reviewed merge

The upgrade branch must already be reviewed, pushed, and merged into
`origin/master`:

```bash
cd "$POLYBOT_ROOT"
git fetch origin
git merge-base --is-ancestor "$POLYBOT_ROLLBACK_COMMIT" origin/master
test "$(git rev-parse origin/master)" = "$POLYBOT_UPGRADE_COMMIT"
git pull --ff-only origin master
git log --oneline "$POLYBOT_ROLLBACK_COMMIT"..HEAD
git status --short --branch
```

Stop if the update is not a fast-forward, contains commits outside the
reviewed upgrade, or leaves a dirty worktree.

### 4. Add the explicit production policy

```bash
nano /home/nelluk/PolyBot39/config.ini
chmod 600 \
  /home/nelluk/PolyBot39/config.ini \
  /home/nelluk/PolyBot39/spreadsheet_creds.json
```

Do not start or import the bot yet. The redacted configuration check runs with
the new environment in the next step.

### 5. Build and verify the new environment while the old bot remains live

```bash
cd "$POLYBOT_ROOT"
uv sync \
  --locked \
  --no-dev \
  --python 3.12.13 \
  --no-python-downloads
.venv/bin/python --version
.venv/bin/python scripts/dependency_inventory.py
POLYBOT_ENV=production \
  .venv/bin/python scripts/check_runtime_config.py
UV_CACHE_DIR=/tmp/polybot39-cutover-uv-cache uv lock --check
POLYBOT_ENV=production \
  MPLCONFIGDIR=/tmp/polybot39-cutover-matplotlib \
  .venv/bin/python -m unittest discover -v
git status --short --branch
```

Expected results are Python 3.12.13, 48 passing offline tests, five explicitly
skipped development-database tests, an 80-package lock, and a clean worktree.
The service is still using the legacy Python 3.9 process during these checks.
The redacted configuration output must identify bot `484067640302764042`,
database `polytopia2`, the production guild set, enabled background tasks,
disabled HTTP API, enabled Bullet integration, and the production image/log
roots. The inspection command does not import models or connect to external
systems.

### 6. Stop the legacy bot and create the final recovery point

This is the beginning of downtime. The earlier backup protects preparation;
this second backup captures a recovery point after the only application that
writes the database has stopped.

```bash
sudo systemctl stop polytopia.service
systemctl show polytopia.service \
  -p ActiveState -p SubState -p MainPID --no-pager

/home/nelluk/backup_db.sh
/usr/bin/pg_restore --list /home/nelluk/polytopia_full_backup.sqlc >/dev/null
tar -tzf "/home/nelluk/backups/polytopia_images-$(date +%A).tar.gz" >/dev/null

export POLYBOT_STOPPED_STAMP=$(date +%Y%m%dT%H%M%S%z)
install -m 0600 /home/nelluk/polytopia_full_backup.sqlc \
  "$POLYBOT_CUTOVER_ARCHIVE/final-stopped-polytopia-full-${POLYBOT_STOPPED_STAMP}.sqlc"
install -m 0600 "/home/nelluk/backups/polytopia_bak-$(date +%A).sqlc" \
  "$POLYBOT_CUTOVER_ARCHIVE/final-stopped-polytopia-partial-${POLYBOT_STOPPED_STAMP}.sqlc"
install -m 0600 "/home/nelluk/backups/polytopia_images-$(date +%A).tar.gz" \
  "$POLYBOT_CUTOVER_ARCHIVE/final-stopped-images-${POLYBOT_STOPPED_STAMP}.tar.gz"
```

Validate both custom-format dumps and the copied image archive. Do not start a
new bot process after any backup or validation failure.

### 7. Start a task-disabled Python 3.12 canary

Create `/tmp/polybot39-canary-upgrade.conf` with:

```ini
[Service]
Environment=POLYBOT_ENV=production
ExecStart=
ExecStart=/home/nelluk/PolyBot39/.venv/bin/python /home/nelluk/PolyBot39/bot.py --skip_tasks
```

Install and inspect it:

```bash
sudo install -d -m 0755 /etc/systemd/system/polytopia.service.d
sudo install -m 0644 \
  /tmp/polybot39-canary-upgrade.conf \
  /etc/systemd/system/polytopia.service.d/upgrade.conf
sudo systemctl daemon-reload
systemctl cat polytopia.service --no-pager
sudo systemctl start polytopia.service
```

The canary deliberately retains the base unit's existing restart policy; the
completed cutover did not add a restart-policy override. Confirm that the
effective `ExecStart` uses `.venv/bin/python`, selects the production profile,
and ends with `--skip_tasks`.

### 8. Canary smoke checklist

- The service stays active with a stable, new PID and zero restarts.
- The database opens and the Discord gateway connects.
- Runtime bot-ID validation accepts production bot `484067640302764042`.
- Expected production guilds load and no unauthorized guild is retained.
- The Bullet extension loads without error.
- No background task loop starts.
- The normal production prefix responds to `guide`.
- A read-only player lookup and team lookup render.
- One existing local-image team card renders.
- No development path, database, bot ID, or `!` prefix appears.
- `polyapi.service` remains inactive.

Do not create or correct a production game merely for smoke testing. Use the
immediate rollback if identity, guild, database, or extension checks fail.

### 9. Activate background tasks and observe

Replace the temporary canary drop-in with the reviewed permanent file:

```bash
sudo install -m 0644 \
  /home/nelluk/PolyBot39/deploy/systemd/polytopia.service.d/upgrade.conf \
  /etc/systemd/system/polytopia.service.d/upgrade.conf
sudo systemctl daemon-reload
systemctl cat polytopia.service --no-pager
sudo systemctl restart polytopia.service
```

Confirm that `--skip_tasks` is absent and that the installed file checksum
matches the tracked file. Observe the service for at least five minutes:

```bash
systemctl show polytopia.service \
  -p ActiveState -p SubState -p MainPID -p ExecStart \
  -p NRestarts -p Result --no-pager
journalctl -u polytopia.service --since "10 minutes ago" --no-pager
systemctl show polyapi.service -p ActiveState -p SubState --no-pager
git status --short --branch
```

Background matchmaking, reminder, match-list, and confirmation tasks must run
without a new exception. Stop and use the rollback below if the service is not
active, repeatedly restarts, authenticates as the wrong bot, loads an
unauthorized guild, reports database errors, or cannot load the Bullet
extension.

## Historical immediate rollback — retired

The commands below record the rollback that was available during the cutover
and settling period. They are no longer executable because the Python 3.9
environment, base `/lib` unit, and cutover drop-in were retired on 2026-08-04.
Do not run them. Current code recovery must retain the canonical Python 3.12
unit and rebuild the locked `.venv` if necessary.

Rollback the code and interpreter without restoring PostgreSQL:

```bash
sudo systemctl stop polytopia.service
cd /home/nelluk/PolyBot39
git switch --detach "$POLYBOT_ROLLBACK_COMMIT"
sudo rm -f /etc/systemd/system/polytopia.service.d/upgrade.conf
sudo systemctl daemon-reload
sudo systemctl start polytopia.service
systemctl show polytopia.service \
  -p ActiveState -p SubState -p MainPID -p ExecStart --no-pager
journalctl -u polytopia.service --since "5 minutes ago" --no-pager
```

The unchanged base unit then uses
`/home/nelluk/PolyBot39/bin/python3 /home/nelluk/PolyBot39/bot.py`. The ignored
production configuration, server settings, spreadsheet credentials, images,
logs, legacy environment, and new `.venv` remain in place. Timestamped copies
of the ignored configuration and both pre-cutover and stopped-service recovery
points remain in the private cutover archive.

Leave the checkout detached at `43b3425` while the legacy systemd command is
active. Do **not** switch back to upgraded `master` while the drop-in is
removed: a later restart would then run upgraded source under the legacy
Python 3.9 interpreter. Return to `master` only as part of a reviewed recovery
that also restores the Python 3.12 drop-in, or deploy a separately reviewed
legacy-compatible recovery branch.

A database restore is an exceptional data-recovery action, not a dependency
rollback step. It would discard legitimate activity after the dump and
requires its own explicit approval.

The legacy Python 3.9 environment and private cutover archive were retained
through the settling period. The separately approved cleanup later removed
only the legacy environment; the private archive remains retained.

That action is complete and recorded in `docs/POST_UPGRADE_CLEANUP.md`. This
immediate rollback procedure is now a historical cutover record: removing a
drop-in cannot select Python 3.9. Recovery instead uses the tracked canonical
Python 3.12 unit and locked environment, with the retained private archive
available only for a separately reviewed reconstruction.

## Separately gated API work

If the inactive HTTP API is needed later, first set `api_enabled = true`, add
`POLYBOT_ENV=production` to its unit, and replace the obsolete worker command
with Gunicorn 26's native ASGI worker:

```text
/home/nelluk/PolyBot39/.venv/bin/gunicorn --workers 1 \
  --worker-class asgi --asgi-loop uvloop --asgi-lifespan on server:server
```

Test the API independently before enabling or starting `polyapi.service`.
