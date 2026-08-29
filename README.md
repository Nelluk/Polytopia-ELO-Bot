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
- your Discord user ID and server ID;
- two new database passwords; and
- the numeric host UID/GID that will own persistent files on Linux.

The guide walks through the remaining schema, first-guild, seed-data, slash-command,
permission, startup, and backup steps. Normal startup does not silently change
the database schema or synchronize Discord commands.

## Policies and support

- [Privacy Policy](PRIVACY.md)
- [Security Policy](SECURITY.md)
- [Data Retention Schedule](docs/DATA_RETENTION.md)

Users of the official upstream PolyELO deployment can invoke `/staffhelp` with
no options for staff support or a privacy request. Do not put personal data,
credentials, or vulnerability details in a public GitHub issue.

The policies above describe the official upstream PolyELO deployment.
Independent operators are responsible for publishing accurate policies and
support contacts for their own instance. `/staffhelp` is intentionally disabled
in the installation-neutral example until its private delivery routes are
configured; see the Docker guide before enabling `tools_support`.

Upstream maintainers run the isolated beta through the separate, direct
[development Compose interface](docs/DEVELOPMENT_DOCKER.md). It uses the same
root Dockerfile as the public stack and ordinary Compose commands.

The active upstream production Compose deployment is documented separately in
[docs/PRODUCTION_DOCKER.md](docs/PRODUCTION_DOCKER.md). It contains GreenCloud-
specific paths and credentials integration and is not the default self-hosting
path.

For an isolated test bot, use a separate Discord application, guild, Compose
project, and PostgreSQL volume. Never point a test process at production data.

## Documentation map

The [documentation map](docs/README.md) separates public installation from
upstream GreenCloud and development operations. Start with the Docker guide;
completed upgrade and application-review records are retained in Git history,
not as current procedures.

## Runtime image data

Team and house images uploaded as Discord attachments are normalised and stored
under the configured image root. This data is intentionally excluded from Git
and must be included in deployment backups. Direct HTTP(S) image URLs remain stored in
PostgreSQL and are used whenever no local image exists.

Independent Docker installations use the backup and restore workflow in
[`docs/DOCKER.md`](docs/DOCKER.md). Operators must also preserve the configured
image volume.

GreenCloud-specific documents describe upstream operations, not independent
installation requirements. Completed modernization and release records live at
the Git checkpoint identified in the documentation map rather than in the
current tree.
