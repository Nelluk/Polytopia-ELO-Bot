# Polytopia-ELO-Bot
A Discord bot for Polytopia matchmaking, ELO leaderboards, match history, and
private game-channel automation.

[![Self-hosting smoke test](https://github.com/Nelluk/Polytopia-ELO-Bot/actions/workflows/self-hosting-smoke.yml/badge.svg)](https://github.com/Nelluk/Polytopia-ELO-Bot/actions/workflows/self-hosting-smoke.yml)

## Recommended self-hosting path

The recommended independent deployment uses Docker Compose and includes a
private PostgreSQL database. Install Docker Engine or Docker Desktop with the
Compose plugin, create a Discord application, and follow **[Run PolyBot with
Docker Compose](docs/DOCKER.md)**.

A new installation needs more than the Discord token. Gather:

- the bot token and bot user ID from the Discord developer portal;
- your Discord user ID, server ID, and one bot-command channel ID;
- two new database passwords; and
- the numeric host UID/GID that will own persistent files on Linux.

The guide walks through the remaining schema, seed-data, slash-command,
permission, startup, and backup steps. Normal startup does not silently change
the database schema or synchronize Discord commands.

The alternative native installation requires CPython 3.12, PostgreSQL, Git,
and [uv](https://docs.astral.sh/uv/). It is documented in **[Self-hosting
PolyBot](docs/SELF_HOSTING.md)**. Dependencies are locked by `pyproject.toml`
and `uv.lock`.

## Policies and support

- [Privacy Policy](PRIVACY.md)
- [Security Policy](SECURITY.md)
- [Data Retention Schedule](docs/DATA_RETENTION.md)
- [Privacy Readiness Checklist](docs/PRIVACY_READINESS_CHECKLIST.md)

Users of the official upstream PolyELO deployment can invoke `/staffhelp` with
no options for staff support or a privacy request. Do not put personal data,
credentials, or vulnerability details in a public GitHub issue.

The policies above describe the official upstream PolyELO deployment.
Independent operators are responsible for publishing accurate policies and
support contacts for their own instance. `/staffhelp` is intentionally disabled
in the installation-neutral example until its private delivery routes are
configured; see the self-hosting guide before enabling `tools_support`.

Upstream maintainers run the isolated beta through the separate, direct
[development Compose interface](docs/DEVELOPMENT_DOCKER.md). It uses the same
root Dockerfile as the public stack and ordinary Compose commands.

The active upstream production Compose deployment is documented separately in
[docs/PRODUCTION_DOCKER.md](docs/PRODUCTION_DOCKER.md). It contains GreenCloud-
specific paths and credentials integration and is not the default self-hosting
path.

For an isolated test bot, use a separate Discord application, guild, and
database. See [Database setup for a test bot](docs/DATABASE_SETUP.md).

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
isolation: schema operations, bot commands, and background tasks can write to
the configured database.

## Runtime image data

Team and house images uploaded as Discord attachments are normalised and stored
under `data/images/`. This directory is intentionally excluded from Git and must
be included in server backups. Direct HTTP(S) image URLs remain stored in
PostgreSQL and are used whenever no local image exists.

The tracked generic backup script is `scripts/backup_db.sh`.

Files named `MODERNIZATION_*`, the constrained release wrapper, and GreenCloud
paths describe the upstream PolyElo deployment. They are maintainer operations,
not prerequisites for an independent installation.
