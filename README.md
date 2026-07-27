# Polytopia-ELO-Bot
A discord bot for the game Polytopia, to enable matchmaking and leaderboards.
Requires Python 3.6+, Postgres, probably will only install properly on a Unix-like OS.

## Policies and support

- [Privacy Policy](PRIVACY.md)
- [Security Policy](SECURITY.md)
- [Data Retention Schedule](docs/DATA_RETENTION.md)
- [Privacy Readiness Checklist](docs/PRIVACY_READINESS_CHECKLIST.md)

For a privacy request, use the bot's configured command prefix followed by
`staffhelp Privacy request - please contact me about my PolyELO data` in a
Discord server where PolyELO operates. Do not post Discord IDs, credentials, or
other sensitive details in a public GitHub issue.

Create an application and application bot account at the Discord developer portal: https://discord.com/developers/applications

```
git clone <git repo address>`
python3 -m venv /new/project/path
cd /new project/path
source bin/activate
pip install -r requirements.txt
```

Make a copy of config.ini and server_settings.py using the example template files.

Change the required settings inside config.ini, which include the API key from the developer portal above.

Create an empty postgresql database and add the database's name and a psql user name into config.ini

Run bot.py 

## Dependency upgrade safety checks

Run the offline compatibility suite without connecting to Discord or
PostgreSQL:

```
bin/python -W error::DeprecationWarning -m unittest discover -s tests -v
```

Capture the active interpreter and all installed distribution versions without
depending on `pip`:

```
bin/python scripts/dependency_inventory.py
bin/python scripts/dependency_inventory.py --json
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
