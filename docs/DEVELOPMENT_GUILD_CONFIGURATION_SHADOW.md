# Development guild configuration shadow reads

P10.4 adds one read-only comparison between the development database revision
created by P10.3 and the effective static development settings. It gathers
the ready Discord cache's bounded role/channel identity, resolves legacy role
names, loads stored rows on a dedicated worker-owned PostgreSQL connection,
and publishes only an immutable health result. Static settings remain the
sole runtime authority.

## Fixed boundary

The shadow path accepts only the exact development profile:

- `POLYBOT_ENV=development`;
- database `polytopia_dev` and role `polybot_dev`;
- application `479029527553638401`;
- background tasks, API, and Bullet disabled; and
- the configured development guild allowlist.

The connection is read-only/autocommit, has bounded connection, statement,
and lock waits, validates live database identity and the exact three-table
storage inventory, closes in the worker, and returns frozen primitive/value
objects. Cancellation drains the submitted worker before propagating. The
event loop never receives a connection, cursor, lazy query, or Peewee model.

The comparison runs on the first ready cycle after authenticated identity and
the ordinary startup schema preflight. Reconnects reuse the first result; it
does not poll and no command performs a configuration query. The ready cache
snapshot contains no members, messages, permissions, token, or gateway state.

## Status contract

The runtime records and logs one of these states:

| Status | Meaning | Authority / promotion disposition |
| --- | --- | --- |
| `matched` | Every effective static document exactly equals its active stored document and every configured guild is active. | Static remains authoritative; P10.5 may be considered. |
| `mismatch` | Guild inventory, enrollment state, or one or more normalized document paths differ. | Static remains authoritative; promotion is blocked until reconciled. |
| `malformed` | Runtime identity, Discord references, storage schema, registry/revision graph, JSON document, or digest is invalid. | Static remains authoritative; promotion is blocked and the stored/static source must be investigated. |
| `unavailable` | The bounded read-only connection or query became unavailable. | Static remains authoritative; promotion is blocked. |

Only `matched` sets `promotion_ready=true`. Source-provenance digest drift by
itself does not create a semantic mismatch: P10.4 compares the fully
materialized effective document, while P10.3 retains source digests as import
evidence. Stored documents and their own digests are always revalidated.

Failure output is bounded and redacted to status/reason codes, guild IDs, and
changed document paths. Unexpected implementation failures retain a traceback
only in the host log and publish the safe `shadow_runtime_failure` reason.
None of these states silently changes `settings.config` or `guild_setting()`.

## Validation and beta operation

The explicit development-database regression requires the unchanged profile
gate and the reviewed private P10.3 Discord snapshot:

```bash
POLYBOT_ENV=development \
POLYBOT_P10_4_SHADOW_INTEGRATION=1 \
POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT=/home/nelluk/PolyBot39-dev/logs/development/guild-configuration/discord-snapshot.json \
  .venv/bin/python -m unittest -v \
  tests.test_guild_configuration_shadow_database
```

Use the established stopped-writer gate when batching this with the complete
development PostgreSQL suite. For beta deployment, restart only the durable
development service from a clean reviewed accumulation checkpoint and verify:

1. exactly one host-wide development writer and zero restart churn;
2. the authenticated development application and expected checkpoint;
3. one `status=matched promotion_ready=true` shadow log line;
4. protected Beta Lab readiness remains healthy; and
5. ordinary prefix/slash behavior still uses static settings.

P10.4 changes no command shape, so it requires no application-command plan or
guild apply. It is operator-visible startup diagnostics rather than a tester
feature; no tester announcement is warranted. A mismatch or failure is fixed
forward while static authority remains selected. No schema, data, Discord,
production, or global-command mutation is part of this runbook.
