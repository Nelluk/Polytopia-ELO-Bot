# Run PolyBot with Docker Compose

The repository root contains a conventional Compose deployment. Ordinary
operation uses `docker compose` directly; no PolyBot deployment wrapper is
required.

## Bundled PostgreSQL setup

Install Docker Engine or Docker Desktop with the Compose plugin, then clone the
repository and create the private deployment files:

```bash
git clone YOUR_FORK_OR_UPSTREAM_URL polybot
cd polybot
cp .env.example .env
cp config.ini-EXAMPLE config.ini
cp server_settings-EXAMPLE.py server_settings.py
mkdir -p data/images logs backups
chmod 700 data/images logs backups
chmod 600 .env config.ini server_settings.py
```

Edit `.env`, `config.ini`, and `server_settings.py` before starting anything.
In particular:

- replace both passwords in `.env`;
- make `POLYBOT_DATABASE_PASSWORD` match `psql_password` in `config.ini`;
- set `psql_host = database` in `config.ini`;
- replace the Discord token, bot ID, owner ID, guild ID, and channel ID;
- set `POLYBOT_UID` and `POLYBOT_GID` to the owner of `data/images`, `logs`,
  and `backups` on Linux.

The bundled database lives in a pre-created external Docker volume. Create the
exact `POSTGRES_VOLUME_NAME` selected in `.env`; for the example value:

```bash
docker volume create polybot-postgres
```

Compose will refuse to start when that volume is absent. Because the volume is
external, `docker compose down -v` does not delete it. An explicit
`docker volume rm` can still destroy it.

Render and build the deployment, then start only PostgreSQL:

```bash
docker compose config
docker compose build
docker compose up -d database
docker compose ps
```

Initialize or upgrade the application schema explicitly. The first command is
read-only and prints the exact confirmation required by the second:

```bash
docker compose run --rm schema
docker compose run --rm schema --apply --confirm 'COPY THE PRINTED CONFIRMATION'
```

Seed missing Polytopia tribes, then start the bot:

```bash
docker compose run --rm bot python bot.py --add_default_data --skip_tasks
docker compose up -d bot
docker compose logs --tail 100 bot
```

Normal startup never changes schema or synchronizes Discord commands. Follow
the explicit command inspection/apply procedure in
[the self-hosting guide](SELF_HOSTING.md) after the bot identity and guild are
verified.

## Ordinary operation

Run these commands from the deployment directory:

```bash
docker compose ps
docker compose logs -f bot
docker compose restart bot
docker compose stop
docker compose start
docker compose down
```

To deploy source changes from the current checkout:

```bash
git pull --ff-only
docker compose build
docker compose run --rm schema
# Apply the printed schema plan if required.
docker compose up -d --force-recreate bot
```

## Logical backups

Create a custom-format PostgreSQL archive directly through the one-shot
Compose service:

```bash
docker compose run --rm backup
```

The service writes privately under `./backups`, validates the temporary dump
with `pg_restore --list`, computes a SHA-256 checksum, and atomically publishes
the completed archive. A failed dump is not published. PostgreSQL custom dumps
use a transactionally consistent snapshot, so routine database backups do not
require stopping the bot.

Retention, copies to another host or storage provider, and periodic restore
tests remain operator responsibilities. Never back up or migrate PostgreSQL by
copying its live Docker volume.

## Restore into a new volume

Never restore over the active database volume. Create a new external volume
and use a temporary Compose project:

```bash
docker volume create polybot-postgres-restore-YYYYMMDD

COMPOSE_PROJECT_NAME=polybot-restore \
POSTGRES_VOLUME_NAME=polybot-postgres-restore-YYYYMMDD \
docker compose up -d database

COMPOSE_PROJECT_NAME=polybot-restore \
POSTGRES_VOLUME_NAME=polybot-postgres-restore-YYYYMMDD \
docker compose run --rm restore ARCHIVE.dump

COMPOSE_PROJECT_NAME=polybot-restore \
POSTGRES_VOLUME_NAME=polybot-postgres-restore-YYYYMMDD \
docker compose run --rm schema --verify

COMPOSE_PROJECT_NAME=polybot-restore \
POSTGRES_VOLUME_NAME=polybot-postgres-restore-YYYYMMDD \
docker compose down
```

The restore service requires the adjacent `.sha256` file, validates the
archive, and refuses a target with any public relations. After independent
verification, stop the live deployment, change only `POSTGRES_VOLUME_NAME` in
its `.env`, and start it again. Retain the old volume unchanged for rollback.

## Existing PostgreSQL on a Linux host

`compose.host-postgres.yaml` runs only the bot and one-shot schema/backup jobs.
It mounts the configured host PostgreSQL Unix-socket directory read-only and
never creates or owns database storage.

Start from its shorter environment example:

```bash
cp .env.host-postgres.example .env
docker compose -f compose.host-postgres.yaml config
docker compose -f compose.host-postgres.yaml build
docker compose -f compose.host-postgres.yaml run --rm schema
docker compose -f compose.host-postgres.yaml up -d bot
```

The database and role must already exist. Configure `config.ini` with a blank
`psql_host` and password only for reviewed production peer authentication;
otherwise configure the matching supported authentication values. The
container UID must resolve to the host role expected by PostgreSQL peer auth.

Back up that database with:

```bash
docker compose -f compose.host-postgres.yaml run --rm backup
```

Database creation, restore, retention, and upgrades remain responsibilities of
the host PostgreSQL operator.

The upstream GreenCloud production bot has an explicit standalone definition,
`compose.production.yaml`, because it additionally mounts the required Bullet
credential and records exact source/image provenance. It is documented in
[PRODUCTION_DOCKER.md](PRODUCTION_DOCKER.md) and must not be started while the
current `polyelo.service` writer is active.

## Private and persistent files

Do not commit `.env`, private configuration, Discord tokens, passwords, logs,
backups, or uploaded images. Back up:

- logical PostgreSQL archives from `./backups`;
- `./data/images`;
- private `.env`, `config.ini`, and `server_settings.py` files;
- any separately configured integration credentials.

The container publishes no port by default. Enabling the optional HTTP API or
upstream-specific integrations requires a separate configuration and exposure
review.
