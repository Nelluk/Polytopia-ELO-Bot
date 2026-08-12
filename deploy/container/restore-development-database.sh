#!/bin/sh

# Restore one verified development archive only into the dedicated recovery
# PostgreSQL service. The fixed hostname and target name are intentionally not
# configurable so this job cannot overwrite the normal development database.

set -eu
umask 077

RESTORE_HOST=restore-postgres
RESTORE_PORT=5432
RESTORE_DATABASE=polytopia_restore_verify
APPLICATION_ROLE=polybot_dev
BACKUP_ROOT=/backups
ADMIN_SECRET=/run/secrets/postgres_admin_password
APPLICATION_SECRET=/run/secrets/polybot_database_password

fail() {
  echo "ERROR: $1" >&2
  exit 2
}

[ "${POLYBOT_ENV:-}" = development ] \
  || fail 'POLYBOT_ENV must be development.'
[ "${PGHOST:-}" = "$RESTORE_HOST" ] \
  || fail 'PGHOST must be the isolated restore-postgres service.'
[ "${PGPORT:-}" = "$RESTORE_PORT" ] \
  || fail 'PGPORT must be 5432.'
[ "${PGDATABASE:-}" = postgres ] \
  || fail 'PGDATABASE must be the isolated maintenance database.'
[ "${PGUSER:-}" = postgres ] \
  || fail 'PGUSER must be the isolated administrative role.'

archive_name=${POLYBOT_BACKUP_ARCHIVE:-}
[ -n "$archive_name" ] \
  || fail 'Set POLYBOT_BACKUP_ARCHIVE to one reviewed archive basename.'
case "$archive_name" in
  */*|.*|*..*|*[!A-Za-z0-9._-]*) fail 'Backup archive must be one safe basename.' ;;
esac
printf '%s\n' "$archive_name" \
  | grep -Eq '^polybot-polytopia_dev-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{40}\.dump$' \
  || fail 'Backup archive name does not match the reviewed development format.'

archive_path="$BACKUP_ROOT/$archive_name"
digest_path="${archive_path}.sha256"
[ -f "$archive_path" ] && [ ! -L "$archive_path" ] && [ -s "$archive_path" ] \
  || fail 'Backup archive must be one nonempty regular non-symlink file.'
[ -f "$digest_path" ] && [ ! -L "$digest_path" ] \
  || fail 'Backup digest must be one regular non-symlink file.'
digest_bytes=$(wc -c <"$digest_path") \
  || fail 'Could not inspect the backup digest sidecar.'

archive_digest=$(sha256sum "$archive_path" | awk '{print $1}') \
  || fail 'Could not digest the selected archive.'
expected_digest_line="$archive_digest  $archive_name"
[ "$digest_bytes" -eq $((${#expected_digest_line} + 1)) ] \
  || fail 'Backup digest sidecar has an unexpected shape.'
[ "$(cat "$digest_path")" = "$expected_digest_line" ] \
  || fail 'Backup digest sidecar does not match the selected archive.'
pg_restore --list "$archive_path" >/dev/null \
  || fail 'pg_restore could not read the selected archive.'

confirmation="RESTORE $RESTORE_DATABASE $archive_digest"
echo 'Development container fresh-volume restore plan'
echo "archive: $archive_name"
echo "sha256: $archive_digest"
echo "isolated service: $RESTORE_HOST"
echo "target database: $RESTORE_DATABASE"
echo 'required initial state: fresh recovery volume with no application role or user database'
echo 'writes: create the application role/database and restore only inside the recovery volume'
echo "confirmation: $confirmation"

provided_confirmation=${POLYBOT_RESTORE_CONFIRMATION:-}
if [ -z "$provided_confirmation" ]; then
  echo 'Plan only; archive validation was local and no PostgreSQL connection was attempted.'
  exit 0
fi
[ "$provided_confirmation" = "$confirmation" ] \
  || fail 'Restore confirmation does not match the exact archive and target.'

for secret in "$ADMIN_SECRET" "$APPLICATION_SECRET"; do
  [ -f "$secret" ] && [ ! -L "$secret" ] \
    || fail 'A required PostgreSQL secret is not a regular file.'
  secret_lines=$(wc -l <"$secret") \
    || fail 'Could not inspect a required PostgreSQL secret.'
  [ "$secret_lines" -le 1 ] \
    || fail 'Each PostgreSQL secret must contain one nonempty line.'
done
admin_password=$(cat "$ADMIN_SECRET")
application_password=$(cat "$APPLICATION_SECRET")
case "$admin_password" in
  ''|*'
'*) fail 'PostgreSQL administrative secret must contain one nonempty line.' ;;
esac
case "$application_password" in
  ''|*'
'*) fail 'PostgreSQL application secret must contain one nonempty line.' ;;
esac
[ "$admin_password" != "$application_password" ] \
  || fail 'Administrative and application passwords must be distinct.'

PGPASSWORD=$admin_password
export PGPASSWORD
POLYBOT_DB_PASSWORD=$application_password
export POLYBOT_DB_PASSWORD
admin_identity=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT current_database() || ':' || current_user") \
  || fail 'Could not inspect the isolated PostgreSQL administrative identity.'
[ "$admin_identity" = 'postgres:postgres' ] \
  || fail 'Restore drill requires the postgres maintenance identity.'
server_version_num=$(psql -X -v ON_ERROR_STOP=1 -Atqc 'SHOW server_version_num') \
  || fail 'Could not inspect the isolated PostgreSQL server version.'
case "$server_version_num" in
  18????) : ;;
  *) fail 'Restore drill requires PostgreSQL major 18.' ;;
esac

nondefault_databases=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT count(*) FROM pg_database WHERE datistemplate = false AND datname <> 'postgres'") \
  || fail 'Could not inspect the isolated database inventory.'
custom_roles=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT count(*) FROM pg_roles WHERE rolname <> 'postgres' AND rolname !~ '^pg_'") \
  || fail 'Could not inspect the isolated role inventory.'
[ "$nondefault_databases" = 0 ] && [ "$custom_roles" = 0 ] \
  || fail 'Recovery service is not fresh; destroy only its recovery volume before retrying.'

psql -X -v ON_ERROR_STOP=1 <<'SQL'
\getenv app_password POLYBOT_DB_PASSWORD
SELECT format(
         'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
         'polybot_dev',
         :'app_password'
       )
\gexec
SELECT format('CREATE DATABASE %I OWNER %I', 'polytopia_restore_verify', 'polybot_dev')
\gexec
SQL

PGPASSWORD=$application_password
export PGPASSWORD
pg_restore \
  --host="$RESTORE_HOST" \
  --port="$RESTORE_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$RESTORE_DATABASE" \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-acl \
  "$archive_path" \
  || fail 'Restore failed; preserve the isolated volume for diagnosis, then destroy it before retrying.'

missing_tables=$(psql \
  --host="$RESTORE_HOST" \
  --port="$RESTORE_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$RESTORE_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT count(*)
    FROM (VALUES
      ('apiapplication'), ('auction'), ('bid'), ('configuration'),
      ('discordmember'), ('game'), ('gamelog'), ('gameside'), ('house'),
      ('lineup'), ('player'), ('playerhousepreference'), ('squad'),
      ('squadmember'), ('team'), ('team_server_broadcast_message'), ('tribe')
    ) AS required(table_name)
    WHERE to_regclass('public.' || required.table_name) IS NULL
  ") || fail 'Could not verify required application tables.'
[ "$missing_tables" = 0 ] \
  || fail 'Restored database is missing required application tables.'

winner_foreign_key=$(psql \
  --host="$RESTORE_HOST" \
  --port="$RESTORE_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$RESTORE_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT count(*)
    FROM pg_constraint AS c
    JOIN pg_class AS source_table ON source_table.oid = c.conrelid
    JOIN pg_class AS target_table ON target_table.oid = c.confrelid
    JOIN pg_attribute AS source_column
      ON source_column.attrelid = source_table.oid
     AND source_column.attnum = c.conkey[1]
    WHERE c.contype = 'f'
      AND source_table.relname = 'game'
      AND source_column.attname = 'winner_id'
      AND target_table.relname = 'gameside'
  ") || fail 'Could not verify the winner foreign key.'
[ "$winner_foreign_key" = 1 ] \
  || fail 'Restored database does not contain the required winner foreign key.'

wrong_table_owners=$(psql \
  --host="$RESTORE_HOST" \
  --port="$RESTORE_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$RESTORE_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT count(*)
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'S')
      AND pg_get_userbyid(relation.relowner) <> '$APPLICATION_ROLE'
  ") || fail 'Could not verify restored object ownership.'
[ "$wrong_table_owners" = 0 ] \
  || fail 'Restored application tables or sequences have the wrong owner.'

restored_counts=$(psql \
  --host="$RESTORE_HOST" \
  --port="$RESTORE_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$RESTORE_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT
      (SELECT count(*) FROM game WHERE guild_id = 478571892832206869),
      (SELECT count(*) FROM house),
      (SELECT count(*) FROM player WHERE guild_id = 478571892832206869),
      (SELECT count(*) FROM team WHERE guild_id = 478571892832206869),
      (SELECT count(*) FROM game WHERE id BETWEEN 2286 AND 2288),
      (SELECT count(*) FROM game WHERE id BETWEEN 200 AND 247),
      (SELECT count(*) FROM player WHERE id BETWEEN 163 AND 186)
  ") || fail 'Could not verify bounded restored data counts.'
printf '%s\n' "$restored_counts" \
  | grep -Eq '^[0-9]+(\|[0-9]+){6}$' \
  || fail 'Restored data counts have an unexpected shape.'

echo 'Fresh-volume restore drill complete.'
echo "verified database: $RESTORE_DATABASE"
echo 'verified: required tables, game.winner_id foreign key, and application ownership'
echo "verified counts: $restored_counts"
echo 'The isolated recovery volume is intentionally retained for inspection.'
