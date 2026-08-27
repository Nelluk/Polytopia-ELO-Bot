# Development historical PolyChampions mirror

This is a separately approved, development-only refresh procedure. It does
not authorize a production backup, database replacement, migration, beta
restart, Discord action, deployment, or tester communication. The source
guild is `447883341463814144`; the sole configured beta target is
`478571892832206869`. The remap tool parks existing target rows at the fixed
positive BIGINT sentinel `9223372036854770000` and never treats that sentinel
as a Discord guild.

## Required sequence

1. Obtain separate production approval for a validated *partial* logical dump.
   The partial dump intentionally excludes production `gamelog`; do not
   import production logs. Validate the archive catalog and checksum before
   using it. The exact production backup command, credentials, and dump path
   are operator-specific placeholders and are not supplied by this runbook.

   On the GreenCloud host, production runs as the `polyelo-production` Compose
   project from `/srv/polyelo/PolyBot39`; a disabled legacy `polyelo.service`
   may remain pending separate host cleanup but is unsupported by current
   `master`. The maintained host `polyelo-backup.service`
   publishes the partial archive as
   `/srv/polyelo/backups/polytopia_bak-<weekday>.sqlc`; that private directory
   is owned by the `polyelo` account, so copying an archive into development
   staging is a separate privileged operator action.

2. Stop the beta under its own approval. Run the host-wide development writer
   census and resolve every reported PID; do not use `pkill`, `killall`, or a
   broad stop. The existing read-only census is:

   ```bash
   POLYBOT_ENV=development \
   /home/nelluk/PolyBot39-beta/.venv/bin/python \
   scripts/audit_development_beta_processes.py --require-clear
   ```

   Stop the direct Compose beta first with `docker compose stop bot` from
   `/home/nelluk/PolyBot39-beta`. A sandboxed process view can miss container
   processes, so perform the final census from a host-wide operator context.

3. Preserve rollback material before replacing the development database.
   Use the host's reviewed logical PostgreSQL backup procedure for
   `polytopia_dev`, validate the custom archive with `pg_restore --list`, retain
   its SHA-256 sidecar off-host, and separately preserve the five current
   `guild_configuration_*` tables: registry, revision, audit, draft, and
   delegation. These tables hold the beta's active authority and are restored
   after the production partial dump. Their backup/export is an operator
   PostgreSQL-admin action; the mirror tool does not export or rewrite them.

4. With the beta stopped and no unguarded writer, replace only the configured
   development `polytopia_dev` database under separate approval. Prefer a fresh
   database/role target when the operator has database-administrator access.
   On GreenCloud, `polybot_dev` owns the database but intentionally lacks
   `CREATEDB` and does not own the `public` schema. The bounded fallback is to
   drop the known production-domain tables plus `gamelog` with `CASCADE`, then
   restore the validated archive in one transaction with `--no-owner --no-acl`.
   Preserve the five `guild_configuration_*` tables until their separate exact
   restore. Never point this operation at `polytopia2`, a production host, or
   any other database.

   Validate the archive catalog first (for a custom archive):

   ```bash
   pg_restore --list VALIDATED_PARTIAL.dump
   pg_restore --no-owner --no-acl --single-transaction \
     --dbname=polytopia_dev VALIDATED_PARTIAL.dump
   ```

   The commands above are illustrative admin commands, not permission to run
   them from Codex. Record the archive name, checksum, PostgreSQL major, and
   bounded restore result separately; the remap digest does not prove which
   archive produced the live database.

   Current partial archives exclude the `gamelog` table and data but may retain
   an orphan `gamelog_id_seq` catalog entry. Remove that sequence after the
   production-domain restore and before schema bootstrap; bootstrap must create
   the empty table and its sequence together.

5. Recreate schema omitted by the partial dump and apply current development
   schema-forward work. The existing guarded schema plan/apply is:

   ```bash
   POLYBOT_ENV=development \
   /home/nelluk/PolyBot39-beta/.venv/bin/python \
   scripts/bootstrap_development_database.py
   # review its exact token, then rerun with --apply --confirm TOKEN
   ```

   This creates missing model tables and the deferred winner foreign key. It
   recreates an empty `gamelog` table when the partial archive omitted it; no
   production GameLog content is expected. If the current development schema
   requires the existing timezone migration, run its read-only plan and its
   separately approved exact apply (`scripts/migrate_player_timezone.py`).
   Restore the preserved five `guild_configuration_*` tables and validate
   their complete topology. Do not remap, redigest, or merge those tables.
   The mirror plan reports configuration `not_ready` when all five are absent,
   refuses a partial topology, and apply requires one active target-guild
   authority row when the complete topology is present.

6. From this clean development checkout, run the read-only mirror plan. It
   verifies the exact development profile (`polytopia_dev`/`polybot_dev`, one
   target guild, API/background tasks/Bullet disabled), checks the live
   database identity, fingerprints the touched schema, counts source/target/
   parking rows and scrub candidates, and prints the exact confirmation:

   ```bash
   POLYBOT_ENV=development \
   /home/nelluk/PolyBot39-beta/.venv/bin/python \
   scripts/manage_historical_mirror.py plan
   ```

   Review that the parking counts are zero. A row already using
   `9223372036854770000` blocks the operation. Keep the complete confirmation
   string; it includes the digest and bounded source/target/parking baselines.

7. After separate approval, apply that exact confirmation while the beta is
   still stopped and the host-wide census remains clear:

   ```bash
   POLYBOT_ENV=development \
   /home/nelluk/PolyBot39-beta/.venv/bin/python \
   scripts/manage_historical_mirror.py apply \
   --confirm 'HISTORICAL MIRROR APPLY ...'
   ```

   Apply reacquires the live identity and plan under the existing
   `BetaDatabaseWriterLock`, revalidates the digest and all pre-state counts
   inside one transaction, parks target rows first, then remaps source rows.
   It preserves global `DiscordMember`, `House`, and `Tribe` rows and leaves
   indirect `GameSide`, `Lineup`, `SquadMember`, auction/bid, and preference
   foreign keys unchanged. It preserves pending and unconfirmed game state.
   It clears only remapped target game/side/team Discord object references,
   deletes broadcasts for remapped games, clears legacy draft announcement
   fields to JSON null while retaining other object fields, and deletes every
   `ApiApplication` row/token. Any pre-commit error rolls back completely.
   A post-commit verification failure reports reconciliation required; it
   does not pretend an inverse rollback exists.

8. Run read-only verification with the same exact confirmation:

   ```bash
   POLYBOT_ENV=development \
   /home/nelluk/PolyBot39-beta/.venv/bin/python \
   scripts/manage_historical_mirror.py verify \
   --confirm 'HISTORICAL MIRROR APPLY ...'
   ```

   Verification proves no direct source rows remain; target and parking
   counts equal the planned source and pre-existing target counts; target
   players, teams, games, hosts, lineups, sides, squads, and squad members
   are guild-consistent; each winner belongs to its game; scrubbed fields,
   target broadcasts, and API applications are absent; and modern
   configuration is target-only and active when present. Historical
   cross-guild anomalies fail with bounded IDs for manual investigation rather
   than silently rewriting unrelated data.

9. Before plan/apply, restore the preserved five-table beta configuration
   authority and validate that it is active and target-only. After verify,
   revalidate that configuration; do not restore it after the mirror. Then
   validate the development profile and disabled integrations, and obtain a
   separate approval to start the beta:

   ```bash
   POLYBOT_ENV=development \
   /home/nelluk/PolyBot39-beta/.venv/bin/python \
   scripts/check_runtime_config.py
   cd /home/nelluk/PolyBot39-beta
   docker compose up -d bot
   ```

   The durable launcher itself receives its required `--skip_tasks` argument
   from the installed service unit; do not invoke it manually without the
   complete reviewed service environment. Startup must show
   `polytopia_dev`/`polybot_dev`, one configured guild,
   disabled API/background tasks/Bullet, and no automatic command sync. A
   bounded operator smoke is separately approved; it may exercise identity,
   a read-only historical game view, and one agreed beta workflow. Discord
   command synchronization, tester announcements, and production deployment
   are separate approvals.

## Optional historical asset reconciliation

The database archive does not contain local Team/House image files, and custom
emoji IDs remain owned by the Discord guild that created them. After a mirror,
perform this optional beta-only reconciliation when visual card coverage is
useful:

1. Copy the matching production `polytopia_images-<weekday>.tar.gz` into the
   development staging directory as a separate privileged operator action.
   Validate its checksum, reject links/absolute paths/path traversal and
   unexpected members, and validate every image before publishing it under the
   isolated development `image_root`. Unchanged House/Team primary keys make
   `houses/<id>.png` and `teams/<id>.png` directly reusable.
2. Inventory the beta guild's available custom emoji read-only. Match stored
   custom emoji by case-insensitive name, with explicit aliases for known beta
   spelling differences such as stored `elyrion` to beta `elyron`. Never assume
   that syntax-valid production custom-emoji IDs are usable by the beta bot.
3. Use beta-owned custom emoji for exact matches. Assign stable Unicode emoji
   to unmatched Teams and any missing Tribes; descriptive House Unicode is also
   acceptable for beta card coverage because production House emoji may be
   blank. Keep this explicitly development-only and transactionally bind the
   update to the stopped beta's writer lock.
4. Prefer validated local files over fragile historical image URLs. A currently
   reachable remote House image may be normalized into the development image
   root; a dead URL may use an explicitly reviewed related Team image as a
   beta-only fallback.

This is intentionally a visual-testing shim, not a promise to reproduce every
production Discord emoji. A later database refresh overwrites the stored emoji
fields and therefore repeats this reconciliation from a fresh plan.

## Rollback and refresh policy

If the remap is refused, keep the beta stopped and investigate the bounded
diagnostic. If apply commits but post-commit verification requires
reconciliation, do not run an inverse SQL remap. Stop before startup and use
the preserved development archive to restore a fresh `polytopia_dev` under
the same separately approved no-owner/no-acl/single-transaction procedure,
then restore the five beta configuration tables and repeat plan/apply/verify.

Every later refresh repeats the complete sequence with a fresh validated
partial dump and a newly printed confirmation. Never reuse an old digest,
parking sentinel rows, production credentials, production database, or
unbounded continuous synchronization.
