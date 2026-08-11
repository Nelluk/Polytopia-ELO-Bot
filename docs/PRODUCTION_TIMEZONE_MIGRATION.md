# Production timezone-column migration

This runbook documents the B1/R-003 additive schema tool. It is not standing
authorization to access or modify the production database. Production verify
and apply require separate explicit approval and are embedded in the reviewed
ordering at `docs/MODERNIZATION_PRODUCTION_CUTOVER.md`.

## Fixed scope

The only production target is database `polytopia2`, using the database role
configured by the explicit production runtime profile and verified again with
PostgreSQL `current_database()` and `current_user`.

The only permitted DDL is:

```sql
ALTER TABLE "public"."discordmember" ADD COLUMN "timezone_offset_minutes" SMALLINT NULL;
ALTER TABLE "public"."discordmember" ADD COLUMN "timezone_offset_cleared" BOOLEAN NOT NULL DEFAULT FALSE;
```

The tool does not backfill or change `timezone_offset`, start application code,
operate Discord, or expose a rollback/drop mode. Code and configuration
rollback leave both harmless additive columns in place.

## Connection-free review

The default mode loads no runtime profile and opens no database connection:

```bash
.venv/bin/python scripts/migrate_player_timezone_production.py
```

It prints the exact additive plan and the non-destructive rollback
disposition. This is the only mode authorized merely by repository review.

## Read-only verification

After separate production-access approval:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_timezone_production.py --verify
```

Verify loads the production profile without creating directories, requires
configured database `polytopia2`, opens one connection, starts with `SET
TRANSACTION READ ONLY`, validates the live database and configured role, and
inspects the table, types, nullability, and defaults. It rolls the read-only
transaction back before closing. Exit `0` means both columns exactly match;
exit `1` means reviewed additive statements remain; exit `2` means identity,
schema, configuration, connection, or other safety validation failed.

## Apply gate

Do not run apply until the separately reviewed B2 runbook has verified a fresh
backup, stopped the production writer, proved no second writer exists, pinned
the exact release/rollback checkpoints, and received explicit production DDL
approval.

The reviewed apply invocation is:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_timezone_production.py \
  --apply \
  --confirm P9-B1-PRODUCTION-TIMEZONE-APPLY
```

Before connecting, apply requires the exact environment, acknowledgement,
configured `polytopia2` database, and nonempty configured role. Inside one
transaction it verifies live identity, inspects existing schema, executes only
missing reviewed schema-qualified statements under a five-second lock timeout,
and re-inspects the exact type/null/default state before commit. Any identity,
metadata, lock, DDL, or post-DDL verification failure rolls the transaction
back. Repeating apply after success executes no DDL and remains idempotent.

Run the read-only verify mode again after apply. Do not start modernization
model code until verification passes. Do not improvise `DROP COLUMN` as an
emergency rollback; stop the new code and restore the reviewed code/config
checkpoint while retaining the additive columns.
