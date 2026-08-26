# Game keep-active schema migration

`Game.cleanup_deferred_until` is a nullable additive PostgreSQL `DATE`. It is
authoritative extension state; `Game.date`, pending expiration, and audit text
remain unchanged. The model is intentionally loaded only after the column is
present.

The connection-free plan is safe to inspect with:

```sh
.venv/bin/python scripts/migrate_game_keep_active.py
```

Development verify/apply is gated to `POLYBOT_ENV=development`,
`polytopia_dev`, and `polybot_dev`; apply additionally requires
`P5.17-DEVELOPMENT-GAME-KEEP-ACTIVE-APPLY`. Production uses the separate
identity-checked tool and `P5.17-PRODUCTION-GAME-KEEP-ACTIVE-APPLY`.

Before either apply, stop the relevant writer, confirm the exact database and
role, take the normal backup, run the connection-free plan and read-only
verification, then apply the single additive statement and rerun verification.
No automatic startup DDL or drop-column rollback is provided. If application
deployment must be rolled back, leave the harmless nullable column in place.
