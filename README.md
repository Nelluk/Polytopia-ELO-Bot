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

## Documentation map

The complete [documentation map](docs/README.md) separates independent
self-hosting instructions, upstream GreenCloud operations, active engineering
references, and historical migration evidence. Start with the Docker guide;
completed upgrade, modernization, and release-candidate records are not
installation prerequisites.

## Runtime image data

Team and house images uploaded as Discord attachments are normalised and stored
under `data/images/`. This directory is intentionally excluded from Git and must
be included in server backups. Direct HTTP(S) image URLs remain stored in
PostgreSQL and are used whenever no local image exists.

Independent Docker installations use the backup and restore workflow in
[`docs/DOCKER.md`](docs/DOCKER.md). Operators must also back up the host
`data/images/` directory.

GreenCloud-specific documents describe upstream operations, not independent
installation requirements. Completed modernization and release records live at
the Git checkpoint identified in the documentation map rather than in the
current tree.
