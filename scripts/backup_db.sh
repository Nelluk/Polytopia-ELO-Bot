#!/usr/bin/env bash

# Back up the production PolyBot PostgreSQL database and local team images.
# Generate and validate private temporary files before atomically publishing
# them, so a failed run cannot truncate the last known-good backup.

set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=${POLYBOT_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}
STATE_ROOT=${POLYBOT_STATE_ROOT:-$(cd -- "$PROJECT_ROOT/.." && pwd)}
BACKUP_DIR=${POLYBOT_BACKUP_DIR:-$STATE_ROOT/backups}
DATABASE=${POLYBOT_DATABASE:-polytopia2}
DATABASE_USER=${POLYBOT_DATABASE_USER:-}

DAY=$(/usr/bin/date +%A)
TARGET=$BACKUP_DIR/polytopia_bak-${DAY}.sqlc
LOGTARGET=$BACKUP_DIR/polytopia_gamelogs.csv.gz
REPORTTARGET=$BACKUP_DIR/polytopia_reporting.duckdb
FULLTARGET=${POLYBOT_FULL_BACKUP:-$STATE_ROOT/polytopia_full_backup.sqlc}
IMAGEDIR=${POLYBOT_IMAGE_DIR:-$PROJECT_ROOT/data/images}
IMAGETARGET=$BACKUP_DIR/polytopia_images-${DAY}.tar.gz
LOCKFILE=${POLYBOT_BACKUP_LOCK:-$STATE_ROOT/.backup_db.lock}
REPORTLOCK=${POLYBOT_REPORT_LOCK:-$STATE_ROOT/.polybot-reporting.lock}
REPORTEXPORTER=${POLYBOT_REPORT_EXPORTER:-$PROJECT_ROOT/scripts/export_reporting_duckdb.py}
REPORTPYTHON=${POLYBOT_REPORT_PYTHON:-$PROJECT_ROOT/.venv/bin/python}

DATABASE_USER_ARGS=()
REPORT_USER_ARGS=()
if [ -n "$DATABASE_USER" ]
then
  DATABASE_USER_ARGS=(--username "$DATABASE_USER")
  REPORT_USER_ARGS=(--user "$DATABASE_USER")
fi

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
  fail "another backup run already holds $LOCKFILE"
fi

if [ ! -d "$BACKUP_DIR" ]
then
  fail "backup directory $BACKUP_DIR does not exist"
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
  "${DATABASE_USER_ARGS[@]}" \
  -Fc \
  --exclude-table=gamelog \
  --file="$TARGET_TMP" \
  "$DATABASE"
then
  fail "partial PostgreSQL dump command failed"
fi

if ! /usr/bin/pg_restore --list "$TARGET_TMP" >/dev/null
then
  fail "partial PostgreSQL dump validation failed"
fi

if ! /usr/bin/psql \
  "${DATABASE_USER_ARGS[@]}" \
  --dbname="$DATABASE" \
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
  "${DATABASE_USER_ARGS[@]}" \
  -Fc \
  --file="$FULLTARGET_TMP" \
  "$DATABASE"
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
  --database "$DATABASE" \
  "${REPORT_USER_ARGS[@]}" \
  --replace
then
  echo "Core backup successful, but reporting export failed." >&2
  echo "The previous reporting snapshot, if any, was preserved." >&2
  exit 1
fi

echo "Backup successful:"
echo "  partial database: $TARGET"
echo "  public gamelog:   $LOGTARGET"
echo "  reporting data:   $REPORTTARGET"
echo "  full database:    $FULLTARGET"
echo "  local images:     $IMAGETARGET"
