# Polytopia-ELO-Bot
A discord bot for the game Polytopia, to enable matchmaking and leaderboards.
Requires CPython 3.12, PostgreSQL, and
[uv](https://docs.astral.sh/uv/). The deployed environment is locked by
`pyproject.toml` and `uv.lock`.

## Policies and support

- [Privacy Policy](PRIVACY.md)
- [Security Policy](SECURITY.md)
- [Data Retention Schedule](docs/DATA_RETENTION.md)
- [Privacy Readiness Checklist](docs/PRIVACY_READINESS_CHECKLIST.md)

For a privacy request in the wider beta, invoke `/staffhelp` with no options.
In the modal, use `Privacy request` as the short summary, put `Please contact
me about my PolyELO data` in the detailed description, and add relevant account
or other information in the optional context field. The native JSONL record is
development-only, so `/staffhelp` is not yet a production-ready replacement.
Before the separately approved P9 rollout decision, production communities
should use their currently deployed support/moderator route. Do not post
Discord IDs, credentials, or other sensitive details in a public GitHub issue.

Create an application and application bot account at the Discord developer portal: https://discord.com/developers/applications

```
git clone <git repo address>
cd /new/project/path
uv sync --locked --no-dev --python 3.12
```

Make a copy of config.ini and server_settings.py using the example template files.

Change the required settings inside config.ini, which include the API key from the developer portal above.

Create an empty PostgreSQL database and configure its name, role, password,
host, and port. See the [test database setup guide](docs/DATABASE_SETUP.md) for
a complete development example.

Select the runtime profile explicitly and run the bot through the synced
environment:

```
POLYBOT_ENV=production .venv/bin/python bot.py
```

Use a separate development configuration, Discord application, and database
for testing:

```
POLYBOT_ENV=development .venv/bin/python bot.py --skip_tasks
```

## Dependency upgrade safety checks

The approved Python 3.12 development-environment strategy and phased execution
plan are documented in
[`docs/DEPENDENCY_UPGRADE_HANDOFF.md`](docs/DEPENDENCY_UPGRADE_HANDOFF.md).

Run the offline compatibility suite without connecting to Discord or
PostgreSQL:

```
POLYBOT_ENV=development .venv/bin/python -m unittest discover -v
```

Capture the active interpreter and all installed distribution versions without
depending on `pip`:

```
.venv/bin/python scripts/dependency_inventory.py
.venv/bin/python scripts/dependency_inventory.py --json
```

The pre-upgrade production snapshot is stored in
`docs/dependency-baseline-2026-07-27.txt`.

Use a dedicated Discord application, PostgreSQL database, and configuration for
live upgrade testing. Changing only the Discord token is not sufficient
isolation: importing `modules.models` creates missing tables, and bot commands
and background tasks can write to the configured database.

## Runtime image data

Team and house images uploaded as Discord attachments are normalised and stored
under `data/images/`. This directory is intentionally excluded from Git and must
be included in server backups. Direct HTTP(S) image URLs remain stored in
PostgreSQL and are used whenever no local image exists.

The tracked backup script is `scripts/backup_db.sh`; the live server copy is
deployed at `/home/nelluk/backup_db.sh`.

The reviewed production procedure is documented in
[`docs/PRODUCTION_CUTOVER.md`](docs/PRODUCTION_CUTOVER.md). Running that
procedure requires separate production approval.
