#!/usr/bin/env bash

# Back up the production PolyBot PostgreSQL database and local team images.
# Generate and validate private temporary files before atomically publishing
# them, so a failed run cannot truncate the last known-good backup.

set -o pipefail
umask 077

DAY=$(/usr/bin/date +%A)
TARGET=/home/nelluk/backups/polytopia_bak-${DAY}.sqlc
LOGTARGET=/home/nelluk/backups/polytopia_gamelogs.csv.gz
REPORTTARGET=/home/nelluk/backups/polytopia_reporting.duckdb
FULLTARGET=/home/nelluk/polytopia_full_backup.sqlc
IMAGEDIR=/home/nelluk/PolyBot39/data/images
IMAGETARGET=/home/nelluk/backups/polytopia_images-${DAY}.tar.gz
LOCKFILE=/home/nelluk/.backup_db.lock
REPORTLOCK=/home/nelluk/.polybot-reporting.lock
REPORTEXPORTER=/home/nelluk/PolyBot39/scripts/export_reporting_duckdb.py
REPORTPYTHON=/home/nelluk/PolyBot39/.venv/bin/python
REPORTING_PARTIAL_EXIT=20
LOCK_BUSY_EXIT=75

TARGET_TMP=
LOGTARGET_TMP=
FULLTARGET_TMP=
IMAGETARGET_TMP=

cleanup() {
  for temporary_file in \
    "$TARGET_TMP" \
    "$LOGTARGET_TMP" \
    "$FULLTARGET_TMP" \
    "$IMAGETARGET_TMP"
  do
    if [ -n "$temporary_file" ]
    then
      /usr/bin/rm -f -- "$temporary_file"
    fi
  done
}

fail() {
  echo "Backup failed: $1" >&2
  exit 1
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

exec 9>"$LOCKFILE" || fail "cannot open lock file $LOCKFILE"
if ! /usr/bin/flock -n 9
then
  echo "Backup deferred: another run already holds $LOCKFILE" >&2
  exit "$LOCK_BUSY_EXIT"
fi

if [ ! -d /home/nelluk/backups ]
then
  fail "backup directory /home/nelluk/backups does not exist"
fi

if [ ! -d "$IMAGEDIR" ]
then
  fail "image directory $IMAGEDIR does not exist"
fi

TARGET_TMP=$(/usr/bin/mktemp "${TARGET}.tmp.XXXXXX") \
  || fail "cannot create temporary partial dump"
LOGTARGET_TMP=$(/usr/bin/mktemp "${LOGTARGET}.tmp.XXXXXX") \
  || fail "cannot create temporary gamelog export"
FULLTARGET_TMP=$(/usr/bin/mktemp "${FULLTARGET}.tmp.XXXXXX") \
  || fail "cannot create temporary full dump"
IMAGETARGET_TMP=$(/usr/bin/mktemp "${IMAGETARGET}.tmp.XXXXXX") \
  || fail "cannot create temporary image archive"

if ! /usr/bin/pg_dump \
  -U nelluk \
  -Fc \
  --exclude-table=gamelog \
  --file="$TARGET_TMP" \
  polytopia2
then
  fail "partial PostgreSQL dump command failed"
fi

if ! /usr/bin/pg_restore --list "$TARGET_TMP" >/dev/null
then
  fail "partial PostgreSQL dump validation failed"
fi

if ! /usr/bin/psql \
  --dbname=polytopia2 \
  --command="COPY (SELECT id, message_ts, guild_id, message FROM gamelog WHERE gamelog.is_protected = FALSE ORDER BY id ASC) TO stdout DELIMITER ',' CSV HEADER" \
  | /usr/bin/gzip >"$LOGTARGET_TMP"
then
  fail "gamelog export command failed"
fi

if ! /usr/bin/gzip -t "$LOGTARGET_TMP"
then
  fail "gamelog gzip validation failed"
fi

if ! /usr/bin/pg_dump \
  -U nelluk \
  -Fc \
  --file="$FULLTARGET_TMP" \
  polytopia2
then
  fail "full PostgreSQL dump command failed"
fi

if ! /usr/bin/pg_restore --list "$FULLTARGET_TMP" >/dev/null
then
  fail "full PostgreSQL dump validation failed"
fi

if ! /usr/bin/tar -czf "$IMAGETARGET_TMP" -C "$IMAGEDIR" .
then
  fail "image archive command failed"
fi

if ! /usr/bin/tar -tzf "$IMAGETARGET_TMP" >/dev/null
then
  fail "image archive validation failed"
fi

/usr/bin/mv -f -- "$TARGET_TMP" "$TARGET" \
  || fail "cannot publish partial PostgreSQL dump"
TARGET_TMP=

/usr/bin/mv -f -- "$LOGTARGET_TMP" "$LOGTARGET" \
  || fail "cannot publish gamelog export"
LOGTARGET_TMP=

/usr/bin/mv -f -- "$FULLTARGET_TMP" "$FULLTARGET" \
  || fail "cannot publish full PostgreSQL dump"
FULLTARGET_TMP=

/usr/bin/mv -f -- "$IMAGETARGET_TMP" "$IMAGETARGET" \
  || fail "cannot publish image archive"
IMAGETARGET_TMP=

if ! "$REPORTPYTHON" "$REPORTEXPORTER" \
  --output "$REPORTTARGET" \
  --lock-file "$REPORTLOCK" \
  --replace
then
  echo "Core backup successful, but reporting export failed." >&2
  echo "The previous reporting snapshot, if any, was preserved." >&2
  exit "$REPORTING_PARTIAL_EXIT"
fi

echo "Backup successful:"
echo "  partial database: $TARGET"
echo "  public gamelog:   $LOGTARGET"
echo "  reporting data:   $REPORTTARGET"
echo "  full database:    $FULLTARGET"
echo "  local images:     $IMAGETARGET"
