# Upstream production Docker deployment

Status: **active on GreenCloud**

GreenCloud production runs as the `polyelo-production` Compose project.
`polyelo.service` is disabled and retained only for rollback. Nothing in this
document is standing authorization to recreate or stop production, change
PostgreSQL, or synchronize Discord commands.

The deployment is deliberately conventional:

- one normal clone at `/srv/polyelo/PolyBot39`;
- root `Dockerfile`, `compose.production.yaml`, and ignored `.env`;
- ignored root `config.ini`, `server_settings.py`, and
  `spreadsheet_creds.json` mounted read-only;
- existing `data/images` and `logs` bind-mounted read-write;
- host PostgreSQL reached only through the read-only Unix-socket mount; and
- no published ports and no Compose-owned production database storage.

The public bundled-PostgreSQL deployment remains `compose.yaml`. The generic
host-PostgreSQL example remains `compose.host-postgres.yaml`. This file records
the upstream PolyElo deployment because it also requires the existing Bullet
credential file.

## Offline preparation

After reviewed source is on clean production `master`, create the ignored
environment file once:

```bash
id polyelo
cp .env.production.example .env
chmod 600 .env
docker compose config --quiet
docker compose build bot
```

Confirm the numeric UID/GID in `.env` still match the host `polyelo` account;
PostgreSQL peer authentication and private-file access depend on that identity.
`POLYBOT_IMAGE` is a stable local image name and does not change for each
commit.

Before relying on the image, confirm `.dockerignore` excludes every ignored
private/runtime input, including `config.ini`, `server_settings.py`,
`spreadsheet_creds.json`, `.env`, operator release state, generated graphs,
logs, backups, and persistent images. The private files belong only in the
documented runtime bind mounts; they must not be present underneath those
mounts in an image layer.

Building an image does not start a bot. Docker records its immutable image ID,
which can be inspected without maintaining duplicate source metadata:

```bash
git status --short --branch
git log -1 --oneline
docker compose config --images | sort -u
docker image inspect --format '{{.Id}}' \
  "$(docker compose config --images | sort -u)"
```

Verify the built image itself without its runtime mounts:

```bash
docker run --rm --network none --read-only --entrypoint /bin/sh \
  "$(docker compose config --images | sort -u)" -c \
  'test ! -e /app/.env &&
   test ! -e /app/config.ini &&
   test ! -e /app/server_settings.py &&
   test ! -e /app/spreadsheet_creds.json &&
   test ! -e /app/.operator-backup-release.json &&
   test ! -e /app/graph.png'
```

The runtime configuration check is also offline and redacted:

```bash
docker compose run --rm --no-deps bot \
  python scripts/check_runtime_config.py
```

The configured schema verification is read-only and must report no required
operations before a topology-only cutover:

```bash
docker compose run --rm schema --verify
```

While Compose is active, do not enable or start `polyelo.service`. A read-only
schema preflight may connect through the socket, but `schema --apply`, bot
recreation, and application-command apply remain separately authorized
operations.

## Cutover and rollback boundary

The initial GreenCloud cutover completed on 2026-08-27. Any future supervisor
change or rollback requires new explicit authorization. The reviewed boundary
is:

1. verify a fresh logical database backup and the existing image-directory
   backup;
2. verify clean reviewed source, the built Docker image ID, and a read-only
   schema plan;
3. stop and disable `polyelo.service`, then prove that no production writer
   remains before starting Compose;
4. apply only separately reviewed schema or guild-command changes, if any;
5. start exactly one `polyelo-production` Compose bot;
6. verify application identity, `polytopia2`/production role over the Unix
   socket, persistent paths, zero published ports, zero unexpected restarts,
   and one production writer; and
7. keep the systemd unit, host virtual environment, prior image, and backups
   intact until Compose is accepted.

Stopping without disabling systemd is not an acceptable steady state: a host
reboot could otherwise start systemd while Docker also restarts the container.
Rollback must stop the Compose bot before re-enabling and starting
`polyelo.service`, again preserving the one-writer boundary.

The existing host backup timers continue to own production backups. The
cutover did not replace them with a Compose backup service. The constrained
systemd release wrapper also remains installed until a separately reviewed
post-cutover cleanup; it must not be invoked while Compose is the active
supervisor.

After cutover, an ordinary source-only production update is intentionally the
same standard Compose workflow:

```bash
git pull --ff-only
docker compose up -d --build
```

Database schema changes, Discord command synchronization, and one-writer
cutover checks remain separately reviewed operations; they are not hidden in
the update command.
