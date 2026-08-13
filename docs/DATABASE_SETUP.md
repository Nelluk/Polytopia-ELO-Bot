# Database setup for a test bot

PolyBot uses PostgreSQL through Peewee. For a test server, give the bot its
own PostgreSQL role and an empty database. Do not point a test bot at an
existing or production database: importing the bot's model module creates
missing tables, and normal bot commands write data.

The examples below use the names expected by the repository's database
integration tests:

- PostgreSQL role: `polybot_dev`
- PostgreSQL database: `polytopia_dev`
- PostgreSQL host and port: `localhost:5432`

## 1. Install and start PostgreSQL

On Debian or Ubuntu:

```bash
sudo apt update
sudo apt install postgresql
sudo systemctl enable --now postgresql
```

If PostgreSQL is already installed, confirm that it is running:

```bash
systemctl status postgresql --no-pager
```

Package installation and service management require administrator access.

## 2. Create an isolated role and database

Open the PostgreSQL administration prompt:

```bash
sudo -u postgres psql
```

At the `postgres=#` prompt, run:

```sql
CREATE ROLE polybot_dev LOGIN;
\password polybot_dev
CREATE DATABASE polytopia_dev OWNER polybot_dev;
\q
```

The `\password` command prompts for the password without putting it in shell or
SQL history. Save that password somewhere secure; it goes in the untracked
development config file in the next step.

Test the login before configuring the bot:

```bash
psql -h localhost -p 5432 -U polybot_dev -d polytopia_dev
```

Enter the password when prompted, then confirm the session is using the
intended database and role:

```sql
SELECT current_database(), current_user;
\q
```

The result should be `polytopia_dev` and `polybot_dev`.

## 3. Configure the bot

From the repository root, create the development configuration files:

```bash
cp config.development.ini-EXAMPLE config.development.ini
cp server_settings_dev-EXAMPLE.py server_settings_dev.py
```

In `config.development.ini`, set:

```ini
psql_db = polytopia_dev
psql_user = polybot_dev
psql_password = THE_PASSWORD_SET_ABOVE
psql_host = localhost
psql_port = 5432
```

Also replace the Discord token, bot ID, guild IDs, and all other placeholders
in both files. The three `production_*` settings are a safety denylist, not a
second database connection. They must identify the upstream production bot's
database, bot, and guilds so the development profile can reject accidental
overlap. If you do not know the current values, ask the bot maintainer rather
than inventing them.

Keep these development settings disabled initially:

```ini
background_tasks_enabled = false
allow_development_background_tasks = false
api_enabled = false
allow_development_api = false
```

Both development config files are ignored by Git. Do not commit database
passwords or Discord tokens.

## 4. Validate the configuration

Install the locked dependencies if needed, then run the safe configuration
check:

```bash
uv sync --locked --python 3.12
POLYBOT_ENV=development .venv/bin/python scripts/check_runtime_config.py
```

This prints a redacted summary and does not import the models or connect to the
database. Check that it reports the development environment,
`polytopia_dev`, `polybot_dev`, and disabled background tasks/API.

## 5. Create the tables and seed tribes

Normal bot startup and model import are schema-read-only. With the
configuration check passing, first print the explicit development bootstrap
plan (this does not connect):

```bash
POLYBOT_ENV=development .venv/bin/python scripts/bootstrap_development_database.py
```

Review the database, role, bounded DDL description, and exact confirmation
token printed by that plan. Then explicitly apply it, substituting the exact
token the plan printed:

```bash
POLYBOT_ENV=development .venv/bin/python scripts/bootstrap_development_database.py \
  --apply \
  --confirm 'BOOTSTRAP DEVELOPMENT DATABASE polytopia_dev AS polybot_dev'
```

The apply path is development-only, verifies the live database and role before
DDL, retains the fixed PostgreSQL advisory lock across the fresh-database
exception, and creates only missing model tables, the
`development_writer_fence` authority row, and the deferred
`game.winner_id -> gameside.id` foreign key in one transaction. It then verifies
the startup schema through a new read-only connection. On a nonfresh database,
the writer fence must already exist and be acquired; use the explicitly gated
existing-database flow in `docs/CONTAINERIZED_DEVELOPMENT.md` instead of the
fresh-schema exception.

After the schema bootstrap succeeds, seed the permanent tribe reference data:

```bash
POLYBOT_ENV=development .venv/bin/python bot.py --add_default_data --skip_tasks
```

The `--add_default_data` option adds the Polytopia tribes; it is safe to run
again because existing tribes are skipped. It acquires the development
database writer fence before mutation and does not own schema creation.
An ordinary bot start first performs a model-free read-only schema preflight
and fails closed if the required tables or winner foreign key are missing.

Verify that tables now exist:

```bash
psql -h localhost -p 5432 -U polybot_dev -d polytopia_dev -c '\dt'
```

You can then start the test bot with background tasks suppressed:

```bash
POLYBOT_ENV=development .venv/bin/python bot.py --skip_tasks
```

## Common errors

- `password authentication failed`: re-run `sudo -u postgres psql`, use
  `\password polybot_dev`, and make the config password match.
- `connection refused`: PostgreSQL is stopped, is listening on a different
  port, or `psql_host`/`psql_port` is wrong.
- `database "polytopia_dev" does not exist`: create it with the exact name in
  step 2, or make `psql_db` match the name you chose.
- `permission denied for schema public`: make the development role the database
  owner. In `psql` as an administrator, check with `\l polytopia_dev`.
- `Development database name must include ...`: development database names
  must contain a distinct `dev`, `test`, `testing`, `development`, or `sandbox`
  segment, such as `polytopia_dev`.
- A runtime configuration error mentioning production overlap means a test
  database, Discord bot, or guild matches the safety denylist. Use isolated
  resources; do not bypass the check.
