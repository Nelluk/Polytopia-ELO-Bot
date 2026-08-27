# PolyBot development containers

This directory implements PolyBot's **development-only upstream beta stack**.
It is not the recommended production self-hosting path and contains fixed
upstream beta safety identities. For an independent production installation,
start with [`docs/SELF_HOSTING.md`](../../docs/SELF_HOSTING.md).

## Start with the wrapper

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

The top-level Compose `name` values support direct diagnostic use. The wrapper
intentionally overrides them with its fixed project name so all commands target
one beta namespace. Prefer the wrapper unless diagnosing the implementation.

For the complete operator contract, recovery procedures, and retained
live-engine evidence, see
[`docs/CONTAINERIZED_DEVELOPMENT.md`](../../docs/CONTAINERIZED_DEVELOPMENT.md).
