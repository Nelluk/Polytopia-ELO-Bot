# PostgreSQL 18 and Python 3.12 post-upgrade cleanup

Status: **Complete on 2026-08-04**

This runbook retires the rollback-only PostgreSQL 12 cluster and Python 3.9
environment after the successful PostgreSQL 18 and Python 3.12 cutovers. Each
host-changing phase requires Nelluk's explicit approval. Completing one phase
does not authorize the next phase.

## Completion evidence

- Pull request 138 merged the canonical unit and prepared runbook as
  `be1f6e8ad6fb8c03917bc45f7bf1919f2417e45c` before host cleanup.
- `/etc/systemd/system/polytopia.service` matches the tracked unit at SHA-256
  `9e90b085feaac52c5b75171da6732d9a48ec9b3d64b7287f9ad8cfbbb0bb7c8d`.
  The enablement link points to that `/etc` unit; the cutover drop-in and
  non-package-owned `/lib` unit are absent. Installing it did not restart the
  bot.
- A fresh mode-0600 full production dump was created at 21:26 EDT and passed
  `pg_restore --list`. All seven rotating dumps passed the same validation.
  The PG4 and final stopped-service archives passed every recorded checksum,
  and their `polytopia2`, `polytopia_dev`, and `twospies` dumps passed restore
  listing.
- `12/main`, measured at 1007 MB, was removed with `pg_dropcluster`. The
  PostgreSQL 12 and 14 server/client packages and old metapackages were
  removed; the residual server-package records were purged. No autoremove ran.
- PostgreSQL 18.4 remains the only cluster, online on localhost-only port 5432.
  `polytopia2`, `polytopia_dev`, and `twospies` retain their expected owners.
- The five explicit Python 3.9 virtual-environment paths were removed,
  recovering approximately 473 MB. The active mode-0700 `.venv` remains
  Python 3.12.13, and the live bot executable resolves to the managed Python
  3.12.13 interpreter.
- `server_settings.py`, `config.ini`, and `spreadsheet_creds.json` are all
  mode 0600 and owned by `nelluk:nelluk`.
- The bot remained PID 2327849 with `NRestarts=1`; PostgreSQL remained PID
  1532765 with `NRestarts=0`. Cleanup caused no restart and left no failed
  systemd units.
- The final 22:11 EDT disk audit reports 3.4 GB free and 84% use, improved
  from 1.9 GB free and 91% use before cleanup.
- Current backups, the PG4 archive, final stopped-service archive, Python
  cutover archive, `twospies`, application data, configuration, images, and
  logs were retained.
- `polyapi.service` remains disabled and inactive. Its legacy Python 3.9
  command is intentionally unusable after cleanup; enabling the API requires
  the separately reviewed Python 3.12 service work in
  `docs/PRODUCTION_CUTOVER.md`.

## Fixed scope

- Production checkout: `/home/nelluk/PolyBot39`
- Production service: `polytopia.service`
- Active Python environment: `/home/nelluk/PolyBot39/.venv`
- Obsolete Python environment components:
  `/home/nelluk/PolyBot39/bin`, `include`, `lib`, `lib64`, and `pyvenv.cfg`
- Active database cluster: PostgreSQL `18/main` on port 5432
- Obsolete stopped cluster: PostgreSQL `12/main` on port 5433
- Retained databases: `polytopia2`, `polytopia_dev`, and `twospies`
- Retained recovery material: current rotating PostgreSQL 18 backups, the PG4
  archive, the final stopped-service archive, and the Python cutover archive

Do not remove `.venv`, production configuration, credentials, images, logs,
current backups, `twospies`, PostgreSQL 18, or shared PostgreSQL packages.
Do not run `apt autoremove` as part of this cleanup.

## C0 — Repository and systemd preparation

The complete reviewed service is tracked at
`deploy/systemd/polytopia.service`. It replaces the cutover-only tracked
drop-in. Before host cleanup, verify it and record the live service state:

```bash
systemd-analyze verify deploy/systemd/polytopia.service
systemctl cat polytopia.service --no-pager
systemctl show polytopia.service \
  -p MainPID -p NRestarts -p FragmentPath -p DropInPaths -p ExecStart \
  --no-pager
```

Install the complete unit without restarting the running bot:

```bash
sudo install -m 0644 \
  /home/nelluk/PolyBot39/deploy/systemd/polytopia.service \
  /etc/systemd/system/polytopia.service
sudo rm -f /etc/systemd/system/polytopia.service.d/upgrade.conf
sudo rmdir --ignore-fail-on-non-empty \
  /etc/systemd/system/polytopia.service.d
sudo systemctl reenable polytopia.service
sudo systemctl daemon-reload
```

Stop unless all of these are true:

- `FragmentPath` is `/etc/systemd/system/polytopia.service`;
- `DropInPaths` is empty;
- `ExecStart` uses `/home/nelluk/PolyBot39/.venv/bin/python`;
- the enablement link points to the `/etc` unit;
- `MainPID` and `NRestarts` did not change.

```bash
systemctl cat polytopia.service --no-pager
systemctl show polytopia.service \
  -p MainPID -p NRestarts -p FragmentPath -p DropInPaths -p ExecStart \
  --no-pager
ls -l /etc/systemd/system/multi-user.target.wants/polytopia.service
```

Only after those checks pass, remove the obsolete non-package-owned base unit
and verify enablement again:

```bash
sudo rm /lib/systemd/system/polytopia.service
sudo systemctl daemon-reload
systemctl is-enabled polytopia.service
systemctl is-active polytopia.service
systemctl cat polytopia.service --no-pager
```

This phase changes future service starts but does not restart the live bot.

## C1 — Final backup and identity gate

Refresh the observational disk audit, run the established production backup,
and validate the resulting custom archive:

```bash
sudo systemctl start racknerd-disk-audit.service
cat /home/nelluk/disk-audit-latest.txt
df -h /
/home/nelluk/backup_db.sh
pg_restore --list /home/nelluk/polytopia_full_backup.sqlc >/dev/null
```

Then confirm the exact cluster and service identities:

```bash
pg_lsclusters
systemctl show postgresql@18-main.service polytopia.service \
  -p Id -p ActiveState -p SubState -p MainPID -p NRestarts --no-pager
sudo ss -ltnp | grep -E ':5432|:5433'
sudo du -sh /var/lib/postgresql/12/main
```

Required gate: `18/main` is online on localhost port 5432, `12/main` is down
on port 5433, nothing listens on 5433, the bot uses the Python 3.12 `.venv`,
and the new backup plus retained archives validate. Stop on any mismatch.

## C2 — Remove PostgreSQL 12 physical rollback

This is irreversible without restoring an archive. Recheck `pg_lsclusters`
immediately before running:

```bash
sudo pg_dropcluster --stop 12 main
```

Verify that `18/main` remains active on 5432, no 5433 listener exists, and the
bot PID and restart count remain unchanged. Do not continue if PostgreSQL 18
or the bot changed state.

## C3 — Remove obsolete PostgreSQL packages

Simulate the exact transaction immediately before approval:

```bash
sudo apt-get -s remove \
  postgresql-12 postgresql-client-12 \
  postgresql-14 postgresql-client-14 \
  postgresql postgresql-contrib
```

Proceed only if the simulation removes exactly those six packages and retains
`postgresql-18`, `postgresql-client-18`, `postgresql-common`,
`postgresql-client-common`, and `libpq5`:

```bash
sudo apt-get remove \
  postgresql-12 postgresql-client-12 \
  postgresql-14 postgresql-client-14 \
  postgresql postgresql-contrib
```

Do not accept an expanded removal transaction and do not run autoremove.
Afterward, verify installed PostgreSQL packages, `pg_lsclusters`, port 5432,
and both live services.

## C4 — Remove the Python 3.9 environment

First prove that neither the live process nor effective unit uses it:

```bash
systemctl show polytopia.service -p MainPID -p ExecStart --no-pager
readlink -f /proc/$(systemctl show -p MainPID --value polytopia.service)/exe
/home/nelluk/PolyBot39/.venv/bin/python --version
sed -n '1,20p' /home/nelluk/PolyBot39/pyvenv.cfg
```

The executable and `ExecStart` must use Python 3.12 under `.venv`; the
root-level `pyvenv.cfg` must identify only the old environment. Remove exactly
these paths:

```bash
rm -rf -- \
  /home/nelluk/PolyBot39/bin \
  /home/nelluk/PolyBot39/include \
  /home/nelluk/PolyBot39/lib
rm -- \
  /home/nelluk/PolyBot39/lib64 \
  /home/nelluk/PolyBot39/pyvenv.cfg
```

Never target `/home/nelluk/PolyBot39/.venv`.

## C5 — Tighten configuration permissions

The production service runs as `nelluk`, so restrict the ignored server
configuration to that account and verify ownership:

```bash
chmod 0600 /home/nelluk/PolyBot39/server_settings.py
stat -c '%a %U:%G %n' /home/nelluk/PolyBot39/server_settings.py
```

## C6 — Final evidence and documentation

Record final clusters, listeners, package versions, effective service unit,
service PID/restarts, backup validation, and disk usage. Do not restart the bot
solely for cleanup: the reviewed Python 3.12 configuration has already survived
normal starts, and C0 changed no live process. A future normal restart will use
the canonical `/etc` unit.

After every phase passed, `docs/POSTGRESQL_UPGRADE_PLAN.md` and
`docs/PRODUCTION_CUTOVER.md` were updated with the actual results, disk
recovery, and retirement of the old physical/interpreter rollback paths.
