# Containerized development feasibility and proof

Status: development-only live-engine infrastructure proof. This is not a
supported long-running deployment or production runbook.
It does not replace either existing systemd service.

## Verdict

A two-service Compose deployment is practical for PolyBot. The reviewed shape
keeps the bot image replaceable, gives PostgreSQL its own pinned-major service
and persistent volume, and removes host PostgreSQL role/database setup from the
ordinary development path. The repository now contains a build definition,
bundled-database Compose definition, external-database variant, explicit
database provisioner, plan-first backup, isolated restore-drill, and exact
ordinary-development import jobs, plus a machine-readable contract.

P11.4A exercised the infrastructure path with Docker 29.7.2 and Compose 5.4.0.
It resolved and committed immutable registry digests, built and inspected an
exact-checkpoint bot image, ran the complete offline suite inside that image,
provisioned a new bundled PostgreSQL 18 cluster twice, applied the plan-first
schema bootstrap, and completed the digest-confirmed backup/fresh-volume
restore drill. It did not start the bot, contact Discord, touch the host
development database, or establish a supported service deployment.

The host had only about 2.3 GB free. Docker published the 1.44 GB on-disk bot
image, but Compose then exited nonzero because its small client-side build
metadata file could not be written at peak usage. The image itself was
inspectable and passed all runtime checks after the unit-only build cache was
removed. This is valid feasibility evidence, but the deployment flow needs
more disk headroom (or a remote build/registry workflow) before it is
operationally clean on this host.

## Reviewed architecture

- `bot` is built from CPython 3.12.13 and the locked `uv` 0.11.32 environment.
  The ignored `.env` supplies a positive non-root UID/GID; the same identity
  owns copied application source in the image. On Linux that numeric identity
  must exactly own both mode-0600 private configuration mounts. On Docker
  Desktop for macOS, the mode-0600 files instead remain owned by the invoking
  host user and Docker Desktop presents the bind to the configured container
  identity. The doctor applies only that narrower Darwin host-owner rule as a
  warning, requires a live Docker Desktop bind probe before bot startup, and
  keeps the Linux exact-owner pass/block unchanged. Runtime uses a read-only
  root filesystem, all Linux capabilities dropped, a bounded tmpfs, resource
  ceilings, and persistent image and log volumes.
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
- `database-import` can connect only to the ordinary bundled `postgres`
  service and fixed `polytopia_dev` target. It accepts only the transferred
  archive and digest recorded in contract version 4, requires a safely
  provisioned relation-empty target plus zero target sessions, restores once
  as `polybot_dev`, and verifies the digest-bound bounded data counts. It never
  starts the bot and is not part of normal startup.

The immutable contract is
`deploy/container/container-contract.toml`. Version changes to its images,
identity, persistence, or startup-effect policy require review together with
the Compose and Dockerfile changes.

## Configuration and secret boundary

The container image excludes the ignored Compose environment, runtime configs,
mutable logs/images, secrets, and Git metadata. It retains tracked tests,
documentation, Beta Lab manifests, and rendering assets so the exact built
image can run its offline suite and serve the same development features. It
receives two ignored container-specific config files as read-only mounts:

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

Run from the repository root. P11.4A exercised these infrastructure commands
under the uniquely scoped `polybot-p11-4a` project. Ordinary use should choose
one reviewed project name and first satisfy the remaining operational gates.

```bash
cp config.development.ini-EXAMPLE deploy/container/config.development.ini
cp server_settings_dev-EXAMPLE.py deploy/container/server_settings_dev.py
cp deploy/container/development.env.example deploy/container/.env
mkdir -p deploy/container/secrets deploy/container/backups
chmod 700 deploy/container/secrets deploy/container/backups
# Fill both config files and the two secret files, set the .env checkpoint to
# the exact clean Git HEAD, set guild_configuration_source=database and
# psql_host=postgres, set POLYBOT_RUNTIME_UID/GID to the owner of both config
# files, then chmod configs and secrets 600.
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
pins; validates that the configured non-root runtime identity owns both
private bot mounts; and reports Docker/standalone-Compose executables found on
`PATH`. It does not run either executable, connect to PostgreSQL or Discord,
create files, or change permissions. A ready report therefore means inputs are
ready for the printed `docker compose ... config` command; that explicit
command is still the first proof of plugin availability and fully rendered
Compose syntax.

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

### Importing the stopped host development database

P11.4B1 adds a separate source-side export for rehearsing a realistic move
from the existing host `polytopia_dev` database. Run it only from the exact
clean primary development checkout, never from the production checkout. The
plan is connection-free and prints the checkpoint-bound confirmation:

```bash
POLYBOT_ENV=development \
  .venv/bin/python scripts/export_host_development_database.py
```

Stop only the durable development beta and prove the host-wide development
writer audit is clear. Then repeat the command with its exact confirmation:

```bash
POLYBOT_ENV=development \
  .venv/bin/python scripts/export_host_development_database.py \
  --confirm 'EXPORT polytopia_dev EXACT_40_HEX_CHECKPOINT'
```

The apply path requires the fixed local `polytopia_dev`/`polybot_dev`
identity, PostgreSQL 18, disabled development integrations, the development
guild allowlist, a clean exact checkout, and the durable beta writer lock for
the complete database operation. It checks destination headroom, samples zero
other sessions before and after `pg_dump`, validates the private temporary
archive with `pg_restore --list`, and atomically publishes the archive plus
its exact digest under ignored `deploy/container/backups`.

This is logical-backup evidence, not continuous session monitoring. A
short-lived uncoordinated client could connect and disconnect between the two
observations. The actual writer exclusion comes from stopping the owned beta,
holding its writer lock, and auditing host processes; the samples are useful
additional observations. Keep the beta stopped while performing the fresh-
volume restore and comparing bounded source/restore table counts.

To rehearse on another machine, copy the archive and `.sha256` pair through a
private channel into that checkout's ignored `deploy/container/backups`, then
follow the fresh-volume restore steps below. The target remains the fixed
isolated `polytopia_restore_verify` database and exposes no host port. Its
administrative and application passwords are new recovery-cluster secrets;
they do not need to equal the source server's credentials. Never point this
flow at an existing database or use it as authority to change production.

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
   other source-database sessions when sampled immediately before and after
   `pg_dump`. It validates a
   private temporary custom archive with `pg_restore --list`, computes its
   SHA-256, then atomically publishes the archive and digest sidecar.

   ```bash
   $COMPOSE --profile recovery run --rm \
     -e 'POLYBOT_BACKUP_CONFIRMATION=BACKUP polytopia_dev EXACT_40_HEX_CHECKPOINT' \
     database-backup
   ```

   A session present at either observation prevents publication. These two
   samples do not prove that no short-lived session connected and disconnected
   between them; the operational guarantee is therefore the stopped-writer
   procedure, not continuous database-side session monitoring. A
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

### Importing the reviewed transfer into the ordinary bundled database

This is a separate one-time development import, not a general restore path.
It accepts only
`polybot-polytopia_dev-20260812T123355Z-d27d6c83508ad00ef4e28d4eabad5fcddcf3189f.dump`
with SHA-256
`a1ab30a068a068da6ce207d41d8b840a31291d721b49ee4e1d7a9c464958aa8b`.
The archive and `.sha256` sidecar remain read-only inputs and must be retained.
On the Mac transfer rehearsal, always retain the explicit project name:

```bash
COMPOSE='docker compose --project-name polybot-mac-beta --env-file deploy/container/.env --file deploy/container/compose.development.yaml'
ARCHIVE='polybot-polytopia_dev-20260812T123355Z-d27d6c83508ad00ef4e28d4eabad5fcddcf3189f.dump'
```

1. Stop the owned container bot and inspect the owned project. A bot container
   must not be running, and import also refuses any sampled target-database
   session.

   ```bash
   $COMPOSE stop bot
   $COMPOSE ps bot postgres
   ```

2. Validate and print the exact plan without starting PostgreSQL. This reads
   only the archive pair and catalog; it does not read a secret, connect to
   PostgreSQL, or write a file.

   ```bash
   $COMPOSE --profile recovery run --rm --no-deps \
     -e "POLYBOT_BACKUP_ARCHIVE=$ARCHIVE" \
     database-import
   ```

3. Start a fresh ordinary PostgreSQL volume and run only the idempotent
   provisioner. Do not run the schema job: the import requires an existing
   restricted `polybot_dev` role and owned `polytopia_dev` database with zero
   public relations.

   ```bash
   $COMPOSE up -d postgres
   $COMPOSE --profile tools run --rm database-provision
   ```

4. Apply the exact digest-bound confirmation emitted by the plan.

   ```bash
   $COMPOSE --profile recovery run --rm \
     -e "POLYBOT_BACKUP_ARCHIVE=$ARCHIVE" \
     -e 'POLYBOT_IMPORT_CONFIRMATION=IMPORT polytopia_dev a1ab30a068a068da6ce207d41d8b840a31291d721b49ee4e1d7a9c464958aa8b' \
     database-import
   ```

   The job rechecks PostgreSQL major 18, `postgres:postgres` maintenance
   identity, the restricted application role, target ownership, no target
   sessions, and a relation-empty public schema before `pg_restore`. Restore
   uses one transaction with `--no-owner --no-acl`. Post-restore checks require
   all application tables, `game.winner_id -> gameside.id`, application-owned
   tables/sequences, 71 guild games, 4 Houses, 44 guild Players, 15 guild
   Teams, result fixtures 2286–2288, 48 showcase games, and 24 showcase
   Players.

5. Repeat the apply command once. It must refuse the now non-fresh target
   before invoking `pg_restore`. Keep the bot stopped for the separately gated
   single-writer and lifecycle proof.

The bundled service publishes no host port. This procedure cannot address an
external database or any production database and contains no create/drop
database operation.

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

## P11.4A evidence

- Registry digests: Python
  `sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`,
  uv `sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c`,
  and PostgreSQL
  `sha256:882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382`.
- Exact source/image checkpoint `40534f5ccbf9442b9af30861493c53e3e47ed38b`;
  local image ID
  `sha256:8b18ec562f8d273fdd4c43548d1b903b8fe51779e287c977ad09bbc1f0751d3a`.
  Inspection verified non-root `1000:1000`, `/app`, the exact OCI revision,
  and no baked private runtime input.
- The no-network, read-only-root, capability-dropped image ran all 2,024
  offline tests with zero failures/errors and 96 skips. Seven extra skips are
  host Git-dependent production-backup boundary cases; they still run and pass
  in the complete host suite. The minimal runtime image intentionally does not
  contain Git.
- Fresh and repeated provisioning succeeded; exact schema confirmation
  produced 17 tables and the winner foreign key. A 57,012-byte custom archive
  with SHA-256
  `bdff2cd533db5140c274aa2e45b72d07cae8b96489fe2cc50100297d5f00054c`
  restored into the second fresh volume and passed required-table, winner-FK,
  and ownership checks. Repeating the restore refused the non-fresh target.
- Both database services were healthy with only internal `5432/tcp`; no host
  port was published. All `polybot-p11-4a` containers, network, four volumes,
  images, cache, and synthetic backup pair were then removed. The pre-existing
  `hello-world` resource remained untouched.

## P11.4B2 Mac import evidence

- The local host was arm64 macOS 26.6.1 with Docker Desktop 4.85.0,
  Engine/Client 29.6.2, Compose 5.3.1, and about 516 GiB available. The pinned
  Python, uv, and PostgreSQL OCI indexes contain both linux/amd64 and
  linux/arm64 manifests; this host resolved native arm64 images.
- The retained 85,932-byte archive and strict sidecar both verify as
  `a1ab30a068a068da6ce207d41d8b840a31291d721b49ee4e1d7a9c464958aa8b`.
  The original transferred pair was not modified.
- A real Docker Desktop mode-0600 bind owned by host `501:20` was readable as
  configured container `1000:1000`. The doctor continues to enforce exact
  numeric ownership on Linux. On Darwin it verifies the current host owner,
  warns that static metadata cannot prove the container view, and requires the
  documented live probe before startup.
- The exact implementation image at
  `103226e1531a6840d663cba2e83d0b07eb2a7bbc` is
  `sha256:e36c8264604a12be1603aeb2d9ad7682176b5fa7d3e683d8f636d5f9fd8e2f0f`,
  native arm64, non-root `1000:1000`, and contains no private runtime input.
  Its hardened offline run passed 2,054 tests with 96 intentional skips;
  focused container/recovery/doctor coverage passed 36 tests.
- The fresh ordinary PostgreSQL 18.4 database passed import and independent
  schema, winner-FK, application-owner, and bounded-count verification. Its
  second exact apply refused the non-fresh schema before restore. Both the
  ordinary and retained recovery volumes remain; only the ordinary service is
  running, with internal `5432/tcp` and no host publication.

The database is ready for the separately gated bot lifecycle unit. The bot is
not ready to start until the user confirms the VPS beta is stopped, the
cross-runtime writer audit is clear, and the ignored reviewed local container
configuration files are provided. Docker Desktop's remapped bind ownership is
a remaining platform behavior that must be live-probed on every startup; it is
not treated as equivalent to Linux host ownership.

## Remaining gates before supported use

1. Provide enough build headroom for Compose to exit cleanly, or use a
   reviewed remote build/registry workflow. The original VPS remained limited
   to about 2.3 GB free; the 2026-08-12 Mac audit had about 516 GiB available.
   Review vulnerability results for the exact images; P11.4A reviewed image
   history and locked packages but did not install a scanner.
2. Exercise persistent bot image/log volumes, runtime resource ceilings,
   SIGINT shutdown, exit-75 restart, database unavailability/recovery, and an
   actual development Discord login using a separately authorized token/writer
   window.
3. Perform a cross-runtime single-writer audit immediately before any
   container bot login. P11.4A deliberately left the healthy host beta as the
   only development writer and never started the container bot.
4. Only after those development results, design a separately approved
   production migration, secret/volume ownership, rollback, and monitoring
   plan. No production database, service, or command registration is
   authorized by this document.
