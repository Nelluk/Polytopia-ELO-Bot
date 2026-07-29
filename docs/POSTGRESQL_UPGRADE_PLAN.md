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

## Phase PG3: rehearsal and compatibility proof

Use a disposable PostgreSQL 18 cluster on a nonproduction port. Restore a
logical production dump and test without Discord:

1. Create a fresh PostgreSQL 18 test cluster on an unused port.
2. Use PostgreSQL 18 client tools to dump from PostgreSQL 12.
3. Restore `polytopia2` into the test cluster.
4. Compare database ownership, encoding, locale, schema, table counts, and
   representative row counts.
5. Compare the largest/high-value tables:
   `gamelog`, `lineup`, `gameside`, `game`, `player`, and `discordmember`.
6. Run representative Peewee reads, writes inside rolled-back transactions,
   graph rendering, and API route tests against PostgreSQL 18.
7. Run the Python 3.12 application test suite.
8. Run the exact `pg_upgrade --check`/`pg_upgradecluster` preflight intended for
   production and preserve its output.
9. Measure restore and upgrade duration to size the maintenance window.
10. Remove only the disposable rehearsal cluster after its results are
    reviewed and separately approved.

The rehearsal will establish the exact `pg_upgradecluster` syntax delivered by
the installed `postgresql-common` version. The anticipated production command
is equivalent to:

```text
pg_upgradecluster --method=upgrade --jobs=2 -v 18 12 main
```

Do not execute that command against production until the rehearsal proves its
port, configuration-copy, old-cluster retention, and rollback behavior.

## Phase PG4: pre-cutover backup set

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

## Phase PG5: maintenance-window cutover

1. Confirm the bot is healthy and `polyapi.service` is inactive.
2. Stop `polytopia.service`.
3. Confirm no application sessions remain.
4. Take and validate a final stopped-service cluster-wide logical backup.
5. Stop PostgreSQL 12 and the empty PostgreSQL 18 destination.
6. Run the rehearsed copy-mode cluster upgrade as `postgres`.
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
