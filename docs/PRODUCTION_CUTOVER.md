# Python 3.12 production cutover and rollback

This is the reviewed runbook for moving the production PolyELO bot from its
legacy Python 3.9 virtual environment to the uv-managed Python 3.12
environment. It is documentation, not authorization to deploy.

## Scope and fixed boundaries

- Production checkout: `/home/nelluk/PolyBot39`
- Reviewed pre-upgrade commit: `43b3425`
- Upgrade branch: `dev-dependency-upgrade`
- Production service: `polytopia.service`
- Production database: `polytopia2`
- Production Discord bot ID: `484067640302764042`
- Production image root: `/home/nelluk/PolyBot39/data/images`
- Production log root: `/home/nelluk/PolyBot39/logs`
- Legacy interpreter retained for rollback:
  `/home/nelluk/PolyBot39/bin/python3` (Python 3.9.20)
- New interpreter after sync:
  `/home/nelluk/PolyBot39/.venv/bin/python` (Python 3.12.13)

The PostgreSQL server is not upgraded by this runbook. There is no application
schema migration and no reason to restore the database during a normal code
rollback.

`polyapi.service` is disabled and inactive. Keep it inactive during this
cutover. Its old `uvicorn.workers.UvicornWorker` command is not the reviewed
Gunicorn 26 configuration, so starting it requires a separate API approval and
service update.

## Reviewed state

- `origin/master` at the review baseline is an ancestor of all seven focused
  upgrade commits.
- The full upgrade changes 29 files with 3,881 insertions and 113 deletions
  before this Phase 8 documentation commit.
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

The service drop-in tracked at
`deploy/systemd/polytopia.service.d/upgrade.conf` adds the explicit production
profile and changes only the interpreter used by `polytopia.service`.

## Approval gate

Everything below changes or exercises production. Do not run it until Nelluk
has explicitly approved the production cutover and an acceptable maintenance
window.

## Cutover commands

### 1. Record and inspect the starting state

```bash
export POLYBOT_ROOT=/home/nelluk/PolyBot39
export POLYBOT_ROLLBACK_COMMIT=43b3425
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

### 2. Create and validate fresh backups

This step connects to the production database and writes backup files:

```bash
/home/nelluk/backup_db.sh
stat /home/nelluk/polytopia_full_backup.sqlc
ls -lh /home/nelluk/backups/polytopia_bak-*.sqlc
ls -lh /home/nelluk/backups/polytopia_images-*.tar.gz
/usr/bin/pg_restore --list /home/nelluk/polytopia_full_backup.sqlc >/dev/null
```

Confirm that the full dump and current weekday image archive have fresh
timestamps and nonzero sizes. Do not continue after any backup or validation
error.

### 3. Fast-forward to the reviewed merge

The upgrade branch must already be reviewed, pushed, and merged into
`origin/master`:

```bash
cd "$POLYBOT_ROOT"
git fetch origin
git merge-base --is-ancestor "$POLYBOT_ROLLBACK_COMMIT" origin/master
git pull --ff-only origin master
git log --oneline "$POLYBOT_ROLLBACK_COMMIT"..HEAD
git status --short --branch
```

Stop if the update is not a fast-forward, contains commits outside the
reviewed upgrade, or leaves a dirty worktree.

### 4. Add the explicit production policy

```bash
nano /home/nelluk/PolyBot39/config.ini
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

### 6. Install and inspect the systemd drop-in

These are the sudo commands Nelluk must run:

```bash
sudo install -d -m 0755 /etc/systemd/system/polytopia.service.d
sudo install -m 0644 \
  /home/nelluk/PolyBot39/deploy/systemd/polytopia.service.d/upgrade.conf \
  /etc/systemd/system/polytopia.service.d/upgrade.conf
sudo systemctl daemon-reload
systemctl cat polytopia.service --no-pager
```

Confirm that the effective unit has `POLYBOT_ENV=production` and the new
`.venv/bin/python` `ExecStart`. Installing the drop-in and reloading systemd
does not restart the currently running bot.

### 7. Perform the one approved restart

```bash
sudo systemctl restart polytopia.service
systemctl show polytopia.service \
  -p ActiveState -p SubState -p MainPID -p ExecStart --no-pager
journalctl -u polytopia.service --since "5 minutes ago" --no-pager
```

Stop and use the rollback below if the service is not active, repeatedly
restarts, authenticates as the wrong bot, loads an unauthorized guild, reports
database errors, or cannot load the Bullet extension.

### 8. Production smoke checklist

- The log reports Python/discord.py startup and bot ID `484067640302764042`.
- Expected production guilds load; no guild is unexpectedly left.
- Gateway heartbeats remain healthy for at least five minutes.
- The normal production prefix responds to `guide`.
- A read-only player lookup and team lookup render.
- One existing local-image team card renders.
- No development path, database, bot ID, or `!` prefix appears.
- Background tasks run without a new exception.
- `polyapi.service` remains inactive.
- The service PID differs from the pre-cutover PID and remains stable.

Do not create or correct a production game merely for smoke testing.

## Immediate rollback

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
logs, legacy environment, and new `.venv` remain in place.

After the incident is understood, return the checkout to the branch without
discarding anything:

```bash
cd /home/nelluk/PolyBot39
git switch master
```

A database restore is an exceptional data-recovery action, not a dependency
rollback step. It would discard legitimate activity after the dump and
requires its own explicit approval.

## Separately gated API work

If the inactive HTTP API is needed later, first set `api_enabled = true`, add
`POLYBOT_ENV=production` to its unit, and replace the obsolete worker command
with Gunicorn 26's native ASGI worker:

```text
/home/nelluk/PolyBot39/.venv/bin/gunicorn --workers 1 \
  --worker-class asgi --asgi-loop uvloop --asgi-lifespan on server:server
```

Test the API independently before enabling or starting `polyapi.service`.
