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
cp config.development.ini-EXAMPLE deploy/container/config.development.ini
cp server_settings_dev-EXAMPLE.py deploy/container/server_settings_dev.py
chmod 600 .env deploy/container/config.development.ini \
  deploy/container/server_settings_dev.py
```

Edit the two private configuration files for the isolated beta application,
guild, and database. Set these `.env` values explicitly:

- `COMPOSE_PROJECT_NAME`: a unique project such as `polybot-beta`;
- `POLYBOT_RUNTIME_UID` and `POLYBOT_RUNTIME_GID`: the host owner of the
  private configuration files;
- `POLYBOT_BOT_IMAGE`: the image tag for this deployment;
- `POLYBOT_SOURCE_CHECKPOINT`: the full clean `git rev-parse HEAD` value;
- `POSTGRES_SOCKET_DIR`: the host PostgreSQL socket directory.

`COMPOSE_FILE=compose.beta.yaml` is a standard Compose environment setting, so
the ordinary commands below need no file or project-name flags.

## Build and operate

Confirm the checkout is clean and the configured checkpoint equals HEAD:

```bash
git status --short --branch
git rev-parse HEAD
docker compose config --quiet
docker compose build
```

The schema job plans by default and requires its printed confirmation before
writing:

```bash
docker compose run --rm schema
```

Normal lifecycle commands are standard Compose operations:

```bash
docker compose up -d bot
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

Also verify the expected image/checkpoint, application identity, Unix-socket
database transport, restart count, and one project bot with no host or other
container writer. Beta Lab fixture readiness is not a deployment-health
signal. The feature's eventual source and live-fixture retirement is tracked
in [`BETA_ONLY_CLEANUP.md`](BETA_ONLY_CLEANUP.md).

## Updating

After integrating and testing a new clean checkpoint:

1. update `POLYBOT_SOURCE_CHECKPOINT` and `POLYBOT_BOT_IMAGE` in `.env`;
2. run `docker compose build`;
3. run the schema plan and apply it separately if required;
4. run `docker compose up -d --force-recreate bot`;
5. verify the authenticated application, database, checkpoint, logs, and one
   running bot container.

Keep the previous image tag until the replacement is verified so rollback is
an `.env` image/checkpoint change plus container recreation.
