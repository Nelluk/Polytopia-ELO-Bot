# Containerized development feasibility and proof

Status: development-only static proof. This is not a production deployment
runbook and does not replace either existing systemd service.

## Verdict

A two-service Compose deployment is practical for PolyBot. The reviewed shape
keeps the bot image replaceable, gives PostgreSQL its own pinned-major service
and persistent volume, and removes host PostgreSQL role/database setup from the
ordinary development path. The repository now contains a build definition,
bundled-database Compose definition, external-database variant, explicit
database provisioner, and machine-readable contract.

This checkpoint is deliberately not a claim that an image has been built or
that Discord has been reached from a container. The current development host
has neither Docker nor Podman installed. The proof is therefore limited to
offline contract, shell-syntax, lockfile, and application tests. A supported
container deployment still needs a later, explicitly authorized machine with
a container engine to build, inspect, start, and smoke the exact images.

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
# the exact clean Git HEAD, then chmod the secrets 600.

python scripts/check_container_deployment.py --mode bundled
docker compose --file deploy/container/compose.development.yaml --profile tools config
docker compose --file deploy/container/compose.development.yaml build bot
docker compose --file deploy/container/compose.development.yaml up -d postgres
docker compose --file deploy/container/compose.development.yaml --profile tools run --rm database-provision
docker compose --file deploy/container/compose.development.yaml --profile tools run --rm schema
# Review the schema plan, then repeat with: --apply --confirm <exact-token>
docker compose --file deploy/container/compose.development.yaml up -d bot
docker compose --file deploy/container/compose.development.yaml logs --tail 100 bot
```

P11.2 adds `check_container_deployment.py`; until then the static tests are the
only repository-owned preflight. The doctor never invokes Docker, connects to
PostgreSQL or Discord, creates files, or changes permissions.

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

A later operational unit should provide digest-bound `pg_dump` and
`pg_restore --list` jobs, stopped-writer restore verification into a fresh
volume, free-space reporting, retention, and a restore drill. PostgreSQL major
upgrades require a separate dump/restore or `pg_upgrade` plan. Never point a
new major image at the existing volume and never use `latest`.

## Remaining gates before supported use

1. Resolve the exact Python, uv, and PostgreSQL image digests from their
   official registries and review their provenance. Version tags are pinned in
   this offline proof, but tags are not immutable digests.
2. Build with network access, review image history/packages and vulnerability
   results, then run the full offline suite inside the built bot image.
3. Exercise fresh-volume provision, repeated provision, schema plan/apply,
   volume persistence, resource ceilings, SIGINT, exit-75 restart, database
   unavailability/recovery, and an actual development Discord login.
4. Add the logical backup/restore drill and cross-runtime single-writer audit.
5. Only after those development results, design a separately approved
   production migration, secret/volume ownership, rollback, and monitoring
   plan. No production database, service, or command registration is
   authorized by this document.
