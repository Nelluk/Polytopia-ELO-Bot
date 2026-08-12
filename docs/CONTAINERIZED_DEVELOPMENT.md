# Containerized development feasibility and proof

Status: development-only static proof. This is not a production deployment
runbook and does not replace either existing systemd service.

## Verdict

A two-service Compose deployment is practical for PolyBot. The reviewed shape
keeps the bot image replaceable, gives PostgreSQL its own pinned-major service
and persistent volume, and removes host PostgreSQL role/database setup from the
ordinary development path. The repository now contains a build definition,
bundled-database Compose definition, external-database variant, explicit
database provisioner, plan-first backup and isolated restore-drill jobs, and a
machine-readable contract.

This checkpoint is deliberately not a claim that an image has been built,
that a recovery job has run, or that Discord has been reached from a
container. At the P11.2/P11.3 offline checks, the development host exposed no
Docker or Podman executable. The proof is therefore limited to offline
contract, shell-syntax, lockfile, and application tests. A supported container
deployment still needs a later live-engine unit to build, inspect, start, and
smoke the exact images and recovery path.

## Reviewed architecture

- `bot` is built from CPython 3.12.13 and the locked `uv` 0.11.32 environment.
  It runs as UID/GID 10001, with a read-only root filesystem, all Linux
  capabilities dropped, a bounded tmpfs, resource ceilings, and persistent
  image and log volumes.
- `postgres` uses PostgreSQL 18.4, matching the development server major
  verified on 2026-08-11. Its data is isolated in `postgres_data`; the bot
  never shares that filesystem.
- PostgreSQL first starts with only its administrative maintenance database.
  The explicit `database-provision` job creates the non-superuser
  `polybot_dev` role and its `polytopia_dev` database, or verifies their safe
  existing shape. It refuses another environment, host, database, role, or
  PostgreSQL major.
- The separate `schema` job plans by default. Application tables are written
  only when an operator reruns it with the exact confirmation emitted by that
  plan. Normal database or bot startup never creates application schema,
  seeds fixtures, restores data, or changes Discord commands.
- The bundled bot waits for PostgreSQL's `pg_isready` health check. Discord
  readiness is not reduced to a misleading local HTTP probe: container
  running state and logs prove process liveness, while the authenticated bot
  identity and ready log line remain the actual gateway-readiness evidence.
- The external-database Compose file omits PostgreSQL and provisioning. Its
  mounted config must name a separately managed development host.
- `database-backup` writes only to the ignored bind-mounted `backups`
  directory. `database-restore-drill` can connect only to `restore-postgres`,
  whose `postgres_restore_data` volume is separate from the ordinary database
  volume. Neither job is part of normal startup.

The immutable contract is
`deploy/container/container-contract.toml`. Version changes to its images,
identity, persistence, or startup-effect policy require review together with
the Compose and Dockerfile changes.

## Configuration and secret boundary

The container image excludes ignored runtime configs, mutable logs/images,
secrets, and Git metadata. It retains tracked tests, documentation, Beta Lab
manifests, and rendering assets so the exact built image can run its offline
suite and serve the same development features. It receives two ignored
container-specific config files as read-only mounts:

- `deploy/container/config.development.ini`
- `deploy/container/server_settings_dev.py`

Keeping container copies avoids changing the host beta's active ignored
configuration. Start from the repository-root development examples or the
already reviewed host development files. For the bundled database, set
`psql_host = postgres`, `psql_db = polytopia_dev`, and
`psql_user = polybot_dev`. Preserve all existing production denylists,
development Discord identity checks, database guild authority, and disabled
task/API/Bullet flags.

The bundled stack also requires these ignored, mode-0600, one-line files:

- `deploy/container/secrets/postgres-admin-password.txt`
- `deploy/container/secrets/polybot-database-password.txt`

The application-password file must equal `psql_password` in the mounted
container config. This duplication is a known compatibility boundary: the
current runtime config reads INI values, not Docker `_FILE` variables. P11.2's
doctor compares the values without displaying them. Changing runtime secret
loading is optional later work, not hidden inside this deployment proof.

## Intended bundled-database flow

Run from the repository root. These are the eventual commands for a host with
Docker Compose; they have not been executed on the current host.

```bash
cp config.development.ini-EXAMPLE deploy/container/config.development.ini
cp server_settings_dev-EXAMPLE.py deploy/container/server_settings_dev.py
cp deploy/container/development.env.example deploy/container/.env
mkdir -p deploy/container/secrets deploy/container/backups
chmod 700 deploy/container/secrets deploy/container/backups
# Fill both config files and the two secret files, set the .env checkpoint to
# the exact clean Git HEAD, set guild_configuration_source=database and
# psql_host=postgres, then chmod configs and secrets 600.
chmod 600 deploy/container/config.development.ini deploy/container/server_settings_dev.py
chmod 600 deploy/container/secrets/postgres-admin-password.txt deploy/container/secrets/polybot-database-password.txt

python scripts/check_container_deployment.py --mode bundled
docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml --profile tools config
docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml build bot
docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml up -d postgres
docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml --profile tools run --rm database-provision
docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml --profile tools run --rm schema
# Review the schema plan, then repeat with: --apply --confirm <exact-token>
docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml up -d bot
docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml logs --tail 100 bot
```

`check_container_deployment.py` is the repository-owned preflight. It requires
a clean exact Git checkpoint; validates the contract against the selected
Compose/Dockerfile assets; parses but does not execute server settings; checks
the container-only config, production denylists, disabled development effects,
secret file type/mode/shape, password agreement, and exact image/checkpoint
pins; and reports Docker/standalone-Compose executables found on `PATH`. It
does not run either executable, connect to PostgreSQL or Discord, create files,
or change permissions. A ready report therefore means inputs are ready for the
printed `docker compose ... config` command; that explicit command is still
the first proof of plugin availability and fully rendered Compose syntax.

Use `--mode external` with the external Compose file and a non-loopback,
non-`postgres` database host. That mode does not require or inspect the bundled
PostgreSQL secrets. `--json` emits the same findings and commands without
including token or password values. Any `BLOCK` finding exits 2 while still
printing the ordered commands; warnings do not prevent readiness.

The first bot start remains intentionally later than database provisioning and
explicit schema apply. Fixture operations remain separate and must retain the
existing single-writer rule: stop `bot`, prove the writer absent, use the
guarded fixture tool in a one-shot bot container, then start `bot` again.
Application command inspection/apply likewise remains explicit, development-
guild-only, and separately reviewed. It is never part of `compose up`.

For an external database, use
`deploy/container/compose.development.external-db.yaml`, set the mounted
config's `psql_host` to the real development endpoint, and omit the bundled
provisioning step. The database must already satisfy the same application-role
and schema gates.

## Backup and fresh-volume restore flow

This flow is for the bundled development database only. An external database
operator must use that provider's independently reviewed logical recovery
procedure; this Compose project does not acquire external administrative
credentials.

Set `POLYBOT_RECOVERY_UID` and `POLYBOT_RECOVERY_GID` in the ignored `.env` to
the positive host UID/GID that owns `deploy/container/backups`. Recovery jobs
run with that identity, a read-only root filesystem, dropped capabilities, and
no Docker socket. Use this base command below, or paste the full equivalent:

```bash
COMPOSE='docker compose --env-file deploy/container/.env --file deploy/container/compose.development.yaml'
```

1. Stop only the containerized bot and prove it is stopped. Do not stop the
   host beta unless it actually uses this same container database.

   ```bash
   $COMPOSE stop bot
   $COMPOSE ps bot postgres
   ```

2. Preview the backup without starting a dependency. This reads no secret,
   opens no database connection, and writes no file. Copy its exact
   confirmation.

   ```bash
   $COMPOSE --profile recovery run --rm --no-deps database-backup
   ```

3. Apply that exact plan. The job requires PostgreSQL 18, the fixed
   `postgres`/`polytopia_dev`/`polybot_dev` identities, enough free destination
   space for the current uncompressed database size plus 64 MiB, and zero
   other source-database sessions before and after `pg_dump`. It validates a
   private temporary custom archive with `pg_restore --list`, computes its
   SHA-256, then atomically publishes the archive and digest sidecar.

   ```bash
   $COMPOSE --profile recovery run --rm \
     -e 'POLYBOT_BACKUP_CONFIRMATION=BACKUP polytopia_dev EXACT_40_HEX_CHECKPOINT' \
     database-backup
   ```

   A role session appearing during the dump prevents publication. A
   `.polybot-backup.lock` left by an abrupt container kill must be investigated
   before it is manually removed.

4. Restart and recheck the bot if it was previously running. Backup completion
   is not a restore proof.

   ```bash
   $COMPOSE up -d bot
   $COMPOSE logs --tail 100 bot
   ```

5. Select the exact archive basename from `deploy/container/backups`, then
   validate and preview it without starting the recovery database. The plan
   recomputes the digest, requires an exact sidecar, and checks the archive
   catalog locally.

   ```bash
   $COMPOSE --profile recovery run --rm --no-deps \
     -e 'POLYBOT_BACKUP_ARCHIVE=EXACT_ARCHIVE_BASENAME.dump' \
     database-restore-drill
   ```

6. Start only the isolated recovery PostgreSQL service and apply the exact
   digest-bound confirmation from the plan.

   ```bash
   $COMPOSE --profile recovery up -d restore-postgres
   $COMPOSE --profile recovery run --rm \
     -e 'POLYBOT_BACKUP_ARCHIVE=EXACT_ARCHIVE_BASENAME.dump' \
     -e 'POLYBOT_RESTORE_CONFIRMATION=RESTORE polytopia_restore_verify EXACT_SHA256' \
     database-restore-drill
   ```

   Apply refuses a recovery volume that already contains the application role
   or a non-default database. It creates only `polybot_dev` and
   `polytopia_restore_verify`, restores in one transaction as the application
   role, then verifies all required application tables, the
   `game.winner_id -> gameside.id` foreign key, and table/sequence ownership.
   The normal `postgres_data` volume and `polytopia_dev` database are never
   restore targets.

7. Retain the recovery volume for inspection. Before another drill, stop and
   remove only `restore-postgres`, then explicitly remove only
   `polybot-development_postgres_restore_data`. That final volume removal is a
   destructive operator action and is intentionally never automatic.

The repository does not automatically delete backups. Keep at least two
checksum-paired archives, keep one copy outside this checkout and host, and
retain the newest archive until a newer one passes this fresh-volume restore
drill. Delete an older pair only after reviewing the exact filenames, free
space, surviving verified generations, and off-host copy. This conservative
manual policy avoids a retention bug silently erasing the only recoverable
generation.

## Restart and shutdown behavior

Docker sends `SIGINT` and allows 45 seconds, matching the reviewed bot cleanup
path. The bot service uses `restart: on-failure:5`; therefore the existing
owner-confirmed `/operator bot restart` exit status 75 is treated as a
supervised restart. The image embeds the exact clean source checkpoint as an
OCI label and environment value, so the command does not need `.git` inside
the container and refuses an absent or malformed checkpoint. Its panel says
that Compose will restart the same reviewed immutable image. It does **not**
claim that a restart deploys newly committed source; that requires an explicit
image build and container recreation. A clean operator stop is not restarted.
PostgreSQL has its own 60-second grace period and `unless-stopped` policy.

Compose does not replace the application's single-writer discipline. Only one
bot replica is supported. Do not use `--scale bot` or run a host beta against
the same database and Discord token. Container adoption needs explicit writer
inventory that covers both host and container processes.

## Persistence, backup, restore, and upgrades

`postgres_data`, `polybot_images`, and `polybot_logs` persist independently of
the replaceable bot container. Beta operation state remains under the log
volume because that is its existing owned root. Backups must be logical,
verified PostgreSQL archives written to an operator-controlled off-volume
destination such as `deploy/container/backups`; copying a live volume is not a
backup or host-move procedure.

The recovery profile now provides digest-bound `pg_dump` and
`pg_restore --list` jobs, stopped-writer checks, free-space reporting, and a
fresh-volume restore drill. Recovery artifacts remain outside the database
volume. PostgreSQL major upgrades require a separate dump/restore or
`pg_upgrade` plan. Never point a new major image at the existing volume and
never use `latest`.

## Remaining gates before supported use

1. Resolve the exact Python, uv, and PostgreSQL image digests from their
   official registries and review their provenance. Version tags are pinned in
   this offline proof, but tags are not immutable digests.
2. Build with network access, review image history/packages and vulnerability
   results, then run the full offline suite inside the built bot image.
3. Exercise fresh-volume provision, repeated provision, schema plan/apply,
   volume persistence, resource ceilings, SIGINT, exit-75 restart, database
   unavailability/recovery, and an actual development Discord login.
4. Execute the logical backup/restore drill and cross-runtime single-writer
   audit with the exact built images.
5. Only after those development results, design a separately approved
   production migration, secret/volume ownership, rollback, and monitoring
   plan. No production database, service, or command registration is
   authorized by this document.
