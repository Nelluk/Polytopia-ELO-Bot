# Durable development-beta operations

WB1.2 provides a guarded, user-level service and an explicit release
announcement path for the development wider beta. This document is an
operator runbook, not authorization to perform a live rollout. The service,
command deployment, role resolution, release delivery, and any database
fixture operation remain separately approved actions.

## Fixed safety contract

The durable service may execute only this reviewed development target:

| Resource | Required value |
| --- | --- |
| Checkout and working directory | `/home/nelluk/PolyBot39-dev` |
| Interpreter | `/home/nelluk/PolyBot39-dev/.venv/bin/python` |
| Environment | `POLYBOT_ENV=development` |
| Beta application | `479029527553638401` |
| Allowed guild | `478571892832206869` |
| Database / role | `polytopia_dev` / `polybot_dev` |
| Runtime flags | `--skip_tasks`; background tasks, API, and Bullet disabled |
| Public release channel | `todo-and-changelog` / `481779940124000256` |
| Private `/staffhelp` mirror | `admin-spam` / `480078679930830849` |
| Tester role | name `testers`; ID is resolved and persisted only by the approved live role step |

The service refuses a different application, guild set, database, role,
profile, checkout, dirty Git tree, runtime flag, or startup-sync setting. Its
launcher holds `logs/development/beta-operations/beta-writer.lock` across the
bot `exec`, so a second guarded development writer exits before Discord or
PostgreSQL work. This is process coordination for the durable beta; it does
not make it safe to run an unguarded `bot.py` process or a production process
against the same database.

The bot's startup path never calls application-command synchronization. A
reconnect, crash restart, or ordinary service restart therefore cannot publish
a release notice. Release delivery is reachable only through the explicit
local control request described below.

## Mandatory first-activation process gate

The file lock protects only processes launched through the guarded wrapper. It
cannot see or stop an already-running unguarded `bot.py --skip_tasks` process
from an older terminal, PTY, Codex task worktree, or manually copied command.
The first service activation must therefore complete a host-wide, read-only
audit and an operator-reviewed stop gate before `enable --now`:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
/home/nelluk/PolyBot39-dev/scripts/audit_development_beta_processes.py \
--require-clear
```

If the audit reports a candidate, inspect each reported PID before taking any
action:

```bash
ps -o pid=,ppid=,user=,lstart=,args= -p PID
readlink /proc/PID/cwd
```

Only after confirming that a PID is the authorized development beta may the
operator stop that exact process cleanly:

```bash
kill -INT PID
```

Do not use `pkill`, `killall`, wildcard matching, or a broad process-kill
command. Never stop a candidate classified as production; do not stop an
unknown candidate until its ownership and checkout are resolved. Rerun the
read-only audit and require a clear result before continuing. The systemd
template repeats this gate with `ExecStartPre`, but it intentionally fails
closed rather than stopping a process for the operator.

## Install and run the user service later

WB1.2 did not install, reload, enable, start, stop, or restart this service.
After the mandatory process gate is clear and a separate approval is granted,
Nelluk can install the repository template as a user unit:

```bash
mkdir -p /home/nelluk/.config/systemd/user
install -m 0644 \
  /home/nelluk/PolyBot39-dev/deploy/systemd/polybot-development-beta@.service \
  /home/nelluk/.config/systemd/user/polybot-development-beta@.service
systemctl --user daemon-reload
systemctl --user enable --now polybot-development-beta@main.service
```

The `@main` instance name is an operator label only; it does not select a
different checkout, guild, database, or bot. Do not edit the `ExecStart`,
environment, or working-directory values to point at another profile.

Read-only availability checks are safe before that approval:

```bash
systemctl --user --no-pager status
loginctl show-user nelluk -p Linger
systemctl --user --no-pager status polybot-development-beta@main.service
```

If the user manager is unavailable after the service has been separately
approved, the exact later privileged action is:

```bash
sudo loginctl enable-linger nelluk
```

That action is not part of WB1.2. Before requesting it, inspect
`/home/nelluk/disk-audit-latest.txt` as required by the server instructions;
do not run it from this runbook automatically.

Useful operator commands after installation and approval:

```bash
systemctl --user status polybot-development-beta@main.service --no-pager
journalctl --user -u polybot-development-beta@main.service --since today --no-pager
journalctl --user -u polybot-development-beta@main.service -f
systemctl --user stop polybot-development-beta@main.service
systemctl --user restart polybot-development-beta@main.service
```

Stop is the clean shutdown path. `KillSignal=SIGINT`, a 45-second stop
timeout, `Restart=on-failure`, a five-second backoff, and a five-starts-per-
minute limit are deliberate. A restart is not a release operation.

## Checkpoint and rollback policy

The service launcher requires a clean development checkout and records the
full `git rev-parse --verify HEAD` value in `POLYBOT_BETA_CHECKPOINT` before
starting the bot. A release manifest must match that exact checkpoint, not a
branch name, abbreviated SHA, or current unreviewed working tree.

For an approved rollback:

1. Stop the user service and verify host-wide that no other development beta
   writer is running; never stop or alter a production process.
2. Move only `/home/nelluk/PolyBot39-dev` to the reviewed rollback checkpoint
   using the normal Git review process. Do not use the production checkout.
3. Run the read-only runtime check and `git status --short --branch`; the
   launcher must see a clean tree before it will start.
4. Start the approved service instance and inspect its journal and identity.
5. Handle application-command state separately through the P8.0 plan/apply
   workflow if the rollback changes commands.

The service never performs a database rollback, schema migration, command
sync, fixture seed/cleanup, or production operation.

## Application-command deployment remains separate

Stop the beta before planning a command-tree change. Review the offline plan:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_application_commands.py \
  --environment development \
  --mode plan \
  --guild-ids 478571892832206869
```

Remote inspection and apply require their separate approval and the exact
P8.0 confirmations:

```text
--confirm-environment development
--confirm-guild-ids 478571892832206869
--confirm-scope guild
--confirm-no-global-sync
```

Only after the reviewed guild-scoped operation is complete may the service be
started. A bot restart never implies a command synchronization, and there is
no global fallback.

## Reviewed release manifests

The tracked `release-manifests/template.json` is the schema/template source;
it is not a release announcement and no release-specific manifest is committed
with the code checkpoint. A committed release file cannot safely contain the
hash of the commit that contains itself. WB1.2 therefore uses a two-stage
ignored operational manifest:

1. `init` copies the tracked template into a mode-0600 draft beneath the
   ignored development operation root.
2. The operator edits only that ignored draft and leaves its all-zero
   `expected_checkpoint` placeholder unchanged.
3. `prepare` requires a clean checkout, reads the exact current HEAD, injects
   that checkpoint into a new mode-0600 prepared manifest, and atomically
   archives the final manifest and fingerprint in release state.
4. `validate` and `deliver` accept only that archived prepared manifest. They
   recheck the current clean HEAD, and the authenticated beta process checks
   the prepared checkpoint against its own startup checkpoint.

This keeps the reviewed content bounded and repository-backed by the tracked
template while ensuring that creating or editing the operational manifest does
not change the Git checkpoint it records. The ignored draft and prepared paths
are:

```text
logs/development/beta-operations/drafts/<release-id>.json
logs/development/beta-operations/prepared/<release-id>.json
```

The prepared manifest uses this exact schema:

```json
{
  "schema_version": 1,
  "release_id": "2026-08-03-wb1-2-minor",
  "expected_checkpoint": "0123456789abcdef0123456789abcdef01234567",
  "title": "Short release title",
  "bounded_summary": "One bounded summary paragraph.",
  "changed_commands": ["/game show"],
  "known_limitations": ["One bounded limitation."],
  "smoke_test_checklist": ["Run the reviewed smoke test."],
  "ping_testers": false
}
```

Validation is strict and dependency-free: no missing or unknown fields,
lowercase release ID, full 40-character lowercase Git SHA, bounded single-line
text, at most 12 changed commands, 8 limitations, and 12 checklist items, and
at least one checklist item. Command references are bounded slash/prefix
references. The rendered one-message announcement must remain below the
Discord content limit. `ping_testers` must be a JSON boolean.

Minor releases set `ping_testers` to `false`. A major/tester-facing release
may set it to `true`, but the role gate below must already be complete.

Prepare a reviewed release without Discord or PostgreSQL:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_release.py init \
  --release-id 2026-08-03-wb1-2-minor

# Edit only the printed ignored drafts/<release-id>.json path. Keep the
# expected_checkpoint value as 40 zeroes while reviewing the bounded content.

POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_release.py prepare \
  --manifest logs/development/beta-operations/drafts/2026-08-03-wb1-2-minor.json
```

The `prepare` output identifies the archived prepared path and fingerprint.
Validate that prepared artifact against the same clean checkout:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_release.py validate \
  --manifest logs/development/beta-operations/prepared/2026-08-03-wb1-2-minor.json
```

Both `init`, `prepare`, and `validate` require the clean reviewed checkout;
`prepare` is the only step that injects the exact current HEAD. These commands
do not create a client, open a database connection, or post anything. Do not
edit `release-manifests/template.json` for a single rollout, and do not put a
release-specific file under the tracked manifest directory.

## Tester role resolution and explicit delivery

The role ID is intentionally not in source, the service template, or a
manifest. During a separately approved live plan/apply step, with the
authenticated beta ready and stopped/restarted only as separately authorized,
resolve the role exactly once:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_release.py resolve-tester-role
```

The existing beta process inspects only guild `478571892832206869`, requires
exactly one role named `testers`, and atomically persists its reviewed ID in
the ignored, mode-0600 file
`logs/development/beta-operations/tester-role.json`. No ID is guessed. A
missing, duplicate, renamed, or changed role blocks a pinged release. A
minor release does not mention or resolve the role and uses
`AllowedMentions.none()`.

After a reviewed manifest and checkpoint are present, delivery is another
explicit operator action:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_release.py deliver \
  --manifest logs/development/beta-operations/prepared/2026-08-03-wb1-2-minor.json
```

The utility sends one JSON request over the local mode-0600 Unix socket to the
already-authenticated beta process. It does not instantiate a second Discord
client. The process revalidates the startup checkpoint, authenticated bot
ID, exact guild, exact public channel name/ID, and (when requested) the
persisted role ID and live unique role. The public payload is built only from
the release manifest: it contains the title, bounded summary, changed
commands, limitations, checklist, release ID, and checkpoint. It never reads
or relays `/staffhelp` report details.

The private `/staffhelp` mirror remains independently fixed to `admin-spam`
(`480078679930830849`). Its structured report content is sent only there by
the WB1.1 post-write mirror. It is not an input to, or fallback destination
for, the public release announcement in `todo-and-changelog`
(`481779940124000256`).

## Idempotency, retry, and state

Release state is stored under the ignored development operation root in
`logs/development/beta-operations/release-state.json`. Directory mode is
0700, state files are 0600, and writes use a temporary file, flush/fsync,
atomic replace, and directory fsync. User content never becomes a path.

The state machine is one-shot per `release_id`:

- `posted` returns `already-posted` on every later explicit request and never
  sends again;
- a certain rejected send records a retryable `failed` state, so a later
  explicit retry can post once and then become `posted`;
- an uncertain send remains `posting` and blocks retry until a bounded history
  scan finds the visible release marker. A found marker records success without
  reposting. If history cannot be checked, or the state remains uncertain and
  no marker is found, the operation remains blocked and requires deliberate
  operator reconciliation; it is not automatically recoverable and must not be
  forced by deleting state or reposting;
- a release ID cannot be reused with a different manifest fingerprint.

This protects against duplicate success after a crash between Discord's
acceptance and the local state write, while explicitly acknowledging that some
crash outcomes cannot be proved from bounded history. There is no automatic
retry on reconnect or service restart. Use `status` for local state inspection:

```bash
POLYBOT_ENV=development \
/home/nelluk/PolyBot39-dev/.venv/bin/python \
scripts/manage_beta_release.py status
```

## Feedback, fixtures, and incident handling

Read `/staffhelp` reports with the WB1.1 read-only utility. Treat the
development feedback root as sensitive; retention/redaction and fixture
cleanup remain separately approved filesystem/database actions. Do not seed
or clean `polytopia_dev` fixtures while the beta writer is running. Use
`docs/DEVELOPMENT_BETA_FIXTURES.md` and its existing exact identity gates.

If the service fails its preflight, inspect the journal and profile output;
do not weaken a guard. If a release is blocked by a wrong identity, channel,
role, checkpoint, manifest, or state file, correct the reviewed source/state
through the approved workflow and retry the bounded operation. Do not delete
state to force a duplicate post.

WB1.2 validation is offline only. No service installation or state creation,
Discord inspection/post, command apply, database mutation, sudo action,
dependency installation, production access, push, or merge is implied by this
document.
