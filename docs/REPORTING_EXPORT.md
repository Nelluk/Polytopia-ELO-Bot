# DuckDB reporting export

PolyBot's DuckDB export is a reporting snapshot, not a PostgreSQL backup. Keep
the custom-format PostgreSQL dump as the private disaster-recovery artifact.

The exporter reads PostgreSQL in a single `REPEATABLE READ, READ ONLY`
transaction. It copies an explicit table-and-column allowlist, validates every
row count, closes and reopens the result read-only, and atomically publishes
the completed file. A failed export cannot truncate the last good snapshot.

The export deliberately omits:

- `apiapplication`, including API tokens;
- `configuration`;
- `gamelog`, which has its own protected-message-filtered CSV export;
- `team_server_broadcast_message`;
- free-form game notes and internal Discord message/channel metadata.

Direct player and Discord identifiers remain in the export because the current
staff reports associate game and rating history with players. Access to the
file must therefore remain limited to authorized staff and follow PolyBot's
privacy and retention policies.

## Generate a snapshot

This command uses local PostgreSQL peer authentication and does not modify the
source database:

```bash
.venv/bin/python scripts/export_reporting_duckdb.py \
  --output /tmp/polytopia_reporting.duckdb
```

For a stable publication path, add `--replace`. Replacement is atomic and the
result is mode `0600`:

```bash
.venv/bin/python scripts/export_reporting_duckdb.py \
  --output /home/nelluk/backups/polytopia_reporting.duckdb \
  --lock-file /home/nelluk/.polybot-reporting.lock \
  --replace
```

The tracked `scripts/backup_db.sh` runs this production command after all core
backup artifacts have been validated and atomically published. If reporting
export fails, the core backups and previous valid reporting snapshot remain in
place, and the script exits nonzero so cron reports the failure. The existing
Dropbox upload runs five minutes after the backup starts. The persistent lock
is kept outside the published backup directory so it is not uploaded.

## Inspect the snapshot

The snapshot includes two self-description tables:

- `reporting_metadata`: export format, DuckDB version, generation time, and
  source PostgreSQL version;
- `reporting_row_counts`: the validated source-snapshot row count for every
  exported table.

Example DuckDB queries:

```sql
SELECT * FROM reporting_metadata ORDER BY key;
SELECT * FROM reporting_row_counts ORDER BY table_name;

SELECT g.id, g.date, p.name, l.elo_change_player
FROM game AS g
JOIN lineup AS l ON l.game_id = g.id
JOIN player AS p ON p.id = l.player_id
WHERE g.is_completed
ORDER BY g.date DESC
LIMIT 100;
```

DBeaver users can create a new DuckDB connection and select the downloaded
`.duckdb` file. Consumers should query a local downloaded copy, not a file that
Dropbox is actively synchronizing.
