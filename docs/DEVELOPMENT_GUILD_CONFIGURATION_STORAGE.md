# Development guild configuration storage operations

P10.3 owns the first additive PostgreSQL storage and exact static import for
dynamic guild configuration. It is development-only. P10.4 may inspect this
contract through the separate read-only startup shadow service, but neither
unit changes the runtime configuration source, enrolls arbitrary guilds,
synchronizes Discord commands, or authorizes any production operation. See
`DEVELOPMENT_GUILD_CONFIGURATION_SHADOW.md` for that later read boundary.

## Fixed safety boundary

Every live operation must resolve all of these values from the selected
runtime profile and then recheck the connected PostgreSQL session:

| Resource | Required value |
| --- | --- |
| Environment | `POLYBOT_ENV=development` |
| Database | `polytopia_dev` |
| PostgreSQL role | `polybot_dev` |
| Discord application | `479029527553638401` |
| Background tasks / API / Bullet | all disabled |
| Runtime configuration authority | static development settings |

The tool has no production mode. It contains no `DROP`, delete, truncate,
global command synchronization, enrollment, activation, or runtime cache
operation.

## Storage contract

The additive schema contains exactly three tables:

- `guild_configuration_registry` identifies the enrolled guild, its selected
  immutable revision, state, generation, and storage schema version;
- `guild_configuration_revision` stores complete schema-versioned JSONB
  documents, canonical document and source digests, ancestry, source, actor,
  and timestamp; and
- `guild_configuration_audit` records the protected initial-import event and
  later lifecycle events without becoming a second configuration source.

Primary, foreign-key, deferrable ancestry/active-revision, shape, digest,
state, and bounded-text constraints are named and verified. A partial table,
column, or constraint inventory blocks both import and verification. The
initial import creates registry revision/generation `1` and audit event `1` in
the same transaction. An exact repeated apply is a verified no-op; a changed
document, source digest, audit, registry state, or unexpected registry guild
is refused rather than silently creating a new revision.

Draft storage is intentionally deferred until the owner control plane needs
it. There is no destructive down migration. While database configuration is
not authoritative, rollback is to leave the static source selected, preserve
the additive evidence, correct the plan, and retry only a reviewed operation.

## Snapshot and offline plan

Capture the configured development guild's bounded role/channel identity while
the beta is healthy. This logs in through Discord HTTP only; it does not open a
gateway, connect to PostgreSQL, modify Discord, or include members, messages,
tokens, or permissions. The ignored snapshot is mode `0600` under a dedicated
mode-`0700` development directory.

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_storage.py snapshot
```

Then build the complete plan offline:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_storage.py plan
```

The plan performs no database or Discord connection. Review every guild ID,
document/source digest, materialized document, additive statement, and the
printed digest-bound confirmation. All legacy role names must resolve to
exactly one non-managed role in the same guild. Every stored channel/category
ID must exist with the expected broad object kind. Unknown, missing,
ambiguous, partial, or obsolete configuration fails closed.

Development `/staffhelp` is a deliberate environment-specific exception to
the static dictionary: its effective mirror is the independently pinned
`admin-spam` channel `480078679930830849`. The import materializes that actual
route when `tools_support` is enabled; it does not weaken the portable schema
rule or pretend that the legacy null value is effective behavior.

## Stopped-writer apply and verification

Do not apply while the beta is active. Stop only
`polybot-development-beta@main.service`, then run the host-wide development
writer audit and require it to report clear. Keep the exact captured snapshot
unchanged between plan and apply.

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_storage.py apply \
  --confirm 'P10.3 APPLY <exact bundle digest from plan>'
```

Apply rechecks the configured and live database/role, requires a read-write
transaction, takes a transaction-scoped advisory lock, creates only an absent
complete schema, imports all configured development guilds, verifies the
stored graph, and commits once. Any DDL, insert, constraint, identity, digest,
or verification failure rolls back the whole transaction.

Repeat the same apply command before restarting the beta. It must report no
created schema, no imported guilds, and the exact configured guild under
`unchanged_guild_ids`. Then run the independent read-only verifier:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_storage.py verify

POLYBOT_ENV=development POLYBOT_P10_3_STORAGE_INTEGRATION=1 \
  .venv/bin/python -m unittest -v \
  tests.test_guild_configuration_storage_database
```

Restart the durable beta with startup synchronization disabled and verify its
development identity, checkpoint, health, and unchanged static-authority
behavior. P10.3 does not require an application-command plan/apply or tester
announcement because it changes no command or user-visible runtime path.

## Failure disposition

- Missing or duplicate live roles/channels: correct or explicitly resolve the
  development configuration; never guess an ID.
- Partial or drifted storage schema: stop and investigate; do not use `IF NOT
  EXISTS` to accept it.
- Transaction failure: confirm rollback and inspect the three-table inventory
  before retrying.
- Exact repeat reports a mismatch: do not rewrite revision one. Diagnose the
  source/document/audit difference and plan a later explicit revision.
- Beta restart failure: leave database authority static, inspect the guarded
  service and logs, and do not weaken startup or identity checks.
