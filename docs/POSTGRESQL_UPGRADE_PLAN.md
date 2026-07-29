# PostgreSQL 12 to 18 upgrade plan

This document plans and records a PostgreSQL server upgrade for `racknerd`.
Completing one phase does not authorize a later phase, cluster
creation/removal, a PostgreSQL restart, a database restore, or a
production-bot restart.

## Recommendation

Upgrade the complete PostgreSQL 12 cluster directly to PostgreSQL 18 by using
`pg_upgrade` in copy mode through Ubuntu's `pg_upgradecluster` wrapper. Rehearse
the exact wrapper command against a disposable PostgreSQL 18 cluster first.

Do not use the already-installed PostgreSQL 14 cluster as the destination.
PostgreSQL 14 reaches end of community support on 2026-11-12, while PostgreSQL
18 is supported through 2030-11-14. PostgreSQL 12 has been unsupported since
2024-11-21.

Primary references:

- [PostgreSQL versioning policy](https://www.postgresql.org/support/versioning/)
- [PostgreSQL 18 `pg_upgrade`](https://www.postgresql.org/docs/current/pgupgrade.html)
- [PostgreSQL cluster-upgrade methods](https://www.postgresql.org/docs/current/upgrading.html)
- [Official PostgreSQL Ubuntu packages](https://www.postgresql.org/download/linux/ubuntu/)

## Observed host and cluster state

Recorded on 2026-07-28:

- Ubuntu 22.04.5 LTS (`jammy`), x86-64, ext4
- 2 vCPUs, 2.3 GiB RAM, 2.4 GiB swap
- approximately 4.3 GiB free on `/`
- no standby, replication node, tablespace, publication, replication slot,
  prepared transaction, or large object
- PostgreSQL listens only on localhost
- data checksums are disabled on the PostgreSQL 12 cluster
- only the built-in `plpgsql` extension is installed in `polytopia2`
- PostgreSQL 18.4 server and client packages are installed from PGDG, but no
  PostgreSQL 18 data cluster exists yet

Clusters:

| Version | Port | State | Purpose |
| --- | ---: | --- | --- |
| 12.20 | 5432 | online | active production and development data |
| 14.23 | 5433 | removed | verified unused default Ubuntu cluster |

The PostgreSQL 14 cluster was inspected as `postgres` on 2026-07-28. It
contains only the connectable `postgres` and `template1` databases at about
8.6 MB each, only built-in roles plus `postgres`, and no client session other
than the inspection query. It also rejects local login as `nelluk` because
that role does not exist. This confirmed it was an unused default cluster.
After explicit approval, `14/main` was removed. Its data and configuration
directories and port-5433 listener are absent; PostgreSQL 14 packages remain
installed.

Databases in the PostgreSQL 12 cluster:

| Database | Owner | Approximate size | Observed use |
| --- | --- | ---: | --- |
| `polytopia2` | `nelluk` | 864 MB | production PolyBot |
| `polytopia_dev` | `polybot_dev` | 9.3 MB | development PolyBot |
| `twospies` | `nelluk` | 8.7 MB | no active session; old backup cron is commented |
| `postgres` | `postgres` | 8.0 MB | administration |

The plan preserves `twospies`. Its removal would be a separate destructive
decision even if it is confirmed abandoned.

The live application presented three idle local sessions to `polytopia2`.
There was no active session to `twospies` or `polytopia_dev` during inspection.

## Why copy-mode `pg_upgrade`

PostgreSQL documents that `pg_upgrade` supports upgrades from 9.2 and later
directly to the current release. Copy mode is preferred here because:

- the cluster is small enough to duplicate with the available disk space;
- it leaves the PostgreSQL 12 data directory independent and restartable;
- it avoids link mode's restriction that the old cluster becomes unsafe after
  the new cluster writes shared files;
- ext4 does not provide the supported reflink/clone behavior described for
  Btrfs, XFS with reflinks, or APFS;
- it should cause substantially less downtime than a full logical
  dump-and-restore migration.

Logical dumps remain the portable recovery source. Copy-mode `pg_upgrade` is
the migration mechanism, not the only backup.

## Fixed safety boundaries

- Upgrade the entire cluster, including roles and every database.
- Keep PostgreSQL local-only.
- Preserve the `polybot_dev` rejection rules for `polytopia2`.
- Do not enable `polyapi.service`.
- Do not change application schema or bot dependency versions in this work.
- Do not use `--link`, `--clone`, `--swap`, or `--no-sync`.
- Do not delete PostgreSQL 12 until a settling period has passed.
- Do not restore over a live database without separate approval.
- Stop the bot before the final backup and cluster upgrade.
- Start the bot against PostgreSQL 18 with background tasks disabled first.

## Phase PG1 completed: resolve the existing PostgreSQL 14 cluster

Nelluk inspected the port-5433 cluster as its owner with:

```bash
sudo -u postgres psql -X -P pager=off -p 5433 -d postgres \
  -c "SELECT version();"

sudo -u postgres psql -X -P pager=off -p 5433 -d postgres \
  -c "SELECT datname, pg_size_pretty(pg_database_size(datname))
      FROM pg_database
      WHERE datallowconn
      ORDER BY datname;"

sudo -u postgres psql -X -P pager=off -p 5433 -d postgres \
  -c "SELECT rolname, rolsuper, rolcanlogin
      FROM pg_roles
      ORDER BY rolname;"

sudo -u postgres psql -X -P pager=off -p 5433 -d postgres \
  -c "SELECT datname, usename, application_name, client_addr, state
      FROM pg_stat_activity
      ORDER BY datname, usename;"
```

The output confirmed only default databases/roles and no client. Its removal
was separately approved and completed with:

```bash
sudo pg_dropcluster --stop 14 main
```

The PostgreSQL 14 packages were intentionally retained. Package cleanup remains
a distinct later action.

## Phase PG2 completed: install PostgreSQL 18 without cutting over

Ubuntu 22.04's distribution repository stops at PostgreSQL 14. Use the official
PostgreSQL Apt repository, which supports `jammy` and publishes PostgreSQL 18.

The official automated setup is:

```bash
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt update
sudo apt install postgresql-18 postgresql-client-18
```

Before running it:

- refresh and read the disk audit;
- record the exact Apt transaction;
- confirm no unrelated package removal;
- expect `postgresql-common` and `libpq` package updates;
- confirm whether installation will create/start `18/main`;
- ensure PostgreSQL 12 remains on port 5432 throughout this phase.

If package installation creates `18/main`, stop it until rehearsal:

```bash
sudo pg_ctlcluster 18 main stop
pg_lsclusters
```

Nelluk separately approved PG2. The Apt simulation showed three upgrades,
four new packages, no removals, and no PostgreSQL 12 package change. On
2026-07-28 the following PGDG packages were installed:

- `postgresql-18`, `postgresql-client-18`, and `postgresql-18-jit` 18.4
- `postgresql-common` and `postgresql-client-common` 293
- `libpq5` 18.4

Installation did not create an `18/main` cluster. Post-install verification
confirmed:

- `12/main` remained online on port 5432 and was the only registered cluster;
- PostgreSQL continued listening only on `127.0.0.1` and `::1`;
- the production server reported version 12.20 and database `polytopia2`;
- the default `psql` client reported version 18.4;
- `polytopia.service` retained PID 1480385 with zero restarts;
- approximately 4.2 GiB remained free on `/`.

No cluster was created, stopped, restarted, upgraded, or restored during PG2.
Completion of PG2 is not approval to begin the disposable PG3 rehearsal.

## Phase PG3 completed: rehearsal and compatibility proof

The rehearsal used this production-isolated sequence:

1. Create a fresh PostgreSQL 12 source cluster on an unused port.
2. Create and validate PostgreSQL 12 and PostgreSQL 18 client dumps from the
   live PostgreSQL 12 server.
3. Restore every application database and the required roles into the
   disposable PostgreSQL 12 source.
4. Compare database ownership, encoding, locale, schema, table counts, and
   representative row counts.
5. Run representative Peewee reads, writes inside rolled-back transactions,
   graph rendering, API route tests, and the complete Python 3.12 suite.
6. Copy the production HBA policy into the disposable source.
7. Run the complete production-shaped `pg_upgradecluster` copy-mode command
   against only the disposable source.
8. Repeat the structural, row-count, HBA, access-policy, and application tests
   against PostgreSQL 18.
9. Restore PostgreSQL 18-generated archives into derived probe databases.
10. Retain the two rehearsal clusters until their results and cleanup receive
    separate approval.

The installed `pg_upgradecluster --check` option checks required packages; it
does not run PostgreSQL's binary compatibility checks. A meaningful rehearsal
therefore used a disposable PostgreSQL 12 source cluster and performed the
complete copy-mode upgrade. This exercised the real `pg_upgrade` consistency
checks without stopping production.

On 2026-07-28:

- `12/rehearsal` was created on port 5434 with UTF8 encoding,
  `en_US.UTF-8` locale, and data checksums disabled, matching production.
- Private PostgreSQL 12 and PostgreSQL 18 logical archives were created for
  `polytopia2`, `polytopia_dev`, and `twospies`, plus globals. The directory
  is mode 0700 and the archive files are mode 0600.
- Both client generations dumped `polytopia2` in approximately 23 seconds.
  All six custom archives passed their matching `pg_restore --list`.
- The PostgreSQL 12 archives restored into `12/rehearsal` without error.
  Restoring `polytopia2` took approximately 25 seconds.
- Snapshot row-count differences from live production were limited to one
  `discordmember`, one `player`, and two `gamelog` rows added after the dump
  began. Relationship-table counts matched exactly.
- All 54 Python 3.12 tests passed against the restored PostgreSQL 12
  `polytopia_dev`, including five database integration tests.
- The production HBA file was copied to the disposable source so the rehearsal
  also tested preservation of the isolated-development-role rejection rules.
- The package preflight passed, after which this exact command upgraded only
  the disposable cluster:

```bash
sudo pg_upgradecluster \
  --method=upgrade \
  --jobs=2 \
  --rename=rehearsal18 \
  -v 18 \
  12 rehearsal
```

The copy-mode upgrade completed in approximately 22 seconds. The wrapper ran
the full PostgreSQL compatibility checks and staged optimizer statistics. It
left `12/rehearsal` stopped in manual mode on port 5433 and started
`18/rehearsal18` on port 5434.

Post-upgrade verification confirmed:

- PostgreSQL 18.4 reported the expected owners, UTF8 encoding,
  `en_US.UTF-8` locale, and disabled data checksums for all databases.
- The six high-value table row counts exactly matched the restored snapshot.
- `polytopia2` retained 17 tables, 55 indexes, 17 sequences, 148 columns,
  23 foreign keys, and 17 primary keys, with no invalid index.
- PostgreSQL 18 additionally represents 101 existing `NOT NULL` attributes as
  catalog constraint type `n`; this explains its larger raw constraint count.
- Every database's recorded and actual collation version matched at 2.35.
- The migrated PostgreSQL 18 HBA file was byte-equivalent to production.
- `polybot_dev` could connect to `polytopia_dev` but was rejected from
  `polytopia2`.
- All 54 Python 3.12 tests passed again against PostgreSQL 18.
- A schema-only restore of the PostgreSQL 18 `polytopia2` archive reproduced
  the exact schema object counts, and a full restore of the PostgreSQL 18
  `polytopia_dev` archive reproduced all representative row counts.
- The two derived restore-probe databases were removed after validation.
- Production `12/main` stayed online on port 5432, and
  `polytopia.service` retained PID 1480385 with zero restarts.

The rehearsal temporarily raised root-filesystem use to 91%, with
approximately 2.0 GiB free. After separate approval, `18/rehearsal18`,
`12/rehearsal`, and `/var/lib/postgresql/rehearsal-backups` were removed.
Only production `12/main` remained, and free space returned to approximately
4.1 GiB.

The rehearsed production command is equivalent to:

```text
pg_upgradecluster --method=upgrade --jobs=2 -v 18 12 main
```

The rehearsal proved its port, configuration-copy, old-cluster retention, and
rollback behavior. Executing it against production still requires separate
maintenance-window approval for PG5.

## Phase PG4 completed: pre-cutover backup set

The normal `/home/nelluk/backup_db.sh` protects `polytopia2` and local images,
but it is not a complete cluster backup. It omits global roles and the other
databases.

Before the maintenance window:

- run and validate the normal atomic bot backup;
- create a private timestamped upgrade archive;
- archive `/etc/postgresql/12/main`;
- record package versions, clusters, role flags, database owners/sizes,
  authentication rules, and active sessions;
- create a globals-only dump with the PostgreSQL 18 client;
- create PostgreSQL 18-client custom-format dumps for `polytopia2`,
  `polytopia_dev`, and `twospies`;
- validate every custom-format dump with PostgreSQL 18 `pg_restore --list`;
- keep a checksum manifest.

Using the newer dump programs is consistent with PostgreSQL's recommendation
for major-version dump/restore planning.

Nelluk separately approved PG4. On 2026-07-28:

- `/home/nelluk/backup_db.sh` completed successfully using its hardened,
  locked, atomic workflow.
- A private timestamped archive was created at
  `/home/nelluk/backups/postgresql18-pg4-20260728T214311-0400`.
- The archive directory is mode 0700 and every contained file is mode 0600.
- PostgreSQL 18 client tools created a globals-only dump and custom-format
  dumps of `polytopia2`, `polytopia_dev`, and `twospies` from the online
  PostgreSQL 12 server.
- The online `polytopia2` dump completed in approximately 21 seconds.
- Every custom-format dump passed PostgreSQL 18
  `pg_restore --list`.
- The archive records installed package versions, clusters, role flags,
  database ownership/encoding/locale/size, sessions, listeners, service
  states, data checksums, extensions, representative row counts, invalid
  indexes, and checksums of the normal bot-backup artifacts.
- `postgresql-config.tar.gz` contains `/etc/postgresql/12/main` and
  `/etc/postgresql-common`, including the production HBA policy and
  `pg_upgradecluster.d/analyze` hook.
- `SHA256SUMS` validates every file in the archive.
- The completed archive is approximately 115 MB.
- Production remained on PostgreSQL 12, localhost-only, with
  `polytopia.service` retaining PID 1480385 and zero restarts.
- Approximately 4.0 GiB remained free after PG4.

PG4 did not stop or restart PostgreSQL or the bot. Its online dumps are the
pre-cutover recovery layer; PG5 still requires a final stopped-service backup
immediately before the production upgrade.

## Phase PG5: maintenance-window cutover

1. Confirm the bot is healthy and `polyapi.service` is inactive.
2. Stop `polytopia.service`.
3. Confirm no application sessions remain.
4. Take and validate a final stopped-service cluster-wide logical backup.
5. Confirm no `18/main` target cluster exists.
6. Run the rehearsed copy-mode cluster upgrade as root. The wrapper stops
   PostgreSQL 12, creates `18/main`, performs `pg_upgrade`, moves the old
   PostgreSQL 12 cluster to a nonproduction port, and starts PostgreSQL 18.
7. Confirm the wrapper assigns PostgreSQL 18 the production port 5432 and
   leaves PostgreSQL 12 stopped, independent, and in manual startup mode on a
   nonproduction port.
8. Compare the migrated PostgreSQL 18 configuration with the archived
   PostgreSQL 12 configuration.
9. Confirm localhost-only listening and all `pg_hba.conf` rules, especially
   the `polybot_dev` rejection from `polytopia2`.
10. Start PostgreSQL 18 and verify roles, owners, databases, extensions,
    encodings, locales, sizes, and key row counts.
11. Run any analyze/rebuild scripts produced by `pg_upgrade`.
12. Run:

    ```bash
    sudo -u postgres /usr/lib/postgresql/18/bin/vacuumdb \
      --all --analyze-in-stages --missing-stats-only
    ```

13. Start `polytopia.service` temporarily with `--skip_tasks`.
14. Verify production bot identity, PostgreSQL 18 server version, database
    access, guilds, Bullet loading, and read-only commands.
15. Replace the canary command with the permanent systemd drop-in, restart the
    bot with tasks enabled, and observe both services for at least ten minutes.

## Rollback

Copy mode leaves PostgreSQL 12 independent. During the task-disabled canary:

1. Stop `polytopia.service`.
2. Stop PostgreSQL 18.
3. Restore the archived PostgreSQL 12 port/startup configuration.
4. Start PostgreSQL 12 on port 5432.
5. Confirm the expected server version, databases, roles, authentication, and
   key row counts.
6. Start the task-disabled bot and smoke-test it before enabling tasks.

The exact port/configuration commands must come from the rehearsal because
`pg_upgradecluster` adjusts the old cluster to an unused port and manual
startup.

Rollback becomes potentially lossy after PostgreSQL 18 accepts production
writes: those writes are absent from the copied PostgreSQL 12 cluster. Keep
the canary short and task-disabled. If writes must be preserved, stop both
systems and perform a separately approved logical reconciliation rather than
blindly starting PostgreSQL 12.

A full logical restore is the last-resort recovery path. It is slower and
would discard activity after the selected dump, so it requires separate
approval.

## Success criteria

- PostgreSQL 18 is the only cluster listening on production port 5432.
- The server reports the reviewed current PostgreSQL 18 minor release.
- All expected databases, roles, ownership, encodings, and locales match.
- `polytopia2`, `polytopia_dev`, and preserved `twospies` data compare.
- Local-only networking and HBA isolation are unchanged.
- The Python 3.12 bot connects without database or extension errors.
- Read-only Discord smoke checks and background tasks pass.
- PostgreSQL and bot processes remain stable for at least ten minutes.
- Fresh PostgreSQL 18 backups pass validation.
- PostgreSQL 12 remains stopped and independently recoverable.

## Deferred cleanup

After at least one to two weeks of stable PostgreSQL 18 operation and multiple
validated backup cycles, request separate approval to:

- remove the old PostgreSQL 12 cluster with `pg_dropcluster`;
- remove unsupported PostgreSQL 12 packages;
- remove PostgreSQL 14 packages if no longer needed;
- delete disposable rehearsal artifacts;
- decide whether to retain or delete the `twospies` database;
- update monitoring and disk-audit expectations.

Do not combine old-cluster removal with the cutover approval.
