# Database and slash-command engineering contract

Last updated: 2026-08-27

Status: **current**. The original modernization program is integrated and
deployed. No release-candidate, migration, or cutover unit is active.

This is the compact authority for future database-access and Discord-command
work. Code, tests, runtime configuration, `AGENTS.md`, and the current Docker
runbooks remain authoritative when they are more specific.

The complete 27,000-line execution ledger and the completed upgrade, review,
candidate, and cutover records are preserved in Git at pre-cleanup checkpoint
`e99ec18e`. For example:

```bash
git show e99ec18e:docs/DATABASE_AND_SLASH_MODERNIZATION.md
git show e99ec18e:docs/MODERNIZATION_PRODUCTION_CUTOVER.md
```

Do not restore a historical procedure into current operations without
revalidating it against the current code, Docker topology, and live host.

## Current deployment state

- Public `master` contains the Docker-first self-hosting package.
- GreenCloud production runs as the `polyelo-production` Compose project from
  `/srv/polyelo/PolyBot39` and uses host PostgreSQL through a read-only Unix
  socket mount.
- The legacy `polyelo.service` is disabled host rollback material. It must never
  run concurrently with Compose.
- The ordinary reviewed source-only production update is `git pull --ff-only`
  followed by `docker compose up -d --build`.
- Schema changes and Discord command synchronization remain explicit,
  separately inspected operations; neither occurs during normal startup.
- Production database backups are owned by the host timer/service. There is no
  Discord-triggered backup command. Independent deployments use the Compose
  backup job.
- The optional HTTP API is disabled in upstream production.

See [PRODUCTION_DOCKER.md](PRODUCTION_DOCKER.md),
[DEVELOPMENT_DOCKER.md](DEVELOPMENT_DOCKER.md), and
[APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md](APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md)
for current operator procedures.

## Safety boundaries

- Treat production and development as different bot applications, guild sets,
  databases, roles, configurations, images, and Compose projects.
- Never start a second bot writer against the same database. Process-local
  coordinators do not serialize another process.
- Production deployment, restart, schema writes, database writes, Discord
  command apply, and external messages require their applicable explicit
  authorization.
- Development integration tests must fail closed unless they verify the exact
  `development` / `polytopia_dev` / `polybot_dev` identity.
- Production schema tools must verify the exact configured and live database
  identity and use their explicit confirmation before applying.
- No ordinary import or startup path may create or alter database schema.
- No startup or reconnect path may synchronize Discord application commands.
- Global Discord command synchronization is unsupported. The command manager
  is guild-only and refuses apply while global commands exist.
- Keep Discord API operations outside database transactions.
- Preserve one-writer, backup, and rollback evidence before any destructive or
  incompatible data operation.
- Do not weaken a safety gate to make a test or deployment pass.

## Database execution contract

Blocking Peewee and filesystem work must not run on Discord's event-loop
thread. A database workflow should normally:

1. parse Discord inputs and capture primitive identifiers on the event loop;
2. defer the interaction when work may exceed the response window;
3. reserve the relevant process-local coordinator or record claim;
4. open a worker-local Peewee connection;
5. reload and revalidate mutable rows using primitive identifiers;
6. perform the complete write and protected audit in one bounded transaction;
7. commit or roll back and close the worker connection; and
8. publish Discord effects only after commit from immutable primitive results.

Do not pass live Peewee model objects, Discord objects, lazy queries, database
proxies, or open transactions between the event loop and workers. Revalidate
permissions and state inside the transaction when they can change after the
initial interaction check.

Post-commit Discord failure must be reported as committed-but-unpublished and
must not invite the caller to repeat the database mutation. Where a workflow
can reconcile, preserve enough immutable identifiers to do so safely.

## Schema contract

The generic configured-target schema manager is the current installation and
upgrade interface:

```bash
python scripts/manage_schema.py
python scripts/manage_schema.py --apply --confirm 'PRINTED CONFIRMATION'
python scripts/manage_schema.py --verify
```

The default plan and verify modes are read-only. Apply obtains the reviewed
lock, performs only registered compatible additions, commits atomically, and
verifies through a new connection. It must refuse incompatible existing
columns and the wrong environment/database/role.

Release-specific migration modules remain narrowly useful for their original
columns and tests, but they are not a general deployment sequence and must not
be chained into ordinary releases.

## Discord command contract

The current model-free command source loads these roots:

- `elo`
- `game`
- `guild`
- `house`
- `leaderboard`
- `league`
- `operator`
- `player`
- `squad`
- `staffhelp`
- `team`

Current first-level structure:

- `/elo recalculate|status`
- `/game join|keep-active|leave|logs|manage|map|name|notes|open|ping|ranked|record|result|search|show|side|start|tribe|win`
- `/guild edit`
- `/house create|image|list|name|show`
- `/leaderboard activity|players|roles|squads|teams`
- `/league badge|free-agents|guide|join-novas|maintenance|mark-active|roster|season|tokens`
- `/operator bot|channels|guild|player|tribe`
- `/player register|show|timezone`
- `/squad name|show`
- `/staffhelp`
- `/team archive|create|emoji|house|image|name|server|show|tier`

Capability policy is default-deny. Discord filters top-level roots, so a
capability cannot hide one child command within an otherwise visible root.
Runtime permission checks remain authoritative even when Discord default
permissions hide an operator root.

The explicit manager must be used after command definitions or capability
assignments change:

```bash
python scripts/manage_application_commands.py \
  --environment ENVIRONMENT --mode plan --guild-ids GUILD_IDS
python scripts/manage_application_commands.py \
  --environment ENVIRONMENT --mode inspect --guild-ids GUILD_IDS
python scripts/manage_application_commands.py \
  --environment ENVIRONMENT --mode apply --guild-ids GUILD_IDS \
  --confirm-environment ENVIRONMENT \
  --confirm-guild-ids GUILD_IDS \
  --confirm-scope guild \
  --confirm-no-global-sync
```

Apply only the exact inspected guild set. Do not broaden a command change to
every configured guild merely because the tool can enumerate them.

## Compatibility decisions

- The prefix interface is retained only where current source still registers
  it. Retirement tests protect explicitly removed commands and aliases.
- `/operator database backup`, `$backup_db`, and `$dbb` are fully retired.
  Upstream manual backup is a host operation; self-hosting uses Compose.
- `$restart`, `$restart_force`, and `$quit` are retired. The retained native
  operator restart exits only under an explicitly recognized supervisor.
- Obsolete repair/diagnostic prefixes including `gtest`, `ptrophies`,
  `boost_from`, and `boost_from_norole` remain retired.
- `/staffhelp` is a no-option private intake surface. Independent operators
  must configure their own private route and policies before exposing it.
- `guild` and `operator` may share a capability assignment, but `/guild edit`
  has same-guild delegated authorization while `/operator` remains restricted
  bot-wide administration.
- Development database-backed guild configuration remains a separately guarded
  authority. Production continues to use its reviewed static configuration
  until a new production-authority project is explicitly approved.

## Current work and future units

There is no active modernization unit. New database/slash work must create a
bounded unit here before implementation with:

- objective and non-goals;
- exact files and command surface;
- database/schema/Discord effects;
- permission and concurrency boundaries;
- required focused, adjacent, and full validation;
- deployment and rollback disposition; and
- explicit actions that remain separately authorized.

Use isolated worktrees when planning and implementation are split across
tasks. A narrow owner-authorized production hotfix may instead follow the
proportional path in `AGENTS.md`.

## Validation baseline

For ordinary offline work, use the configured development profile and locked
development environment. At minimum:

- run focused tests for the changed service/adapter;
- run adjacent taxonomy, permission, lifecycle, and documentation tests when
  those boundaries change;
- run full offline discovery before a broad integration checkpoint;
- run `git diff --check`; and
- review the exact diff for accidental configuration, credential, generated,
  or historical-artifact changes.

Database integration, Docker deployment, Discord inspection/apply, and live
acceptance are separate gates. Record exact results and intentional skips; do
not describe an unrun gate as passing.

## Latest completed operational changes

- 2026-08-27: GreenCloud production moved from systemd to the direct
  `polyelo-production` Compose project with one writer, host PostgreSQL, host
  backups, bounded Docker log rotation, and no published bot port.
- 2026-08-27: Compose source checkpoint plumbing was removed; ordinary source
  updates use standard Git and Compose primitives.
- 2026-08-27: `/operator database backup` was removed from source and the
  PolyChampions guild tree. Source checkpoint `9dd701e9` passed focused, full,
  fresh-install, and Compose-install validation before deployment.
- 2026-08-27: documentation was separated into current and historical
  audiences at checkpoint `e99ec18e`; the later aggressive cleanup keeps that
  commit as the complete historical archive.

Update this section only for changes that materially alter the current
database, command, deployment, or operational contract. Detailed completed
unit transcripts belong in Git history, not in this file.
