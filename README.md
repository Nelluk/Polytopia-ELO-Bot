# Polytopia-ELO-Bot
A discord bot for the game Polytopia, to enable matchmaking and leaderboards.
Requires Python 3.6+, Postgres, probably will only install properly on a Unix-like OS.

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
