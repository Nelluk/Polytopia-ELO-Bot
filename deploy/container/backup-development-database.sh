#!/bin/sh

# Create one verified, off-volume logical backup of the bundled development
# database. The exact confirmation is deliberately tied to the reviewed image
# checkpoint; no confirmation means plan-only behavior.

set -eu
umask 077

SOURCE_DATABASE=polytopia_dev
SOURCE_ROLE=polybot_dev
SOURCE_HOST=postgres
SOURCE_PORT=5432
BACKUP_ROOT=/backups
ADMIN_SECRET=/run/secrets/postgres_admin_password
MINIMUM_HEADROOM_BYTES=67108864

temporary_archive=
temporary_digest=
published_archive=
backup_lock=

fail() {
  echo "ERROR: $1" >&2
  exit 2
}

cleanup() {
  if [ -n "$temporary_archive" ]; then
    rm -f -- "$temporary_archive"
  fi
  if [ -n "$temporary_digest" ]; then
    rm -f -- "$temporary_digest"
  fi
  if [ -n "$published_archive" ]; then
    rm -f -- "$published_archive"
    rm -f -- "${published_archive}.sha256"
  fi
  if [ -n "$backup_lock" ]; then
    rmdir -- "$backup_lock" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "${POLYBOT_ENV:-}" = development ] \
  || fail 'POLYBOT_ENV must be development.'
[ "${PGHOST:-}" = "$SOURCE_HOST" ] \
  || fail 'PGHOST must be the bundled postgres service.'
[ "${PGPORT:-}" = "$SOURCE_PORT" ] \
  || fail 'PGPORT must be 5432.'
[ "${PGDATABASE:-}" = postgres ] \
  || fail 'PGDATABASE must be the administrative maintenance database.'
[ "${PGUSER:-}" = postgres ] \
  || fail 'PGUSER must be the bundled administrative role.'

checkpoint=${POLYBOT_SOURCE_CHECKPOINT:-}
case "$checkpoint" in
  ''|*[!0-9a-f]*) fail 'POLYBOT_SOURCE_CHECKPOINT must be one lowercase Git SHA-1.' ;;
esac
[ "${#checkpoint}" -eq 40 ] \
  || fail 'POLYBOT_SOURCE_CHECKPOINT must be one lowercase Git SHA-1.'

confirmation="BACKUP $SOURCE_DATABASE $checkpoint"
echo 'Development container database backup plan'
echo "source service: $SOURCE_HOST"
echo "source database: $SOURCE_DATABASE"
echo 'required writer state: bot stopped; zero other source-database sessions'
echo 'archive: custom-format pg_dump plus SHA-256 sidecar in /backups'
echo 'publication: validated temporary files followed by atomic rename'
echo "confirmation: $confirmation"

provided_confirmation=${POLYBOT_BACKUP_CONFIRMATION:-}
if [ -z "$provided_confirmation" ]; then
  echo 'Plan only; no secret, filesystem, or PostgreSQL operation was attempted.'
  exit 0
fi
[ "$provided_confirmation" = "$confirmation" ] \
  || fail 'Backup confirmation does not match the exact plan.'

[ -d "$BACKUP_ROOT" ] && [ ! -L "$BACKUP_ROOT" ] \
  || fail '/backups must be one existing non-symlink directory.'
[ -w "$BACKUP_ROOT" ] \
  || fail '/backups is not writable by the configured recovery UID/GID.'
backup_lock="$BACKUP_ROOT/.polybot-backup.lock"
mkdir -- "$backup_lock" \
  || fail 'Another backup is active or an interrupted backup lock needs inspection.'
[ -f "$ADMIN_SECRET" ] && [ ! -L "$ADMIN_SECRET" ] \
  || fail 'PostgreSQL administrative secret is not a regular file.'
secret_lines=$(wc -l <"$ADMIN_SECRET") \
  || fail 'Could not inspect the PostgreSQL administrative secret.'
[ "$secret_lines" -le 1 ] \
  || fail 'PostgreSQL administrative secret must contain one nonempty line.'

PGPASSWORD=$(cat "$ADMIN_SECRET")
case "$PGPASSWORD" in
  ''|*'
'*) fail 'PostgreSQL administrative secret must contain one nonempty line.' ;;
esac
export PGPASSWORD

admin_identity=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT current_database() || ':' || current_user") \
  || fail 'Could not inspect the bundled PostgreSQL administrative identity.'
[ "$admin_identity" = 'postgres:postgres' ] \
  || fail 'Bundled backup requires the postgres maintenance identity.'

server_version_num=$(psql -X -v ON_ERROR_STOP=1 -Atqc 'SHOW server_version_num') \
  || fail 'Could not inspect the bundled PostgreSQL server version.'
case "$server_version_num" in
  18????) : ;;
  *) fail 'Bundled backup requires PostgreSQL major 18.' ;;
esac

source_identity=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT count(*) FROM pg_database WHERE datname = '$SOURCE_DATABASE' AND pg_get_userbyid(datdba) = '$SOURCE_ROLE'") \
  || fail 'Could not inspect the source database identity.'
[ "$source_identity" = 1 ] \
  || fail 'The fixed development database is absent or has the wrong owner.'

active_sessions() {
  psql -X -v ON_ERROR_STOP=1 -Atqc \
    "SELECT count(*) FROM pg_stat_activity WHERE datname = '$SOURCE_DATABASE' AND pid <> pg_backend_pid()" \
    || fail 'Could not inspect development writer sessions.'
}

before_sessions=$(active_sessions)
[ "$before_sessions" = 0 ] \
  || fail 'Refusing backup while another source-database session exists.'

database_bytes=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT pg_database_size('$SOURCE_DATABASE')") \
  || fail 'Could not inspect development database size.'
case "$database_bytes" in
  ''|*[!0-9]*) fail 'Development database size was not numeric.' ;;
esac

available_kib=$(df -Pk "$BACKUP_ROOT" | awk 'NR == 2 {print $4}') \
  || fail 'Could not inspect backup destination capacity.'
case "$available_kib" in
  ''|*[!0-9]*) fail 'Backup destination free space was not numeric.' ;;
esac
available_bytes=$((available_kib * 1024))
required_bytes=$((database_bytes + MINIMUM_HEADROOM_BYTES))
echo "source bytes: $database_bytes"
echo "destination free bytes: $available_bytes"
[ "$available_bytes" -ge "$required_bytes" ] \
  || fail 'Backup destination lacks source-size plus 64 MiB headroom.'

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive_name="polybot-polytopia_dev-${timestamp}-${checkpoint}.dump"
digest_name="${archive_name}.sha256"
archive_path="$BACKUP_ROOT/$archive_name"
digest_path="$BACKUP_ROOT/$digest_name"
[ ! -e "$archive_path" ] && [ ! -L "$archive_path" ] \
  || fail 'Refusing to replace an existing backup archive.'
[ ! -e "$digest_path" ] && [ ! -L "$digest_path" ] \
  || fail 'Refusing to replace an existing backup digest.'

temporary_archive=$(mktemp "$BACKUP_ROOT/.polybot-backup.partial.XXXXXX") \
  || fail 'Could not create a private temporary archive.'
temporary_digest=$(mktemp "$BACKUP_ROOT/.polybot-digest.partial.XXXXXX") \
  || fail 'Could not create a private temporary digest.'

pg_dump \
  --host="$SOURCE_HOST" \
  --port="$SOURCE_PORT" \
  --username=postgres \
  --dbname="$SOURCE_DATABASE" \
  --format=custom \
  --compress=9 \
  --no-owner \
  --no-acl \
  --lock-wait-timeout=10s \
  --file="$temporary_archive" \
  || fail 'pg_dump failed; no archive was published.'
[ -s "$temporary_archive" ] \
  || fail 'pg_dump produced an empty archive.'
pg_restore --list "$temporary_archive" >/dev/null \
  || fail 'pg_restore could not validate the temporary archive.'

after_sessions=$(active_sessions)
[ "$after_sessions" = 0 ] \
  || fail 'A source-database session appeared during backup; no archive was published.'

archive_digest=$(sha256sum "$temporary_archive" | awk '{print $1}') \
  || fail 'Could not digest the temporary archive.'
case "$archive_digest" in
  *[!0-9a-f]*|'') fail 'Archive digest was malformed.' ;;
esac
[ "${#archive_digest}" -eq 64 ] || fail 'Archive digest was malformed.'
printf '%s  %s\n' "$archive_digest" "$archive_name" >"$temporary_digest" \
  || fail 'Could not write the temporary digest sidecar.'

mv -- "$temporary_archive" "$archive_path" \
  || fail 'Could not publish the validated archive.'
temporary_archive=
published_archive=$archive_path
mv -- "$temporary_digest" "$digest_path" \
  || fail 'Could not publish the archive digest sidecar.'
temporary_digest=
published_archive=
rmdir -- "$backup_lock" || fail 'Could not release the backup lock.'
backup_lock=

echo 'Development container database backup complete.'
echo "archive: $archive_name"
echo "sha256: $archive_digest"
echo 'retention: keep this pair until a newer archive has passed a fresh-volume restore drill'
