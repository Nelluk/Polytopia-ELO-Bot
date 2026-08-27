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

For staff help or a privacy request, invoke `/staffhelp` with no options. In
production, the bot relays the modal directly to that server's configured
staff-only channel and pings its configured Helper role; it does not write a
local feedback archive. The development beta additionally records its report
in the restricted JSONL beta-feedback store before mirroring it to beta staff.
For a privacy request, use `Privacy request` as the short summary and ask for
private follow-up without posting credentials or unrelated sensitive details.
Do not put personal information in a public GitHub issue.

## Self-hosting

Create an application and bot account in the
[Discord developer portal](https://discord.com/developers/applications). Enable
the Server Members and Message Content privileged intents, then invite the bot
with the `bot` and `applications.commands` scopes.

```
git clone <git repo address>
cd /new/project/path
uv sync --locked --no-dev --python 3.12
```

Create private production configuration from the installation-neutral examples:

```bash
cp config.ini-EXAMPLE config.ini
cp server_settings-EXAMPLE.py server_settings.py
chmod 600 config.ini server_settings.py
```

Replace every token/ID/password placeholder, create the configured PostgreSQL
role and empty database, and validate the redacted profile:

```bash
POLYBOT_ENV=production .venv/bin/python scripts/check_runtime_config.py
POLYBOT_ENV=production .venv/bin/python scripts/manage_schema.py
```

Stop every bot process that could use this database, review the schema plan,
and rerun its printed command with `--apply --confirm '...'`. Then seed the
reference tribes, deploy the configured guild's slash commands, and start:

```
POLYBOT_ENV=production .venv/bin/python bot.py --add_default_data --skip_tasks
POLYBOT_ENV=production .venv/bin/python scripts/manage_application_commands.py \
  --environment production --mode plan
POLYBOT_ENV=production .venv/bin/python bot.py --skip_tasks
```

The command plan is offline. Remote command inspection/apply requires the exact
guild ID and explicit confirmations described in the
[self-hosting guide](docs/SELF_HOSTING.md). Keep `--skip_tasks` during initial
validation; enable background tasks in `config.ini` only when ready.

The complete guide covers PostgreSQL creation, configuration fields, schema
upgrades, slash-command deployment, service operation, backups, and Discord
permissions: **[Self-hosting PolyBot](docs/SELF_HOSTING.md)**.

The repository also includes a container stack, but it is an advanced,
development-only upstream beta environment with fixed safety identities—not a
generic production Compose recipe. Contributors should start with the
[container-stack guide](deploy/container/README.md) and use its `./polybot`
wrapper rather than assembling raw Compose commands.

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
