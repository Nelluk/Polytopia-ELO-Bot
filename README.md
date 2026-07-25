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

## Runtime image data

Team and house images uploaded as Discord attachments are normalised and stored
under `data/images/`. This directory is intentionally excluded from Git and must
be included in server backups. Direct HTTP(S) image URLs remain stored in
PostgreSQL and are used whenever no local image exists.
