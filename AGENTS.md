# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## Docker access on GreenCloud

- Codex's default task sandbox may remap supplementary groups to `nogroup`, so
  a sandboxed `id` or Docker socket permission denial is not evidence that
  `nelluk` lacks Docker access.
- For authorized read-only Docker or Compose inspection, retry the exact
  command outside the sandbox through the normal escalation/approval flow,
  without `sudo`. Verify with elevated `id` and `docker ps`.
- Do not chmod `/var/run/docker.sock`, change group membership, add sudo rules,
  or authorize mutating Docker operations based only on sandbox results.

## Constrained production release wrapper

- The tracked wrapper source is `deploy/polyelo-release`; its fixed installed
  path is `/srv/polyelo/bin/polyelo-release`. Read
  `docs/PRODUCTION_RELEASE_WRAPPER.md` before changing or invoking it.
- Invoke `sudo -n /srv/polyelo/bin/polyelo-release` only after Nelluk has
  explicitly authorized the production release, including its service,
  additive-schema, and fixed guild-command effects. The passwordless rule is
  an OS capability, not standing deployment authorization.
- The production checkout must already be clean on `master`. The wrapper does
  not fetch, merge, reset, restore, or install dependencies. It accepts no
  arguments and leaves `polyelo.service` stopped if an unprivileged release
  step fails.
- Add reviewed idempotent migrations to
  `scripts/production_release.sh`; do not broaden sudoers, expose production
  configuration, or add arbitrary arguments for normal releases.

## GreenCloud development-beta deployment

- Before every beta setup, deploy, start, stop, restart, status, or log
  operation, read the Development section of `/home/nelluk/SERVER_INFO.md`.
- GreenCloud's canonical beta is the direct `compose.beta.yaml` project in
  `/home/nelluk/PolyBot39-beta`. Its ignored root `.env` selects that file
  and the `polybot-mac-beta` project. It uses host PostgreSQL through the
  read-only `/var/run/postgresql` mount; Compose must not own a beta database.
- Before mutation, run `docker compose config --quiet`, `docker compose ps`,
  and, while the bot is running, the runtime configuration check documented
  in `docs/DEVELOPMENT_DOCKER.md`. Verify the checkpoint, application identity,
  database transport, restart count, and one-writer census against
  `SERVER_INFO.md`; stop and investigate any conflict. Beta Lab fixture
  readiness is not a deployment-health signal.
- For a source-only beta correction with no schema or command-tree change:
  run proportionate tests, create a clean exact Git checkpoint, update the
  ignored `.env` checkpoint and image tag, run `docker compose build`, inspect
  the schema plan, and recreate only `bot` with Compose. Verify the
  authenticated application, checkpoint, stable container, and one-writer
  census. Do not synchronize commands when command definitions did not change.
- Do not use the legacy `./polybot` deployment wrapper or a bundled PostgreSQL
  mode on GreenCloud unless Nelluk explicitly approves a topology change.

## Project Overview

Polytopia-ELO-Bot is a Discord bot for the mobile game Polytopia. It provides matchmaking, ELO-based leaderboards, and league management across multiple Discord servers (primarily the main Polytopia server and PolyChampions).

## Active Modernization Plan

For database-access and slash-command work, read and update
`docs/DATABASE_AND_SLASH_MODERNIZATION.md`. It is the durable execution
record for completed phases, current work, decisions, validation evidence,
and the next recommended unit.

For work split across planning/review and implementation Codex tasks, also
read `docs/MODERNIZATION_COLLABORATION_WORKFLOW.md`. Use separate Git
worktrees and its checkpoint/handoff protocol; never let two tasks switch or
edit the same checkout concurrently. This is the ordinary planned-work path,
not a prohibition on an owner-authorized production hotfix under the
proportional path below.

## Engineering Proportionality

Prefer the smallest complete solution and match process to demonstrated risk.
Do not invent extra services, abstractions, rollout phases, generalized
frameworks, or extended observation windows for a narrow reversible change
unless a concrete failure mode requires them. Existing production, database,
Discord, and destructive-action approval gates still apply; proportionality
means satisfying those gates directly, not multiplying them speculatively.

For an additive, backward-compatible schema change that the running code does
not read, an atomic apply plus exact verification in the planned maintenance
window is normally sufficient. Require a soak period only when there is a
specific runtime behavior to observe. Label optional hardening as optional and
lead with the minimal recommended path.

### Owner-authorized production hotfixes

When Nelluk explicitly asks for a narrow fix directly in the production
checkout or on production `master`, that instruction overrides the ordinary
modernization worktree/accumulation-branch workflow for that fix. Treat it as
a proportional hotfix, not automatically as a full production-cutover
project.

This path is appropriate when the change is small and reversible and does not
introduce a schema/data migration, dependency or runtime-topology change,
command-tree synchronization, credential/configuration change, destructive
operation, or broad architectural rewrite. Under this path:

- verify the exact production checkout, branch, clean starting state, and
  running version before editing;
- diagnose with narrow logs/read-only state, make the smallest complete fix
  with one writer, add focused regression coverage, run proportionate focused
  and adjacent tests, review the diff, and create a clean Git checkpoint;
- do not require a separate worktree/branch, beta deployment, full offline
  suite, independent multi-agent review, push, release ceremony, or extended
  soak unless a concrete risk in the actual change warrants it or Nelluk asks;
- keep aggregate operations such as `all` scopes within their established
  guild/data boundary when restoring a cross-guild single-object path; and
- treat production restart/deploy, Discord command sync or messages, database
  writes, and push as separate actions requiring their applicable explicit
  authorization. Nelluk may elect to perform the restart or smoke test.

If investigation expands beyond those limits, stop using the hotfix exception
and propose the ordinary isolated workflow for the expanded change. The fact
that code lives in the production checkout does not by itself raise a
source-only correction to the highest engineering risk tier.

## Tech Stack

- CPython 3.12
- discord.py 2.7.1 (Discord bot framework)
- Peewee ORM with PostgreSQL database
- FastAPI for optional REST API
- Matplotlib/Pandas/SciPy for statistics and graphing
- uv with `pyproject.toml` and `uv.lock` for reproducible environments

## Development Workspace Bootstrap

The deployed bot runs in Docker, but coding and test worktrees intentionally
share a host-only development environment from the primary checkout. A fresh
primary checkout must be bootstrapped, with dependency-installation approval,
before its worktree helper can run:

```bash
cd /home/nelluk/PolyBot39-beta
uv sync --locked --python 3.12.13
```

Do not run `uv sync` separately in each worktree. Run
`/home/nelluk/PolyBot39-beta/scripts/setup_development_worktree.sh "$PWD"`
and use `/home/nelluk/PolyBot39-beta/.venv/bin/python` for worktree tests.
The host `.venv` is development tooling only; its presence does not change the
Docker deployment model or authorize a native bot process.

## Running the Bot

```bash
# Create or synchronize the locked development environment
uv sync --locked

# Run the Discord bot
POLYBOT_ENV=development .venv/bin/python bot.py --skip_tasks

# Run with options
POLYBOT_ENV=development .venv/bin/python bot.py --add_default_data
POLYBOT_ENV=development .venv/bin/python bot.py --recalc_elo
POLYBOT_ENV=development .venv/bin/python bot.py --game_export
```

Production uses `POLYBOT_ENV=production` and `uv sync --locked --no-dev`.
Production execution and service restarts require separate explicit approval;
see `docs/PRODUCTION_CUTOVER.md`.

## Running the API Server

```bash
POLYBOT_ENV=development .venv/bin/python -m uvicorn server:server --host 127.0.0.1 --port 8000
```

The development API is disabled by default and requires its separate runtime
policy acknowledgement before this command can run.

## Configuration

- `config.ini` / `server_settings.py` - ignored production profile files
- `config.development.ini` / `server_settings_dev.py` - ignored development
  profile files
- `POLYBOT_ENV` - must be explicitly `production` or `development` in
  deployed commands

## Architecture

### Entry Points
- `bot.py` - Main Discord bot entry point. Initializes `MyBot` class and loads cog extensions
- `server.py` - FastAPI server entry point for REST API

### Core Modules (in `modules/`)
- `models.py` - Peewee ORM models (DiscordMember, Player, Team, Game, GameSide, Lineup, etc.). Contains ELO calculation logic
- `utilities.py` - Helper functions: DB connection management, game record locking, role lookups
- `settings.py` - Runtime settings, permission checks (`is_staff`, `is_mod`), guild configuration lookup via `guild_setting()`

### Discord Cogs (in `modules/`)
- `games.py` - Core game commands: win/lose, game info, player stats, ELO graphs
- `matchmaking.py` - Open game hosting, joining via reactions, matchmaking lobbies
- `league.py` - PolyChampions-specific: team management, drafts, house/tier system
- `administration.py` - Staff commands: game corrections, bans, bulk operations
- `misc.py` - Utility commands: guide, roles, info
- `bullet.py` - Bullet league management
- `customhelp.py` - Custom help command formatting
- `api_cog.py` - Discord cog that wraps API functionality

### Key Data Models
- `DiscordMember` - Discord user with Polytopia name
- `Player` - Server-specific player profile (DiscordMember + guild)
- `Team` - Competitive team with ELO rating
- `House` - Affiliation of teams (PolyChampions)
- `Game` - A match with sides, date, completion status
- `GameSide` - One side of a game (team or players)
- `Lineup` - Player assignments within a GameSide

### Multi-Server Architecture
The bot runs on multiple Discord servers with per-guild settings. `settings.config` (loaded from `server_settings.py`) contains server-specific configuration. Use `settings.guild_setting(guild_id, 'setting_name')` to retrieve guild-specific values.

## Command Prefix

Default prefix is `$` but can be configured per-guild via `command_prefix` setting.

## Permission System

User levels (0-7) control command access:
- Level 0: Unregistered
- Level 1-3: Progressive game hosting/joining permissions
- Level 4: Advanced matchmaking
- Level 5: Staff (helper roles)
- Level 6: Mod
- Level 7: Owner

Check with `settings.is_staff()`, `settings.is_mod()`, `settings.get_user_level()`.
