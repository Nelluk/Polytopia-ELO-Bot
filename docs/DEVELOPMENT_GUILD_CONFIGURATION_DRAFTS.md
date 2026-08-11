# Development guild-configuration drafts, activation, and rollback

P10.6b1 adds one private owner-managed inactive configuration draft for each
already enrolled development guild. P10.6b2 activates reviewed ordinary
settings and publishes a new immutable runtime snapshot. P10.6b3 restores an
earlier accepted document by cloning it into a new monotonic revision and
publishing that complete snapshot. None of these units enrolls a guild,
synchronizes commands, or authorizes production work.

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
- Draft edit writes touch only `guild_configuration_draft`. They never write the
  active registry, immutable revisions, protected audit history, runtime
  snapshot, Discord objects, or application-command tree.
- Activation requires the full current draft digest, rejects no-op and command-
  capability changes, and writes the revision, registry generation, protected
  audit, and draft expiry atomically.
- After commit, a separate read-only connection reloads the complete active
  graph. Publication requires exact committed evidence and unchanged unrelated
  guilds. A failure is committed/reconciliation-required, never rolled back.
- Rollback is owner-only and requires an exact earlier same-guild revision.
  Preview and confirmation bind the source document's full digest and the
  current active revision, generation, and digest. The commit appends a new
  complete revision and audit event; it never moves the active pointer
  backward, deletes history, or consumes a draft.

The private `/operator guild edit` workspace exposes Create/Reset, Refresh,
six section selectors, typed role/channel/category selectors, Validate,
Activate, and Discard. Validation checks the complete document and current
same-guild Discord role/channel identity. Activate is disabled until the
current nonempty draft has validated and then requires typing
`ACTIVATE <full-digest>` exactly.

The private `/operator guild rollback revision:<number>` command displays the
source and current revision evidence, changed fields, and full source digest.
It requires typing `ROLLBACK <revision> <full-source-digest>` exactly. A stale
active revision or changed source digest requires a fresh preview. A source
with different application-command capabilities and a no-op source are
rejected because this unit cannot silently drift the registered Discord tree.
An existing inactive draft is not changed; after rollback its base is stale,
so reset it before making further edits.

Activation does not synchronize application commands. A draft that changes
command capabilities may be edited but cannot activate in P10.6b2. Use the
separate reviewed command plan/apply lifecycle when a later unit coordinates
stored capability activation with Discord registration.

If the panel reports that `rN/gN` committed but runtime publication could not
be verified, do not activate again. The authoritative database has already
advanced. Run `/operator bot restart`; database-mode startup directly loads
and validates the active graph. If the process cannot start from that graph,
selecting the explicit static profile source before a guarded restart is the
transitional rollback. There is no automatic fallback.

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

The P10.6b1 database test creates, reads, replaces, and expires one draft inside one
outer transaction and always rolls that transaction back. It leaves no draft
fixture.

P10.6b2 adds a second gated real-schema proof, also only in a stopped-writer
window:

```bash
POLYBOT_ENV=development \
POLYBOT_P10_6B2_ACTIVATION_INTEGRATION=1 \
  .venv/bin/python -m unittest \
  tests.test_guild_configuration_activation_database
```

It creates an edited draft, appends and selects the active revision, verifies
the exact generation/audit and consumed draft, and then rolls the outer
transaction back. It leaves no revision, audit, generation, or draft fixture.

P10.6b3 adds a third gated proof under the same stopped-writer boundary:

```bash
POLYBOT_ENV=development \
POLYBOT_P10_6B3_ROLLBACK_INTEGRATION=1 \
  .venv/bin/python -m unittest \
  tests.test_guild_configuration_rollback_database
```

It activates a temporary ordinary-settings revision, clones the original
document into a newer rollback revision, verifies the monotonic generation,
parent/source evidence, and protected audit, and then rolls the outer
transaction back. It retains no revision, audit, generation, or draft change.
Any production schema/import remains a separately reviewed unit.
