#!/bin/sh

# Restore one checksum-paired custom archive into a freshly initialized,
# relation-empty application database. Use only with a new external volume.

set -eu

: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"

[ "$#" -eq 1 ] || {
  echo 'Usage: docker compose run --rm restore ARCHIVE.dump' >&2
  exit 2
}

archive_name=$1
case "$archive_name" in
  ''|*/*|*[!A-Za-z0-9._-]*|*.dump.dump)
    echo 'Restore refused: pass one .dump basename from ./backups.' >&2
    exit 2
    ;;
esac
case "$archive_name" in
  *.dump) ;;
  *)
    echo 'Restore refused: archive name must end in .dump.' >&2
    exit 2
    ;;
esac

archive_path="/backups/$archive_name"
digest_path="$archive_path.sha256"
[ -f "$archive_path" ] && [ ! -L "$archive_path" ] || {
  echo "Restore refused: archive is absent or unsafe: $archive_name" >&2
  exit 2
}
[ -f "$digest_path" ] && [ ! -L "$digest_path" ] || {
  echo "Restore refused: checksum is absent or unsafe: $archive_name.sha256" >&2
  exit 2
}

expected_digest_line=$(cat "$digest_path")
actual_digest=$(sha256sum "$archive_path" | awk '{print $1}')
[ "$expected_digest_line" = "$actual_digest  $archive_name" ] || {
  echo 'Restore refused: SHA-256 verification failed.' >&2
  exit 2
}
pg_restore --list "$archive_path" >/dev/null || {
  echo 'Restore refused: pg_restore could not read the archive catalog.' >&2
  exit 2
}

relation_count=$(psql --no-psqlrc --tuples-only --no-align \
  --dbname "$PGDATABASE" <<'SQL'
SELECT count(*)
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f');
SQL
)
[ "$relation_count" = 0 ] || {
  echo "Restore refused: target database is not empty ($relation_count public relations)." >&2
  exit 2
}

pg_restore \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-acl \
  --dbname "$PGDATABASE" \
  "$archive_path"

echo "Restore completed: $archive_name"
