# Upstream development beta with Docker Compose

This is the maintainer deployment for the upstream PolyBot beta. Independent
self-hosters should use the default [Docker Compose guide](DOCKER.md).

The beta is one ordinary Compose project in one checkout. It uses the host's
existing PostgreSQL server through a read-only Unix-socket mount and never
owns database storage.

## Configure the checkout

Run from the beta checkout:

```bash
cp .env.beta.example .env
cp config.development.ini-EXAMPLE config.development.ini
cp server_settings_dev-EXAMPLE.py server_settings_dev.py
chmod 600 .env config.development.ini server_settings_dev.py
```

Edit the two private configuration files for the isolated beta application,
guild, and database. Set these `.env` values explicitly:

- `COMPOSE_PROJECT_NAME`: a unique project such as `polybot-beta`;
- `POLYBOT_RUNTIME_UID` and `POLYBOT_RUNTIME_GID`: the host owner of the
  private configuration files;
- `POLYBOT_BOT_IMAGE`: a stable local image name such as
  `polybot-beta:local`; it does not change for each source update;
- `POSTGRES_SOCKET_DIR`: the host PostgreSQL socket directory.

`COMPOSE_FILE=compose.beta.yaml` is a standard Compose environment setting, so
the ordinary commands below need no file or project-name flags.

## Build and operate

Confirm that Compose can render the configuration, then build and start the
bot from the current checkout:

```bash
docker compose config --quiet
docker compose up -d --build
```

The schema job plans by default and requires its printed confirmation before
writing:

```bash
docker compose run --rm schema
```

Normal lifecycle commands are standard Compose operations:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 bot
docker compose restart bot
docker compose stop bot
docker compose down
```

Normal startup never changes schema or synchronizes Discord commands. The bot
launcher retains the beta identity checks and database-wide single-writer
lock. No host port is published.

## Runtime verification

Verify the effective runtime profile inside the authenticated bot container:

```bash
docker compose exec bot python scripts/check_runtime_config.py
```

Also verify the application identity, Unix-socket database transport, restart
count, and one project bot with no host or other container writer. Docker's
image ID (`docker compose images`) and the checkout's Git history provide
source diagnostics without duplicate version settings. The retired Beta Lab
and legacy deployment cleanup is
recorded in [`BETA_ONLY_CLEANUP.md`](BETA_ONLY_CLEANUP.md).

## Updating

For an ordinary source-only update:

```bash
git pull --ff-only
docker compose up -d --build
```

If release notes identify a schema change, run the schema plan and separately
apply its printed confirmation before starting code that requires it. Verify
the authenticated application, database, logs, restart count, and one-writer
boundary after updating. Roll back source with Git and rebuild the same stable
local image name; no `.env` version fields need to change.
