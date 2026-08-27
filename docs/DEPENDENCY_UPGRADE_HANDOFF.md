# Python 3.12 and dependency upgrade handoff

Status: **historical completed upgrade record; do not use as a current
deployment procedure**

This document was the source of truth for the completed Python 3.12 and
dependency upgrade. Current dependencies are defined by `pyproject.toml` and
`uv.lock`; current deployment instructions are [DOCKER.md](DOCKER.md),
[SELF_HOSTING.md](SELF_HOSTING.md), and
[PRODUCTION_DOCKER.md](PRODUCTION_DOCKER.md). The paths, service topology, and
baseline below describe the 2026-07-27 starting point.

## Objective

Build and validate a development instance of PolyBot using a uv-managed
CPython 3.12 environment, then upgrade dependencies in controlled groups. The
production checkout, interpreter, database, bot token, runtime image data, and
running service must remain untouched until the development version has passed
all offline and live acceptance checks.

This is an execution project, not permission to restart production, change the
production database, install system packages, or deploy. Obtain Nelluk's
explicit approval before any such action.

## Verified starting point

The planning task verified the following on 2026-07-27:

- Production checkout: `/home/nelluk/PolyBot39`
- Baseline commit: `e3535ad` (`Document dependency upgrade safety checks`)
- `master` and `origin/master` were synchronized at that commit.
- Working tree was clean before this handoff document was added.
- Production runtime: CPython 3.9.20 in the repository-root legacy virtualenv.
- Host: Ubuntu 22.04 x86-64.
- `/usr/bin/python3.10` is present.
- Docker, Podman, uv, and pyenv were not installed.
- Approximately 5.7 GB was free on `/`.
- The legacy environment's imports work, but its `pip` command fails because
  `distutils.cmd` is unavailable.
- The complete pre-upgrade package inventory is recorded in
  `docs/dependency-baseline-2026-07-27.txt`.
- `scripts/dependency_inventory.py` can inventory an environment without pip.
- The offline suite contains 21 passing tests:

  ```bash
  bin/python -W error::DeprecationWarning \
    -m unittest discover -s tests -v
  ```

Re-verify the Git state and test baseline in the new clone. Do not assume the
commit ID above is still the remote tip.

## Decisions already made

These decisions should be treated as fixed unless new evidence reveals a
concrete problem:

1. Use a separate clone at `/home/nelluk/PolyBot39-dev`, not a Git worktree.
2. Develop on a branch such as `dev-dependency-upgrade`; do not work on
   `master`.
3. Do not containerize as part of this upgrade. Reconsider containers only
   after the native deployment is stable.
4. Use a uv-managed CPython 3.12 interpreter and a `.venv` in the development
   clone.
5. Initially constrain Python to `>=3.12,<3.13`. Widen support later in a
   separate change.
6. Use an explicit `POLYBOT_ENV` value. Do not infer the environment from a
   token, hostname, branch, checkout path, or database name.
7. A single environment selection must choose the complete runtime profile:
   Discord identity, database, server configuration, runtime files, logs,
   background-task policy, and API policy.
8. Development must use a separate Discord application, PostgreSQL database,
   PostgreSQL role, guild configuration, images, and logs.
9. Development background jobs and the HTTP API are disabled initially.
10. Keep the production database schema and production image location
    unchanged during this project.
11. Upgrade dependency groups in separate, testable commits. Do not run a
    blanket upgrade.
12. Keep the existing production interpreter and environment intact as the
    rollback target until the upgraded bot has been stable in production.

## Isolation model

The desired layout is:

```text
/home/nelluk/PolyBot39
    production clone on master
    existing Python 3.9 environment
    production configuration
    production data/images

/home/nelluk/PolyBot39-dev
    development clone on dev-dependency-upgrade
    uv-managed Python 3.12 + .venv
    development configuration
    development data/images
    development logs
```

Changing only the Discord token is unsafe. Importing `modules.models` currently
connects to PostgreSQL, creates missing tables, and attempts to create a
deferred foreign key. Commands and background jobs can also write to the
configured database.

## Phase 0: create and verify the development clone

Nelluk plans to create the clone before starting the execution task. A typical
sequence is:

```bash
cd /home/nelluk
git clone git@github.com:Nelluk/Polytopia-ELO-Bot.git PolyBot39-dev
cd /home/nelluk/PolyBot39-dev
git switch -c dev-dependency-upgrade
```

The execution task should begin with:

```bash
git status --short --branch
git log -3 --oneline --decorate
python3 --version
```

Confirm that:

- The current directory is `/home/nelluk/PolyBot39-dev`.
- The branch is not `master`.
- The production checkout has not been modified.
- Step 1 files are present.

Do not copy the production `config.ini` wholesale into the development clone.
Create development configuration from tracked examples and insert only the
required development credentials.

## Phase 1: central runtime profiles

Implement and test this phase using the existing environment before performing
the dependency upgrade. Keep it as its own commit.

### Environment selector

Support exactly:

```text
POLYBOT_ENV=production
POLYBOT_ENV=development
```

Reject unknown values with a clear startup error. For transition compatibility,
an unset value may temporarily mean `production`, but both eventual service
definitions should set the value explicitly.

Do not scatter checks such as `if development` across the application. Create
one immutable runtime-profile object early in startup and have modules consume
its resolved values.

### Profile resources

The profile should resolve at least:

- Environment name.
- Configuration file.
- Discord token.
- Expected Discord application/bot ID.
- Owner ID.
- PostgreSQL database, user, password, host, and port/socket settings.
- Server-settings module.
- Whether recurring background tasks may start.
- Whether the HTTP API may start.
- Image root.
- Log root.

Preserve the current production paths initially:

```text
production images: data/images
production logs:   logs
```

Use distinct development paths:

```text
development images: data/development/images
development logs:   logs/development
```

The loader must create required development log/image directories with
appropriate user-only or group-safe permissions. It must never create or
modify production resources merely while validating a development profile.

### Configuration files

A reasonable target is:

```text
config.ini                         existing ignored production config
config.development.ini             ignored development secrets
config.development.ini-EXAMPLE     tracked template
server_settings.py                 existing ignored production settings
server_settings_dev.py             ignored development settings
server_settings_dev-EXAMPLE.py     tracked test-guild-only template
```

Keeping `config.ini` as the production filename avoids an unnecessary
production migration in the first deployment. The environment profile selects
the other files for development.

Add PostgreSQL password/host configuration instead of retaining the current
hard-coded password in `modules/models.py`. Preserve compatible defaults only
where required for the first production rollout, and never log a token or
password.

### Modules that must use the profile

At minimum, inspect and update:

- `settings.py`
- `bot.py`
- `modules/models.py`
- `modules/api.py`
- `modules/image_storage.py`
- `logging_config.py`
- `server.py`

`modules/api.py` currently reads `config.ini` independently. Remove that second
configuration path so the API cannot accidentally connect its Discord client
with a different environment's token.

### Fail-fast development checks

Before importing the model layer or starting Discord, development should reject:

- A database name that does not clearly identify a development database.
- The configured production database name, when it can be determined safely.
- The production Discord application ID.
- A server-settings profile containing production guilds, unless Nelluk has
  explicitly approved exact shared guild IDs and the development configuration
  records both those IDs and a separate risk acknowledgement.
- Image or log paths that resolve to the production paths.
- Background tasks or API enablement unless intentionally enabled by a
  separate, explicit setting.

After Discord login, compare `bot.user.id` with `expected_bot_id` and terminate
on mismatch.

Add a safe configuration-inspection command, such as
`scripts/check_runtime_config.py`, that imports only the profile loader and
prints a redacted summary:

```text
environment
expected bot ID
database name and host (never password)
server-settings module
allowed guild IDs
task/API policy
image/log roots
```

It must not import `modules.models`, connect to PostgreSQL, or connect to
Discord.

### Phase 1 tests

Add tests for:

- Production selection and preserved paths.
- Development selection and isolated paths.
- Unknown environment rejection.
- Missing required development values.
- Redacted diagnostic output.
- Production database/token/guild/path rejection in development.
- `modules.api` using the central profile.
- Expected bot-ID validation.
- CLI `--skip_tasks` still forcing tasks off.

Use temporary configuration files and directories. Tests must not connect to
Discord or PostgreSQL.

Acceptance gate:

```bash
bin/python -W error::DeprecationWarning -m unittest discover -s tests -v
bin/python -m compileall -q bot.py server.py modules scripts tests
git diff --check
```

## Phase 2: create development external resources

This phase includes external state and requires explicit approval from Nelluk.

Before requesting sudo, follow `AGENTS.md` and inspect
`/home/nelluk/disk-audit-latest.txt`. Do not create users/databases or install
software without approval.

### PostgreSQL

Create:

```text
database: polytopia_dev
role:     polybot_dev
```

The development role should own the development database and have no privileges
on the production database. Use a separately generated password. Do not commit
it or expose it in command output.

Start with an empty development database. Importing the model layer will create
the schema. Seed only the minimum required reference data. If realistic testing
later requires a production snapshot, restore a backup into the development
database, treat it as sensitive, keep background tasks disabled, and never
connect the dev role to production.

### Discord

Use:

- The existing development bot application/token.
- The expected development bot ID.
- Only Nelluk's test Discord guild.
- A visibly distinct command prefix or activity.

On 2026-07-27, Nelluk explicitly approved sharing private test guild
`478571892832206869` and test channel `480078679930830849` with the production
bot. The development profile must record this exact guild in
`shared_production_guild_ids` and set the separate risk acknowledgement. No
other production guild or channel is approved for development.

The development `server_settings_dev.py` must not contain other production
guild or channel IDs except constants that are never included in the active
server map.

### Preflight

Run the redacted configuration check before importing models. Confirm aloud in
the task update that it identifies the development database, development bot
ID, test guild, task/API disabled state, and isolated paths.

Then perform the first schema initialization against only `polytopia_dev`.
Verify the connected database from PostgreSQL before proceeding.

## Phase 3: install uv-managed Python 3.12

Installing uv and downloading Python require network access and external writes;
obtain approval as required by the execution environment.

Use the official uv installation method and documentation:

- <https://docs.astral.sh/uv/getting-started/installation/>
- <https://docs.astral.sh/uv/guides/install-python/>

Target:

```bash
uv python install 3.12
uv venv --python 3.12 .venv
```

Do not use uv's `--default` option and do not replace `/usr/bin/python3`.
Confirm:

```bash
uv run python --version
```

It must report Python 3.12.x from the development clone's environment.

Execution checkpoint (2026-07-27):

- Installed uv 0.11.32 with the pinned official standalone installer
  (`SHA-256 43aff33a967fe40e8c17949d8c85c65bc43f3b5c94742393c957f56ab5ba80f4`).
- Disabled shell-profile modification during installation.
- Installed uv-managed CPython 3.12.13 without `--default`.
- Created `/home/nelluk/PolyBot39-dev/.venv` from that managed interpreter.
- Installed no project dependencies; dependency locking remains Phase 4.

## Phase 4: reproducible project metadata

Add:

```text
pyproject.toml
uv.lock
```

Set:

```toml
requires-python = ">=3.12,<3.13"
```

Declare only direct application dependencies in `pyproject.toml`. Put testing,
locking, and audit tools in a development dependency group. Commit `uv.lock`.
Keep `.venv` ignored.

Do not delete `requirements.txt` during the migration. Decide after successful
production cutover whether it remains a generated compatibility export:

```bash
uv export --frozen --no-dev --format requirements-txt
```

The exact command should be verified against the installed uv version.

The first lock should resolve a coherent Python 3.12 environment; it does not
need to maximize every package version in one operation.

Acceptance gate:

```bash
uv sync --frozen
uv run python scripts/dependency_inventory.py
uv run python -W error::DeprecationWarning \
  -m unittest discover -s tests -v
uv run python -m compileall -q bot.py server.py modules scripts tests
git diff --check
```

Test a clean recreation of `.venv` only after confirming it is the development
clone's `.venv` and that removal/recreation is explicitly in scope.

Execution checkpoint (2026-07-27):

- Added `pyproject.toml` and `uv.lock` for Python `>=3.12,<3.13`; the project is
  non-packaged and `requirements.txt` remains unchanged.
- Declared runtime imports and entry-point dependencies directly. Added
  `pip-audit==2.10.1` only to the development dependency group.
- Preserved the production baseline where it is coherent on Python 3.12.
  Required compatibility lifts are pandas 2.2.2, Pillow 10.4.0, NumPy 1.26.4,
  aiohttp 3.9.5, and Pydantic 1.10.16. Pydantic 1.10.15 failed on Python
  3.12.13 because it did not pass `recursive_guard` to
  `ForwardRef._evaluate()`.
- Constrained pyparsing to the recorded production version 2.4.7. Its exact
  Python 3.12 `sre_constants` warning and discord.py 2.3.2's exact `audioop`
  warning are ignored only in tests; resolving them belongs to the numerical
  and Discord Phase 5 groups. Other deprecation warnings remain errors.
- `uv sync --frozen` checked 93 installed packages. The inventory reports
  CPython 3.12.13 from `/home/nelluk/PolyBot39-dev/.venv`.
- The strict offline suite passed 39 tests, compileall passed, and
  `git diff --check` passed. No bot, API server, or service was started.
- `pip-audit --locked .` does not recognize `uv.lock`. Auditing a temporary
  frozen `uv export` instead reported 70 advisory rows (including duplicates)
  across aiohttp 3.9.5, Gunicorn 20.1.0, Pillow 10.4.0, Requests 2.31.0, and
  Starlette 0.37.2. Nothing was auto-fixed; address these packages in their
  defined Phase 5 groups.

## Phase 5: dependency upgrade groups

Upgrade and commit one group at a time. Read official migration notes before
each group. After every group, run the full acceptance gate and record the
resolved versions.

Recommended order:

1. Packaging/test tooling.
2. Requests, Peewee, psycopg2, and gspread-related packages.
3. Pillow.
4. NumPy, SciPy, pandas, and Matplotlib together.
5. discord.py and its aiohttp stack.
6. FastAPI, Pydantic, and Starlette together.
7. Uvicorn, Gunicorn, uvloop, and httptools.

Do not split ABI-coupled numerical packages across arbitrary environments.
Do not upgrade FastAPI without handling the Pydantic v1-to-v2 boundary and
testing every API route.

Known compatibility points:

- Pillow 10 removed old font-size APIs. The repository already moved relevant
  card code to `getbbox()` and current resampling/layout constants.
- FastAPI currently warns that `@server.on_event("startup")` is deprecated.
  Migrate to lifespan handling during the API group.
- `modules/api.py` uses an older Discord client loop initialization pattern.
- The production baseline uses Pydantic 1.8.2 even though FastAPI is newer.
- The numerical stack is mixed-age and must be resolved together for Python
  3.12 wheels.

Each upgrade commit should contain only the lock/metadata changes, required
compatibility code, and relevant tests for that group.

Group 1 execution checkpoint (2026-07-27):

- Reviewed packaging and test tooling after the Phase 1-4 checkpoint commit.
  The only direct development dependency is `pip-audit==2.10.1`; the offline
  suite uses the standard-library `unittest` package. uv remains the separately
  installed, pinned 0.11.32 executable rather than a project dependency.
- Verified from upstream release metadata that pip-audit 2.10.1 is current,
  then ran `uv lock --upgrade-group dev`. The lock still resolves 95 packages
  and produced no metadata or lockfile changes.
- The existing ignore rules already cover uv's project-local `.venv/` and
  `.python-version`; `pyproject.toml` and `uv.lock` correctly remain tracked.
  No `.gitignore` change was needed.
- A clean `.venv` recreation remains deferred to a later release-candidate
  gate. Frozen synchronization already verifies the current environment, and
  recreation would add a destructive step without changing this no-op group.
- The full acceptance gate passed: frozen sync checked 93 packages, lock
  validation resolved 95 packages, the CPython 3.12.13 inventory completed,
  all 39 strict offline tests passed, compileall passed, and the diff check
  passed.
- The repeated audit is unchanged at 70 advisory rows (including duplicates)
  across the five packages assigned to later runtime dependency groups. No
  package was auto-fixed.

Group 2 execution checkpoint (2026-07-27):

- Reviewed upstream release notes and the repository's use of Requests,
  Peewee, psycopg2, gspread-asyncio, gspread, and Google service-account
  credentials.
- Upgraded Requests 2.31.0 to 2.34.2, Peewee 3.17.5 to 3.19.0,
  psycopg2-binary 2.9.9 to 2.9.12, and google-auth 2.35.0 to 2.56.2.
- Intentionally retained Peewee on its latest 3.x release. Peewee 4 is a
  separate major-version migration with removals and behavioral/default
  changes; it is not required for Python 3.12 or for this dependency group.
- gspread-asyncio 2.0.0 remains its latest release and hard-pins
  `gspread==6.0.*`. An attempted gspread 6.2.1 resolution correctly failed, so
  gspread remains at the newest compatible version, 6.0.2. No unreleased Git
  dependency or async-wrapper rewrite was introduced.
- google-auth's resolved dependency path replaced cachetools 5.5.2 and rsa
  4.9.1 with cryptography 49.0.0, cffi 2.1.0, and pycparser 3.0. The complete
  lock now resolves 96 packages and the dev environment contains 94 packages.
- Added an offline Group 2 smoke test covering gspread exceptions,
  gspread-asyncio manager construction, Google service-account credential
  construction APIs, and psycopg2's `DuplicateObject` exception.
- The full acceptance gate passed under CPython 3.12.13: all 40 strict offline
  tests passed, compileall passed, lock validation passed, and the diff check
  passed. No database, Discord, or HTTP service connection was made.
- The frozen audit improved from 70 advisory rows across five packages to 67
  rows across four packages; Requests no longer has findings. No package was
  auto-fixed.

Group 3 execution checkpoint (2026-07-27):

- Reviewed the Pillow 10 through 12 deprecation/removal notes and all
  repository Pillow usage, then upgraded Pillow 10.4.0 to the current 12.3.0
  release.
- Replaced two deprecated `Image.getdata()` calls with
  `get_flattened_data()`, preserving a mutable list where the inverse-text
  renderer edits mask pixels. Updated the anti-scam hash resize to
  `Image.Resampling.LANCZOS` and made its input image lifetime explicit.
- Added an offline anti-scam average-hash test. The strict suite now covers
  hashing, image normalization, inverse text, draft cards, and arrow cards.
- The first strict run caught the immutable tuple returned by
  `get_flattened_data()` in the inverse-text path. After the localized list
  conversion, the full acceptance gate passed under CPython 3.12.13: all 41
  strict offline tests passed, compileall passed, lock validation passed, and
  the diff check passed.
- uv resolved the same 96-package graph and changed only Pillow from 10.4.0 to
  12.3.0 for this group. No database, Discord, or HTTP service connection was
  made.
- Once execution limits cleared, the frozen audit confirmed that all Pillow
  findings were removed. It now reports 43 advisory rows across only aiohttp
  (32), Gunicorn (2), and Starlette (9), all assigned to later groups. No
  package was auto-fixed.

Group 4 execution checkpoint (2026-07-27):

- Reviewed the repository's complete numerical/plotting usage and the upstream
  NumPy 2, pandas 3, SciPy, Matplotlib, and pyparsing compatibility notes.
  Usage is limited to ELO-history DataFrames, daily resampling/interpolation,
  Savitzky-Golay filtering, and Matplotlib PNG output.
- Upgraded Matplotlib 3.8.4 to 3.11.1, NumPy 1.26.4 to 2.5.1, pandas 2.2.2
  to 3.0.5, pyparsing 2.4.7 to 3.3.2, and SciPy 1.13.0 to 1.18.0 as one
  coupled numerical stack. pandas 3 no longer resolves pytz on Linux, and
  tzdata is now platform-conditional.
- Removed the temporary `sre_constants` deprecation-warning exception that
  had existed solely for pyparsing 2.4.7. The only remaining strict-suite
  warning exception is discord.py 2.3.2's deferred stdlib `audioop` import.
- Added a focused offline test that runs the production-shaped pandas daily
  resampling/interpolation pipeline, applies SciPy's `savgol_filter`, verifies
  finite NumPy output, and renders a Matplotlib PNG through an in-memory
  buffer using the headless backend.
- The focused test and full acceptance gate passed under CPython 3.12.13:
  all 42 strict offline tests passed, compileall passed, frozen lock
  validation resolved 95 packages, dependency inventory passed, and the diff
  check passed. No database, Discord, or HTTP service connection was made.
- The frozen audit remains at 43 advisory rows across aiohttp (32), Gunicorn
  (2), and Starlette (9), all assigned to later upgrade groups. None of the
  Group 4 packages has a reported finding, and no package was auto-fixed.

Group 5 execution checkpoint (2026-07-27):

- Reviewed discord.py 2.4 through 2.7 and aiohttp 3.10 through 3.14 release
  notes, plus every repository use of Discord and asyncio loop APIs. aiohttp is
  used only as discord.py's transport dependency; the repository has no direct
  aiohttp client or server call sites.
- Upgraded discord.py 2.3.2 to 2.7.1 and aiohttp 3.9.5 to 3.14.3 together.
  The resolved graph added aiohappyeyeballs 2.7.1 and now contains 96 packages.
- Removed the obsolete `loop=` argument from the API Discord client and
  centralized its construction. Replaced all runtime `bot.loop.create_task`
  and `bot.loop.run_in_executor` uses with `asyncio.create_task` and
  `asyncio.get_running_loop().run_in_executor`, respectively.
- Added offline coverage for current Discord/aiohttp client construction and a
  guard against reintroducing legacy `bot.loop` or `Client(loop=...)` access.
  Updated the runtime-profile startup test to verify task scheduling through
  `asyncio.create_task`.
- discord.py 2.7.1 still imports Python 3.12's deprecated stdlib `audioop`
  module. The existing exact-message warning exception therefore remains; all
  other deprecation warnings remain errors. This project is intentionally
  constrained to Python 3.12 during this upgrade.
- The first migrated strict run found one outdated test double that returned a
  tuple where the new scheduling path expected a coroutine. After updating
  that fixture to mock `asyncio.create_task`, all 44 strict offline tests
  passed under CPython 3.12.13. Compileall, dependency inventory, frozen lock
  validation, and the diff check also passed.
- The frozen audit improved from 43 advisory rows across three packages to 11
  rows across only Gunicorn (2) and Starlette (9), both assigned to later
  groups. All 32 aiohttp findings were cleared. No package was auto-fixed, and
  no database, Discord, or HTTP service connection was made.

Group 6 execution checkpoint (2026-07-28):

- Reviewed the FastAPI Pydantic-v2 migration guidance, Starlette 1.x release
  and lifespan notes, the single repository Pydantic model, and all three API
  routes.
- Upgraded FastAPI 0.111.0 to 0.139.2, Pydantic 1.10.16 to 2.13.4, Starlette
  0.37.2 to 1.3.1, and the coupled AnyIO 3.7.1 constraint to 4.14.2.
  Pydantic Core 2.46.4, annotated-types 0.8.0, and typing-inspection 0.4.2
  were added.
- Current FastAPI has a substantially leaner base dependency set than 0.111.0.
  Removing its former bundled standard extras reduced the resolved graph from
  96 to 81 packages while retaining the project's explicit Uvicorn and related
  server dependencies for their later group.
- Replaced the deprecated `@server.on_event("startup")` hook with an
  async-context-managed lifespan. It validates API enablement, starts the
  Discord client task, closes the client, cancels/awaits the task, and clears
  client state deterministically. Discord-backed member lookup now returns an
  explicit 503 if called without an active lifespan client.
- Updated the Pydantic smoke test to use `model_dump()`. Added direct,
  network-free ASGI coverage for the users-read, games-read, and game-create
  routes, including dependency overrides, response serialization, request
  validation, note persistence, and a 422 response for an invalid body.
- Starlette's new HTTPX2-backed synchronous `TestClient` stalled even with an
  empty Starlette app in this environment. The temporary HTTPX2 test
  dependency was removed, and route coverage uses the application directly
  through ASGI messages instead. This keeps the lock minimal and tests the
  actual FastAPI routing stack without a network connection.
- All 44 strict offline tests passed under CPython 3.12.13. Compileall,
  dependency inventory, frozen lock validation, and the diff check passed.
  No database, Discord, or HTTP service connection was made.
- The frozen audit improved from 11 advisory rows across Gunicorn and
  Starlette to two rows in only Gunicorn 20.1.0. All nine Starlette findings
  were cleared. Gunicorn is assigned to Group 7; no package was auto-fixed.

Group 7 execution checkpoint (2026-07-28):

- Reviewed the official Uvicorn 0.51 and Gunicorn 26 release notes, current
  PyPI metadata for uvloop and httptools, every repository server-package
  reference, and the API entry point. The repository has no Gunicorn
  configuration or API service definition; `server.py` documents direct
  Uvicorn execution.
- Upgraded Uvicorn 0.29.0 to 0.51.0, Gunicorn 20.1.0 to 26.0.0, uvloop
  0.19.0 to 0.22.1, and httptools 0.6.1 to 0.8.0. Gunicorn no longer depends
  on setuptools, reducing the resolved graph from 81 to 80 packages.
- Added a focused network-free server-stack test. It loads a Uvicorn
  configuration around a trivial in-memory ASGI callable with the httptools
  protocol, resolves Gunicorn's native ASGI worker with uvloop and lifespan
  enabled, creates and closes a uvloop event loop, and parses a URL through
  httptools. It does not import `server.py`, run a server, bind a socket,
  connect Discord, or touch a database.
- The full acceptance gate passed under CPython 3.12.13: frozen sync checked
  77 installed packages, lock validation resolved 80 packages, all 45 strict
  offline tests passed, compileall passed, dependency inventory passed, and
  the diff check passed.
- The frozen audit reports no known vulnerabilities. Upgrading Gunicorn
  removed the final two advisory rows. No package was auto-fixed, and no
  database, Discord, or HTTP service connection was made.

## Phase 6: offline and database integration testing

The default suite must remain network-free and database-free.

Add an explicitly enabled integration suite for `polytopia_dev` that verifies:

- Model import/schema initialization.
- Database connection identity.
- Representative Peewee reads and transaction rollback.
- Bot extension/cog loading with recurring tasks disabled.
- API application construction and route requests.
- Promotion, demotion, draft, graph, and embed rendering.

Integration tests must refuse to run unless:

- `POLYBOT_ENV=development`
- The database passes development safety validation.
- A separate explicit integration-test flag is present.

Use transactions and rollback where practical. Do not point integration tests
at production.

Execution checkpoint (2026-07-28):

- Added `tests/test_database_integration.py`, gated by both
  `POLYBOT_ENV=development` and `POLYBOT_RUN_DB_INTEGRATION=1`. Before
  importing `settings` or `modules.models`, it requires the exact
  `polytopia_dev` database and `polybot_dev` role, requires background tasks
  and the API policy to remain disabled, and verifies the live PostgreSQL
  session identity through a direct read-only connection.
- The normal database-free discovery run remains safe: 45 tests pass and the
  five integration tests skip with an explicit opt-in message. The gated full
  run passes all 50 tests with deprecation warnings treated as errors.
- The integration suite verifies model import and the expected schema,
  representative Peewee reads and a rolled-back write, all nine bot extension
  loads without a Discord connection, stopped recurring task loops, a direct
  database-backed FastAPI route request without an HTTP server or API
  lifespan, draft ordering, pending-game embed generation, database-backed
  graph rendering, and promotion, demotion, and draft card rendering.
  Bullet's absent development spreadsheet configuration is supplied only by
  an in-memory test mock; no Google API connection is made.
- Every fixture write is enclosed in a rollback transaction. A separate
  post-run query confirmed zero Phase 6 game-log, team, user, game, or history
  fixture rows remained in `polytopia_dev`.
- The strict model import exposed two compatibility issues. Removed Peewee's
  obsolete and ignored `autorollback=True` database option, and changed the
  `Game.date` `DateField` default from `datetime.datetime.today` to
  `datetime.date.today` so newly created and reloaded games use the same type.
- The complete acceptance gate passed under CPython 3.12.13: all 50 gated
  tests passed, all 45 default offline tests passed with five gated skips,
  compileall passed, dependency inventory passed, frozen lock validation
  resolved 80 packages, and the diff check passed. No Discord, HTTP, Google,
  or production-database connection was made.
- PostgreSQL catalog inspection found that `polybot_dev` inherits `CONNECT`
  and `TEMP` on the production database from PostgreSQL's default `PUBLIC`
  grants, but not `CREATE`. Phase 2 installed an HBA isolation rule, which is
  the effective connection barrier. The active HBA file is root-readable only
  and could not be re-inspected without Nelluk's sudo password; no attempt was
  made to test-connect to production.

## Phase 7: live development-bot acceptance

Start only the development bot:

```bash
POLYBOT_ENV=development uv run python bot.py --skip_tasks
```

Before connecting, verify the redacted profile. Watch the development logs and
confirm the logged-in bot ID and guild are the expected development resources.

Manual acceptance checklist:

- Bot starts, reaches ready, and maintains gateway heartbeats.
- It is present only in the test guild.
- The configured development prefix works.
- Registration and a harmless database-backed lookup work.
- Team lookup and game-result embeds render.
- `$team_image` accepts an attachment, retrieves it, and uses the local file.
- `$promote` and `$demote` render with local and one-off remote images.
- `$draft` renders with a local team image.
- A representative graph renders.
- A disposable game workflow can be created and corrected in the dev database.
- No production Discord channel outside the explicitly approved shared test
  channel receives a message.
- No production database row changes.
- Development images/logs remain in development paths.
- Shutdown is clean.

Keep background jobs and API disabled until the core checklist passes. Enable
and test them separately if they are required in production.

Execution checkpoint (2026-07-28):

- Nelluk re-inspected the active PostgreSQL HBA file with sudo. Lines 97 and
  98 explicitly reject `polybot_dev` connections to production database
  `polytopia2` over both `127.0.0.1/32` and `::1/128`. PostgreSQL evaluates
  those exact rules before broader localhost authentication rules. No
  production-database connection was attempted.
- The final redacted preflight selected environment `development`, bot ID
  `479029527553638401`, database `polytopia_dev`, role `polybot_dev`, test
  guild `478571892832206869`, and isolated development image/log roots.
  Background tasks, the HTTP API, and the Bullet spreadsheet extension were
  all disabled. The minimal default-data seed found all 16 tribes already
  present, and the five gated development-database tests passed before the
  Discord connection.
- Added `bullet_enabled` to the central runtime profile. It defaults to true
  for production and false for development, is shown in redacted inspection
  output, and prevents `modules.bullet` from loading in development. This
  avoids requiring absent Google credentials or allowing the live development
  bot to access the production Bullet spreadsheet.
- The first login attempt was rejected by Discord with HTTP 401 because the
  token value in `config.development.ini` had literal surrounding quotes.
  Structural inspection printed no token content, confirmed that its encoded
  application ID matched the expected bot, and identified the quotes. Nelluk
  removed them; the next attempt authenticated as `PolyELO Bot Beta`
  (`479029527553638401`) on discord.py 2.7.1 and loaded only the approved test
  guild.
- Live acceptance in test channel `480078679930830849` covered `!guide`,
  staff registration, player lookup, pending-game creation, team creation and
  lookup, attached team-image storage, promotion/trade cards using a remote
  Discord avatar and the local team image, a draft card, leaderboard graph
  delivery, and a complete disposable two-player game. The game was filled
  through the staff third-party join path, started, renamed, rendered,
  completed, reverted with `!unwin`, and deleted. A direct development query
  confirmed game 24 and its dependent rows were removed.
- Two observed command behaviors predate this upgrade. Quotes around a
  multi-word argument to `!team` remain part of its greedy lookup string, so a
  partial unquoted lookup succeeds. `!lbteam` intentionally selects only teams
  with a non-null league tier; the new untiered team therefore produced an
  empty graph and Matplotlib's no-labelled-artists warning. The populated
  offline graph fixture rendered successfully.
- The first Ctrl-C shutdown exited with status zero but exposed an existing
  `CustomHelp.cog_unload()` error because the cog never stored its bot
  reference. Added that reference and a focused unload regression test. A
  second live development-only startup reached ready and then shut down with
  status zero and no teardown exception.
- The final gate passed under CPython 3.12.13: all 51 tests passed with the
  development-database gate enabled; the default offline run passed 46 tests
  and skipped the five gated tests; compileall, dependency inventory, frozen
  lock validation for 80 resolved packages, and `git diff --check` passed.
- Development fixture team `Phase7 Test Team` and its uploaded image remain
  intentionally in `polytopia_dev` and
  `data/development/images/teams/9.png`; they do not overlap production paths.
  The production clone remained clean at commit `43b3425`, and
  `polytopia.service` remained active throughout as PID `1388246` using
  `/home/nelluk/PolyBot39/bin/python3 /home/nelluk/PolyBot39/bot.py`. It was
  not restarted or modified.

## Phase 8: review and production cutover plan

The exact reviewed procedure is in
[`docs/PRODUCTION_CUTOVER.md`](PRODUCTION_CUTOVER.md). It includes preflight,
fresh database and image backups, fast-forward-only deployment, explicit
production policy, locked environment creation, offline verification, a
tracked systemd drop-in, one restart, smoke tests, and non-destructive
rollback.

Execution checkpoint (2026-07-28):

- Reviewed all seven focused commits from production baseline `43b3425`
  through Phase 7 commit `7b60964`. Before the Phase 8 documentation changes,
  the branch changed 29 files with 3,881 insertions and 113 deletions.
  `origin/master` is the direct ancestor, the production checkout is clean,
  and the upgrade branch is seven commits ahead.
- Validated the ignored production configuration through the new redacted
  loader without importing models or connecting to PostgreSQL or Discord. It
  selects bot `484067640302764042`, database `polytopia2` through the default
  local socket, all production guilds, background tasks, Bullet, and existing
  production image/log roots.
- The active `polytopia.service` runs the Python 3.9.20 legacy interpreter
  from `/home/nelluk/PolyBot39/bin`. The runbook keeps that environment intact
  and switches the service to `.venv/bin/python` only through a tracked
  systemd drop-in with explicit `POLYBOT_ENV=production`.
- Found `polyapi.service`, a second unit referencing the legacy environment.
  It is disabled and inactive. Its old
  `uvicorn.workers.UvicornWorker` command is not valid as the reviewed
  Gunicorn 26 deployment path, so the bot cutover keeps the API disabled and
  inactive. Any API activation is separately gated.
- Confirmed uv 0.11.32 and managed CPython 3.12.13 are installed. Current
  authoritative uv documentation and the installed CLI support
  `uv sync --locked --no-dev --python 3.12.13`; `--locked` is preferred over
  `--frozen` because it also rejects an out-of-date lock.
- Confirmed the live backup job runs at 01:00, 09:00, and 17:00 and covers a
  full custom-format PostgreSQL dump plus weekday-rotated local images. The
  reviewed full dump is about 120 MB, the image archive about 200 KB, the
  tested `.venv` about 437 MB, and the latest disk audit reports 4.9 GB free.
  The runbook nevertheless requires a fresh validated backup immediately
  before cutover.
- Retired the stale pip instructions and dependency ranges in `README.md` and
  `requirements.txt`; `pyproject.toml` and `uv.lock` are now explicitly the
  only installation sources.
- Added two network-free deployment-asset tests. The final Phase 8 gate passes
  all 53 tests with the development-database gate enabled; the default run
  passes 48 tests and skips the five gated tests. Compileall, the 80-package
  lock check, and `git diff --check` pass.
- No production file, database, service, configuration, Git reference, or
  process was changed during Phase 8 review.

Publishing or merging the branch and executing any command below the runbook's
production approval gate require separate explicit approval. PostgreSQL 12
replacement remains a separate infrastructure project.

## Rules for the execution task

- Work only in `/home/nelluk/PolyBot39-dev`.
- Do not modify `/home/nelluk/PolyBot39` from the development task.
- Do not expose or commit tokens, database passwords, or real configuration.
- Do not run production services, production commands, or production database
  migrations.
- Do not restart either bot unless Nelluk explicitly requests it.
- Do not use sudo without reading the current disk audit and obtaining approval.
- Preserve unrelated user changes.
- Use `apply_patch` for source-file edits.
- Use one phase and one dependency group at a time.
- Provide the exact test result and working-tree state at each checkpoint.
- Stop and ask if a safety check indicates mixed production/development
  resources.

## Suggested opening prompt for the new Codex task

Copy this after cloning:

> Work in `/home/nelluk/PolyBot39-dev`. Read `AGENTS.md` and
> `docs/DEPENDENCY_UPGRADE_HANDOFF.md` completely. This is the execution task
> for the planned Python 3.12/dependency upgrade. Verify the clone, branch,
> baseline tests, and that `/home/nelluk/PolyBot39` will remain untouched.
> Start with Phase 1 only: central runtime profiles and their offline tests.
> Do not install uv, create a database, connect a Discord bot, restart a
> service, or begin dependency upgrades yet. At the end, report the tests,
> diff, and next approval boundary.
