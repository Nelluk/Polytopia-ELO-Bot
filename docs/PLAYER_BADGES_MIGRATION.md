# Player badges migration

P12.1 adds one backward-compatible PostgreSQL column:

```sql
ALTER TABLE "public"."player"
ADD COLUMN "badges" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[];
```

The tool is model-free and does nothing on import or ordinary bot startup.
Its default mode is an offline plan:

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
durable data. Production schema apply and deployment require later, separate
approval and identity-specific tooling/review.
