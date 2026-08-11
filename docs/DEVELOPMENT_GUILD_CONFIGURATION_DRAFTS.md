# Development guild-configuration drafts

P10.6b1 adds one private owner-managed inactive configuration draft for each
already enrolled development guild. It does not activate a revision, reload
the bot, enroll a guild, synchronize commands, or authorize production work.

## Fixed safety contract

- Exact environment/database/user: `development` / `polytopia_dev` /
  `polybot_dev`; background tasks, API, and Bullet remain disabled.
- One complete canonical document per guild, copied from the exact active
  revision and generation and bound to the configured bot owner.
- Draft lifetime is 24 hours. Reset replaces any prior or expired draft;
  discard expires the row without deleting history from authoritative tables.
- Every edit replaces the complete validated document using the observed
  draft version, document digest, base revision, base generation, and actor.
  Stale or expired controls fail closed.
- Draft writes touch only `guild_configuration_draft`. They never write the
  active registry, immutable revisions, protected audit history, runtime
  snapshot, Discord objects, or application-command tree.

The private `/operator guild edit` workspace exposes Create/Reset, Refresh,
six section selectors, typed role/channel/category selectors, Validate, and
Discard. Validation checks the complete document and current same-guild
Discord role/channel identity. There is deliberately no Activate control in
P10.6b1.

## Additive development schema gate

Generate the exact connection-free plan from the reviewed checkout:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_drafts.py plan
```

The plan opens no database or Discord connection and prints the exact
statement digest and required confirmation. Applying the one-table schema is
a separately approved live development operation. Before apply, stop only the
guarded development beta and verify the host-wide writer audit is clear. Then
run the unchanged development profile and the exact confirmation printed by
the plan:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_drafts.py apply \
  --confirm 'P10.6B1 APPLY <exact-plan-digest>'

POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_drafts.py verify
```

The apply validates the existing P10 registry/revision/audit schema, exact
live database identity, read-write transaction state, and a transaction-scoped
advisory lock. It creates only the absent exact draft table, re-verifies its
columns and constraints, and commits atomically. Partial or drifted schema is
rejected; failure rolls back. There is no destructive down migration.

## Validation cadence

Offline focused validation:

```bash
POLYBOT_ENV=development .venv/bin/python -m unittest \
  tests.test_guild_configuration_draft_storage \
  tests.test_operator_guild_configuration_drafts
```

After the separately approved schema apply, run the transactional real-schema
lifecycle only in the stopped-writer gate:

```bash
POLYBOT_ENV=development \
POLYBOT_P10_6B1_DRAFT_INTEGRATION=1 \
  .venv/bin/python -m unittest \
  tests.test_guild_configuration_draft_storage_database
```

The database test creates, reads, replaces, and expires one draft inside one
outer transaction and always rolls that transaction back. It leaves no draft
fixture. Activation, rollback-to-revision, runtime reconciliation, and any
production schema/import remain later separately reviewed units.
