# Player badges migration

P12.1 adds one backward-compatible PostgreSQL column:

```sql
ALTER TABLE "public"."player"
ADD COLUMN "badges" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[];
```

The migration tools are model-free and do nothing on import or ordinary bot
startup. This is a schema-only expansion: it does not populate badges,
transform existing player records, backfill identities, or change any existing
value. Existing players receive the reviewed empty-array default.

## Development

The development tool's default mode is an offline plan:

```bash
python scripts/migrate_player_badges.py
```

Development verification and apply are separately gated operations. Stop the
development writer first and prove the exact `development` / `polytopia_dev` /
`polybot_dev` identity. Verification is read-only:

```bash
POLYBOT_ENV=development .venv/bin/python scripts/migrate_player_badges.py --verify
```

After separate approval, apply with the exact confirmation printed by the
reviewed tooling:

```bash
POLYBOT_ENV=development .venv/bin/python scripts/migrate_player_badges.py \
  --apply --confirm P12.1-DEVELOPMENT-PLAYER-BADGES-APPLY
```

Apply acquires the development writer lock and performs the additive DDL plus
exact post-apply verification in one transaction. It refuses an existing
column whose array element type, nullability, or default differs. It is safe
to rerun when the exact column already exists.

Application rollback leaves this harmless additive column in place. There is
intentionally no drop-column rollback command because awarded badges are
durable data.

## Production

The separate production tool is fixed to `POLYBOT_ENV=production` and database
`polytopia2`. Its live modes require the configured database role to equal
PostgreSQL `current_user`; the role must be nonempty, but is not hard-coded or
printed. The default plan is connection-free:

```bash
.venv/bin/python scripts/migrate_player_badges_production.py
```

Production verification is read-only and remains a separately approved
production-database access:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_badges_production.py --verify
```

After the production writer is stopped, the writer/session census is clear,
and the exact DDL is separately approved, apply uses one transaction, a
five-second lock timeout, and exact post-DDL verification:

```bash
POLYBOT_ENV=production \
.venv/bin/python scripts/migrate_player_badges_production.py \
  --apply \
  --confirm P12.1-PRODUCTION-PLAYER-BADGES-APPLY
```

The production tool refuses the development profile/database, another live
database or role, a mismatched existing column, or a missing confirmation. It
has no destructive rollback mode. Production access, verification, apply, and
deployment remain separately approval-gated by the modernization cutover.
