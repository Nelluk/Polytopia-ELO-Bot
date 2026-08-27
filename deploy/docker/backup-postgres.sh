#!/bin/sh

# Create, validate, checksum, and atomically publish one logical PostgreSQL
# custom-format archive in the bind-mounted backup directory.

set -eu
umask 077

: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"

backup_root=/backups
prefix=${POLYBOT_BACKUP_PREFIX:-polybot}
case "$prefix" in
  ''|*[!A-Za-z0-9._-]*)
    echo 'Backup refused: POLYBOT_BACKUP_PREFIX may contain only letters, numbers, dot, underscore, and dash.' >&2
    exit 2
    ;;
esac

[ -d "$backup_root" ] || {
  echo 'Backup refused: /backups is not a directory.' >&2
  exit 2
}

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive_name="${prefix}-${timestamp}.dump"
archive_path="$backup_root/$archive_name"
digest_path="$archive_path.sha256"
[ ! -e "$archive_path" ] && [ ! -e "$digest_path" ] || {
  echo "Backup refused: $archive_name already exists." >&2
  exit 2
}

temporary_archive=$(mktemp "$backup_root/.${prefix}.dump.partial.XXXXXX")
temporary_digest=$(mktemp "$backup_root/.${prefix}.sha256.partial.XXXXXX")
cleanup() {
  [ -z "$temporary_archive" ] || rm -f -- "$temporary_archive"
  [ -z "$temporary_digest" ] || rm -f -- "$temporary_digest"
}
trap cleanup EXIT
trap 'cleanup; exit 129' HUP
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

pg_dump --format=custom --file="$temporary_archive" "$PGDATABASE"
pg_restore --list "$temporary_archive" >/dev/null
digest=$(sha256sum "$temporary_archive" | awk '{print $1}')
printf '%s  %s\n' "$digest" "$archive_name" >"$temporary_digest"

mv -- "$temporary_archive" "$archive_path"
temporary_archive=
if ! mv -- "$temporary_digest" "$digest_path"; then
  rm -f -- "$archive_path"
  exit 1
fi
temporary_digest=
trap - EXIT HUP INT TERM

echo "Backup created: $archive_name"
echo "SHA-256: $digest"
