# Legacy PolyBot development-container implementation

This directory contains the lower-level implementation retained from the
former wrapper-managed **development-only upstream beta stack**. The canonical
beta now uses the repository-root
[`compose.beta.yaml`](../../compose.beta.yaml) through ordinary Compose
commands documented in
[`docs/DEVELOPMENT_DOCKER.md`](../../docs/DEVELOPMENT_DOCKER.md).

Do not use this legacy wrapper for a new deployment. It remains available as
migration and rollback reference while the direct-Compose deployment is
established. Independent production installations should start with
[`docs/DOCKER.md`](../../docs/DOCKER.md).

## Legacy wrapper reference

Run commands from the repository root. The `./polybot` wrapper selects the
right Compose files, project name, profiles, image checkpoint, and runtime
identity checks:

```bash
./polybot --help
./polybot setup
# Fill any placeholders reported by setup, then rerun it.
./polybot deploy
./polybot status
./polybot logs
```

`setup` prepares ignored private inputs and builds the exact clean Git
checkpoint. `deploy` runs that setup contract and then starts or replaces the
beta. Neither command turns this into a production deployment or synchronizes
Discord commands.

## Choose a database mode

| Mode | Database | Typical use |
| --- | --- | --- |
| `bundled` (default) | Compose-managed PostgreSQL and volume | Easiest isolated macOS/Linux beta |
| `external` | Separately managed PostgreSQL over TCP | Remote or otherwise network-reachable development database |
| `external-socket` | Host PostgreSQL through a read-only Unix-socket mount | Linux host with PostgreSQL kept off TCP |

Pass the mode on every command when it is not `bundled`, for example:

```bash
./polybot --mode external status
./polybot --mode external-socket deploy
```

The backup, restore verification, import, and first-guild bootstrap commands
are bundled-mode operations. External-database operators remain responsible
for provisioning, schema approval, backup, and restore.

## What the Compose files contain

- `compose.development.yaml`: bundled mode. Normal services are `postgres` and
  `bot`; one-shot setup jobs use the `tools` profile and backup/restore jobs use
  the `recovery` profile.
- `compose.development.external-db.yaml`: external TCP mode. It contains
  `bot` plus the one-shot `schema` tool and does not manage PostgreSQL.
- `compose.development.external-db.local-socket.yaml`: Linux-only overlay for
  external-socket mode; it adds the read-only PostgreSQL socket mount.
- `development.env.example`: documented interpolation inputs. `./polybot
  setup` creates and maintains the ignored `.env`; do not commit it.
- `container-contract.toml`: machine-readable fail-closed deployment contract.
- `Dockerfile` and the shell scripts: implementation details used by the
  wrapper and one-shot jobs.

The top-level Compose `name` values support direct diagnostic use. The legacy
wrapper overrides them with its fixed project name. Do not mix these definitions
with the root direct-Compose interface in one operation.

For the complete operator contract, recovery procedures, and retained
live-engine evidence, see
[`docs/CONTAINERIZED_DEVELOPMENT.md`](../../docs/CONTAINERIZED_DEVELOPMENT.md).
