# Run PolyBot with Docker Compose

PolyBot ships adaptable Compose examples rather than an active deployment
file. Your `compose.yaml`, `.env`, and private application files are ignored by
Git, so an update never overwrites deployment decisions or credentials.

The recommended example works on Docker Desktop and Docker Engine, bundles
PostgreSQL, and uses database-backed guild settings. Normal startup never
changes schema or synchronizes Discord commands.

## Create an installation

```bash
git clone https://github.com/Nelluk/Polytopia-ELO-Bot.git polybot
cd polybot
cp compose.example.yaml compose.yaml
cp .env.example .env
cp config.ini-EXAMPLE config.ini
cp server_settings-EXAMPLE.py server_settings.py
mkdir -p backups
chmod 700 backups
chmod 600 .env config.ini server_settings.py compose.yaml
```

Edit the four private files before starting anything:

- In `.env`, choose `POLYBOT_ENV=development` or `production`, replace both
  database passwords, and choose installation-specific project/image names.
- In `config.ini`, replace the Discord token, expected bot ID, owner ID, and
  optional superuser IDs. Keep optional effects disabled initially.
- In `server_settings.py`, replace `SERVER_GUILD_ID` with the one Discord guild
  used for first bootstrap.
- Adapt `compose.yaml` only when the defaults do not fit the installation.

The recommended Compose deployment treats `.env` as the only authority for
the application database host, port, name, user, and password. `config.ini`
does not duplicate those values.

`development` is the safest initial mode. Its database name must contain
`dev`, `development`, `test`, `testing`, or `sandbox`; known production bot and
guild identities are rejected unless the existing explicit shared-guild
exception is configured.

## Discord application and permissions

Create a Discord application and bot user. Enable the Server Members and
Message Content privileged intents, then invite it to the configured guild
with the `bot` and `applications.commands` OAuth scopes.

For the standard command surface, allow the bot to view channels, send
messages, embed links, attach files, read history, add reactions, manage
messages, manage game channels, and manage assignable roles. Place its role
above roles it must assign. Discord Administrator is not required.

## Build and verify configuration

```bash
docker compose config --quiet
docker compose build bot
docker compose run --rm --no-deps bot python scripts/check_runtime_config.py
```

The check is offline and redacted. Confirm the environment, expected bot,
database, allowed guild, disabled optional effects, and database configuration
source before continuing.

## Initialize PostgreSQL and schema

Start only PostgreSQL:

```bash
docker compose up -d database
docker compose ps
```

Plan schema initialization, then copy the exact printed confirmation into the
apply command:

```bash
docker compose run --rm schema
docker compose run --rm schema --apply --confirm 'COPY PRINTED CONFIRMATION'
```

The plan authenticates with the application credential, verifies the live
database and role, and inspects schema read-only. The apply is the only schema
write. Ordinary bot startup performs no DDL.

## Bootstrap the first guild

The database-authority bot needs one active guild document before it can
start. Capture a read-only Discord snapshot, plan the exact database write,
then apply its printed confirmation. Replace the example ID in all commands:

```bash
docker compose run --rm bot python \
  scripts/bootstrap_first_guild_configuration.py snapshot \
  --guild-id 123456789012345678

docker compose run --rm bot python \
  scripts/bootstrap_first_guild_configuration.py plan \
  --guild-id 123456789012345678

docker compose run --rm bot python \
  scripts/bootstrap_first_guild_configuration.py apply \
  --guild-id 123456789012345678 \
  --confirm 'COPY PRINTED CONFIRMATION'
```

The bootstrap requires a fresh, relation-empty application schema. It creates
the guild-configuration tables and one conservative Standard-guild document:

- ordinary access begins at level 2;
- persistent Teams and global leaderboard participation are off;
- side-size defaults are conservative;
- no optional channel or role references are set; and
- core, `/guild`, `/squad`, and owner-only `/operator` capabilities are ready
  for a later explicit guild command apply.

No Discord commands are synchronized by bootstrap.

Seed the static Polytopia tribe rows, then start the bot:

```bash
docker compose run --rm bot python bot.py --add_default_data --skip_tasks
docker compose up -d bot
docker compose logs --tail 100 bot
```

Verify the authenticated application and a stable restart count. The owner can
then use `/guild settings` to finish ordinary server configuration.

## Synchronize commands explicitly

Startup never changes Discord application commands. Plan and inspect the exact
guild before applying its database-backed capability policy:

```bash
docker compose run --rm bot python \
  scripts/manage_application_commands.py \
  --environment development --mode plan \
  --guild-ids 123456789012345678

docker compose run --rm bot python \
  scripts/manage_application_commands.py \
  --environment development --mode inspect \
  --guild-ids 123456789012345678

docker compose run --rm bot python \
  scripts/manage_application_commands.py \
  --environment development --mode apply \
  --guild-ids 123456789012345678 \
  --confirm-environment development \
  --confirm-guild-ids 123456789012345678 \
  --confirm-scope guild \
  --confirm-no-global-sync
```

Use `production` in all four environment positions for a production profile.
The tool is guild-only and refuses apply while global commands exist.

## Ordinary operation and updates

```bash
docker compose ps
docker compose logs -f bot
docker compose restart bot
docker compose stop
docker compose start
docker compose down
```

`docker compose down` retains named volumes. `docker compose down -v` destroys
the database and other named-volume data and is not an ordinary operation.

For an update:

```bash
git pull --ff-only
docker compose config --quiet
docker compose up -d --build
```

Review release notes first. They identify schema operations, command-tree
changes, and changes to tracked examples. Git does not update the ignored
`compose.yaml`; compare it with `compose.example.yaml` and adopt only relevant
changes.

## Backups and restore

```bash
docker compose run --rm backup
```

The service creates a custom-format archive under `./backups`, validates it,
and publishes a SHA-256 checksum. Copy completed backups and private
configuration off-host with an explicit retention policy.

Never restore over the active database volume. Copy `compose.yaml`, select a
new Compose project/volume, start its database, and run:

```bash
docker compose run --rm restore ARCHIVE.dump
docker compose run --rm schema --verify
```

The restore service requires the adjacent checksum and refuses a database that
already contains public relations.

## Database password rotation

The application password has one authority: `.env`. Changing the file does not
change an already-created PostgreSQL role. For a retained database:

1. Take and verify a backup.
2. Stop the bot.
3. Open the administrator `psql` console in the database container and use
   `\password APPLICATION_ROLE` so the new password is not placed in shell
   history.
4. Update `POLYBOT_DATABASE_PASSWORD` in `.env`.
5. Run `docker compose run --rm schema` to verify authentication read-only.
6. Start and verify the bot.

For a disposable development installation, resetting the database volume and
repeating schema/guild bootstrap is usually simpler.

## External PostgreSQL and private adaptations

`compose.external-postgres.example.yaml` is a smaller alternate example for a
database reachable over TCP. The database and role must already exist; schema,
bot, and command procedures remain the same. Add backup/restore behavior that
matches the external database operator's policy.

A Linux Unix-socket deployment can adapt that example with a read-only socket
mount and blank host/password only for reviewed production peer
authentication. Host-specific credentials, integrations, resource limits and
mounts belong in the ignored `compose.yaml`, not the tracked examples.

## Private files

Do not commit `compose.yaml`, `.env`, `config.ini`, `server_settings.py`,
integration credentials, logs, backups, or uploaded images. The tracked
`.dockerignore` excludes those runtime inputs from image layers.
