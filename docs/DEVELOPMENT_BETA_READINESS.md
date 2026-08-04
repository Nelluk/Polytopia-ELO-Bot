# Development wider-beta readiness

WB1.3a provides a read-only readiness inventory and an offline desired-state
planner for the development wider beta. It is a review aid, not a rollout
mechanism. It does not create, rename, edit, delete, invite, synchronize, seed,
clean, recalculate, restart, or apply anything.

The fixed scope is:

| Resource | Required value |
| --- | --- |
| Environment | `POLYBOT_ENV=development` |
| Application | `479029527553638401` |
| Guild | `478571892832206869` |
| Database / role | `polytopia_dev` / `polybot_dev` |
| Tester role | `testers` / pinned ID `480905534019731476` |
| Public release channel | `todo-and-changelog` / `481779940124000256` |
| Private staffhelp mirror | `admin-spam` / `480078679930830849` |

The durable beta remains the single development writer. WB1.3a did not inspect,
stop, restart, or alter the currently running beta.

## Inventory surfaces

### Discord inventory through the authenticated beta

The beta-control request operation is `readiness-inventory`. It uses the
existing protected mode-0600 local Unix socket and the already-authenticated
Discord client inside the beta. The CLI never creates a second Discord client:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_readiness.py --json discord-inventory
```

The response is a deterministic primitive-only JSON snapshot. It contains:

- the exact bot, guild, application, and development-profile target identity;
- all cached guild roles within the fixed bound, with ID, name, managed flag,
  position, cached member count, permission bits, and display flags;
- cached categories and non-category channels within fixed bounds, with ID,
  name, type, category, position, bot permission bits, and bounded permission
  overwrite metadata;
- exact verification of `todo-and-changelog` and `admin-spam` by guild, ID,
  and name;
- exact verification of one `testers` role and its pinned local ID; and
- the current development-guild application-command capability assignment.

It never reads channel history or staffhelp report bodies. It omits member
lists, member names, message content, attachment bytes, tokens, and private
staffhelp details. Member-specific permission overwrites retain only their
permission shape and not the member identity. A missing/wrong bot, guild,
fixed channel, duplicate/missing tester role, pinned-ID mismatch, oversized
response, or unsafe socket fails closed. The request and response are both
bounded; the operation has no Discord mutation method. The inventory payload
and saved snapshot are limited to 256 KiB. The successful socket response is
limited to 257 KiB, leaving a strict 1 KiB envelope for the control response;
the client uses that same response limit. The documented `--json` output does
not add a trailing byte, so an exact-bound payload remains loadable.

The inventory is cache-based. A missing cached fixed channel is a refusal, not
a reason to perform an unbounded fetch. A role member count or permission bit
is `null` when the safe cached value is unavailable.

### Development database inventory

Database inventory is a separate CLI-only process; it is not reachable through
the bot socket and cannot block the bot event loop:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_readiness.py --json database-inventory
```

Before opening a connection it requires exactly the development environment,
`polytopia_dev`, `polybot_dev`, one allowed guild (`478571892832206869`), and
disabled background tasks, API, and Bullet integration. The live PostgreSQL
session must independently report `current_database() = polytopia_dev` and
`current_user = polybot_dev`.

The connection is owned by the CLI process and a worker-local Peewee-style
`connection_context()`. Its first transaction statement is `SET TRANSACTION
READ ONLY`; all later statements are bounded `SELECT`s. The implementation
does not import `modules.models`, so the inventory path cannot trigger the
legacy model-import table-creation check.

The snapshot includes bounded team and house identifiers, team player counts,
role-binding identifiers derived from `team.name` and `house.name` (the
database does not store Discord role IDs), player/team/house/game counts, and
owned fixture summaries. It recognizes the existing marked game fixtures
(`149`/`150`/`151` when unchanged) and the separately owned leaderboard
showcase (24 players and 48 games, currently IDs `200`–`247` when unchanged).
It does not return arbitrary game notes, member lists, report text, tokens, or
fixture write authority. Profile, live database, connection, and query-shape
failures refuse the operation. Team and house counts are exact, so their
truncation flags mean `count > limit`; fixture queries fetch one extra row,
return at most the fixed limit, and set `truncated` only when that extra row
is observed.

A focused gated real-database inventory test passed after an independent
preflight confirmed `POLYBOT_ENV=development`, `polytopia_dev`, and
`polybot_dev`. It performed only the inventory's read-only transaction and
representative shape assertions; no fixture, schema, recalculation, or other
database mutation was run.

## Desired-state manifest and offline planning

The repository-backed template is
[`readiness-manifests/template.json`](/home/nelluk/.codex/worktrees/8d96/PolyBot39-dev/readiness-manifests/template.json).
Its schema is implemented and strictly validated by
`modules.beta_readiness.validate_readiness_manifest`. It has bounded lists and
text, exact target identities, safe fixed-channel and tester-role fields, and
explicit sections for:

- current/proposed capability assignments, with `tools_support` represented as
  an unresolved optional decision;
- proposed development-only teams, houses, and exact role bindings, with no
  names selected in the template;
- fixture families to retain or clean up;
- cleanup and rollback steps; and
- the bounded smoke checklist, invitation prerequisites, and the 5–20 tester
  range.

Validate it without selecting a runtime profile or opening a database:

```bash
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_readiness.py --json validate \
  --manifest readiness-manifests/template.json
```

To compare reviewed JSON snapshots saved beneath the repository, run:

```bash
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_readiness.py --json plan \
  --manifest readiness-manifests/template.json \
  --discord-inventory readiness-snapshots/discord.json \
  --database-inventory readiness-snapshots/database.json
```

The planner returns sorted errors, unresolved decisions, capability additions
and removals, channel/role mismatches, proposed team/house and role-binding
differences, fixture retention differences, and cleanup/rollback/smoke plans.
It always reports `ready_for_live_apply: false`; it has no apply or remote
mutation path. Input files must be repository-backed relative JSON files with
no traversal or symlink path components. Manifest files are limited to 128 KiB;
inventory snapshots are limited to 256 KiB.

The template intentionally leaves policy choices unresolved. A valid plan is
not approval to choose names, add `tools_support`, create database records,
create Discord roles/channels, or invite testers.

## Review and later live boundaries

WB1.3a ends after inventory, validation, and offline diff review. A later
explicit unit must be approved for each live action. The later unit must keep
these boundaries:

1. Capture the Discord inventory from the running authenticated beta and the
   database inventory from a separately gated CLI process. Save the exact
   snapshots and the reviewed manifest under repository-backed, non-sensitive
   paths. Do not put report bodies or tokens in snapshots.
2. Review every target identity, fixed channel, unique tester role/pinned ID,
   capability diff, proposed team/house name, role binding, fixture ID,
   cleanup step, rollback checkpoint, smoke item, and invitation prerequisite.
   Resolve every `unresolved` item explicitly. No operator or tool may infer a
   team/house name or capability policy.
3. If roles, channels, teams, houses, or bindings are approved, use a separate
   development-only apply implementation with exact reviewed inputs. There is
   no generic remote apply request in WB1.3a. Fixture creation/cleanup remains
   under the existing exact gates and requires the beta writer to be stopped
   when the fixture runbook requires exclusivity.
4. If application-command capabilities change, stop the beta as required by
   the deployment runbook, run the offline `manage_application_commands.py`
   plan, obtain the exact guild-scoped apply approval, and verify that global
   synchronization is impossible. Adding `tools_support` would expose
   `/staffhelp`; it is not implied by this readiness plan.
5. Start or use exactly one reviewed durable development beta only after its
   separate lifecycle approval. Resolve/pin the `testers` role through the
   existing explicit role step, deliver only a reviewed release manifest to
   `todo-and-changelog`, and perform the bounded smoke checklist. The private
   `admin-spam` mirror is never a public release fallback.
6. Invite only 5–20 testers after the manifest diff, live apply evidence,
   healthy-beta evidence, smoke results, and rollback ownership have all been
   reviewed. Invitation is a separate explicit Discord action and is not
   available in the CLI.

### Precise later live inventory procedure

When a later task is explicitly authorized to capture live state, and the
durable beta is already healthy and authenticated, run these two read-only
commands from the development worktree. Do not launch a second bot and do not
change the service:

```bash
mkdir -p readiness-snapshots

POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_readiness.py --json discord-inventory \
  > readiness-snapshots/discord.json

POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_readiness.py --json database-inventory \
  > readiness-snapshots/database.json

/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_readiness.py --json plan \
  --manifest readiness-manifests/template.json \
  --discord-inventory readiness-snapshots/discord.json \
  --database-inventory readiness-snapshots/database.json
```

The redirection creates local snapshot files only; the two operations remain
remote/database read-only. Before running this procedure, confirm the exact
development profile and that no production checkout, production database,
global command sync, fixture mutation, or service lifecycle action is being
selected. The resulting plan is the review artifact for a later apply task;
it is not itself a live readiness approval.

## Validation boundary

Readiness planning and transport validation are offline. Focused readiness
tests cover deterministic and
bounded DTOs, privacy redaction, identity/channel/role refusal, protected
socket dispatch, request/response bounds, database identity and connection
lifecycle, manifest schema/path safety, deterministic diffs, capability
changes, and the absence of apply behavior. The full offline suite remains
the authoritative regression check. The separately gated real-database
inventory test also passed under the exact development identity gate. No
Discord smoke, tester invitation, command synchronization, fixture mutation,
production action, dependency installation, push, or merge is implied by this
document.
