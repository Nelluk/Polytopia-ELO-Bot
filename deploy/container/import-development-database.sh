#!/bin/sh

# Import the one reviewed host-development archive into the ordinary bundled
# development database. The target, source artifact, and expected bounded
# counts are fixed so this job cannot become a general-purpose restore path.

set -eu
umask 077

IMPORT_HOST=postgres
IMPORT_PORT=5432
TARGET_DATABASE=polytopia_dev
APPLICATION_ROLE=polybot_dev
BACKUP_ROOT=/backups
ADMIN_SECRET=/run/secrets/postgres_admin_password
APPLICATION_SECRET=/run/secrets/polybot_database_password
EXPECTED_ARCHIVE=polybot-polytopia_dev-20260812T123355Z-d27d6c83508ad00ef4e28d4eabad5fcddcf3189f.dump
EXPECTED_DIGEST=a1ab30a068a068da6ce207d41d8b840a31291d721b49ee4e1d7a9c464958aa8b
EXPECTED_COUNTS='71|4|44|15|3|48|24'

fail() {
  echo "Development database import refused: $1" >&2
  exit 2
}

[ "${POLYBOT_ENV:-}" = development ] \
  || fail 'POLYBOT_ENV must be development.'
[ "${PGHOST:-}" = "$IMPORT_HOST" ] \
  || fail 'PGHOST must be the bundled postgres service.'
[ "${PGPORT:-}" = "$IMPORT_PORT" ] \
  || fail 'PGPORT must be 5432.'
[ "${PGDATABASE:-}" = postgres ] \
  || fail 'PGDATABASE must be the bundled maintenance database.'
[ "${PGUSER:-}" = postgres ] \
  || fail 'PGUSER must be the bundled administrative role.'

archive_name=${POLYBOT_BACKUP_ARCHIVE:-}
[ "$archive_name" = "$EXPECTED_ARCHIVE" ] \
  || fail 'POLYBOT_BACKUP_ARCHIVE must be the exact reviewed archive basename.'
archive_path="$BACKUP_ROOT/$archive_name"
digest_path="${archive_path}.sha256"
[ -f "$archive_path" ] && [ ! -L "$archive_path" ] && [ -s "$archive_path" ] \
  || fail 'The reviewed archive must be one nonempty regular non-symlink file.'
[ -f "$digest_path" ] && [ ! -L "$digest_path" ] \
  || fail 'The archive digest must be one regular non-symlink file.'

archive_digest=$(sha256sum "$archive_path" | awk '{print $1}') \
  || fail 'Could not digest the reviewed archive.'
[ "$archive_digest" = "$EXPECTED_DIGEST" ] \
  || fail 'Archive digest does not match the reviewed transfer.'
expected_digest_line="$archive_digest  $archive_name"
digest_bytes=$(wc -c <"$digest_path") \
  || fail 'Could not inspect the archive digest sidecar.'
[ "$digest_bytes" -eq $((${#expected_digest_line} + 1)) ] \
  || fail 'Archive digest sidecar has an unexpected shape.'
[ "$(cat "$digest_path")" = "$expected_digest_line" ] \
  || fail 'Archive digest sidecar does not exactly match the reviewed archive.'
pg_restore --list "$archive_path" >/dev/null \
  || fail 'pg_restore could not read the reviewed archive.'

confirmation="IMPORT $TARGET_DATABASE $archive_digest"
echo 'Development bundled database import plan'
echo "archive: $archive_name"
echo "sha256: $archive_digest"
echo "fixed service: $IMPORT_HOST"
echo "fixed target: $TARGET_DATABASE"
echo "fixed application role: $APPLICATION_ROLE"
echo 'required state: bot stopped, safely provisioned target, no public application relations'
echo 'writes: one-transaction restore as polybot_dev with archived ownership and ACL omitted'
echo 'verification: required tables, winner FK, ownership, and reviewed bounded data counts'
echo "confirmation: $confirmation"

provided_confirmation=${POLYBOT_IMPORT_CONFIRMATION:-}
if [ -z "$provided_confirmation" ]; then
  echo 'Plan only; archive validation was local and no PostgreSQL connection or write was attempted.'
  exit 0
fi
[ "$provided_confirmation" = "$confirmation" ] \
  || fail 'Import confirmation does not match the exact target and archive digest.'

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
admin_identity=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT current_database() || ':' || current_user") \
  || fail 'Could not inspect the bundled PostgreSQL administrative identity.'
[ "$admin_identity" = 'postgres:postgres' ] \
  || fail 'Import requires the postgres maintenance identity.'
server_version_num=$(psql -X -v ON_ERROR_STOP=1 -Atqc 'SHOW server_version_num') \
  || fail 'Could not inspect the bundled PostgreSQL server version.'
case "$server_version_num" in
  18????) : ;;
  *) fail 'Import requires PostgreSQL major 18.' ;;
esac

target_sessions=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT count(*) FROM pg_stat_activity WHERE datname = '$TARGET_DATABASE'") \
  || fail 'Could not inspect target database sessions.'
[ "$target_sessions" = 0 ] \
  || fail 'The bot or another target-database client is connected; stop it before import.'

safe_role=$(psql -X -v ON_ERROR_STOP=1 -Atqc "
  SELECT count(*)
  FROM pg_roles AS role
  WHERE role.rolname = '$APPLICATION_ROLE'
    AND role.rolcanlogin
    AND NOT role.rolsuper
    AND NOT role.rolcreatedb
    AND NOT role.rolcreaterole
    AND NOT role.rolreplication
    AND NOT role.rolbypassrls
") || fail 'Could not inspect the provisioned application role.'
[ "$safe_role" = 1 ] \
  || fail 'The fixed application role is absent or over-privileged.'
safe_database=$(psql -X -v ON_ERROR_STOP=1 -Atqc "
  SELECT count(*)
  FROM pg_database AS database
  WHERE database.datname = '$TARGET_DATABASE'
    AND pg_get_userbyid(database.datdba) = '$APPLICATION_ROLE'
    AND database.datallowconn
    AND NOT database.datistemplate
") || fail 'Could not inspect the provisioned target database.'
[ "$safe_database" = 1 ] \
  || fail 'The fixed target is absent, unsafe, or not owned by the application role.'

PGPASSWORD=$application_password
export PGPASSWORD
target_identity=$(psql \
  --host="$IMPORT_HOST" \
  --port="$IMPORT_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$TARGET_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT current_database() || ':' || current_user") \
  || fail 'Could not connect with the provisioned application identity.'
[ "$target_identity" = "$TARGET_DATABASE:$APPLICATION_ROLE" ] \
  || fail 'Application connection reached an unexpected database or role.'
public_relations=$(psql \
  --host="$IMPORT_HOST" \
  --port="$IMPORT_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$TARGET_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT count(*)
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
  ") || fail 'Could not inspect target database freshness.'
[ "$public_relations" = 0 ] \
  || fail 'Target is not fresh; public application relations already exist.'

PGPASSWORD=$admin_password
export PGPASSWORD
target_sessions=$(psql -X -v ON_ERROR_STOP=1 -Atqc \
  "SELECT count(*) FROM pg_stat_activity WHERE datname = '$TARGET_DATABASE'") \
  || fail 'Could not recheck target database sessions.'
[ "$target_sessions" = 0 ] \
  || fail 'A target-database client connected during preflight; import was not started.'

PGPASSWORD=$application_password
export PGPASSWORD
pg_restore \
  --host="$IMPORT_HOST" \
  --port="$IMPORT_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$TARGET_DATABASE" \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-acl \
  "$archive_path" \
  || fail 'Import failed; preserve the target for diagnosis and provision a fresh target before retrying.'

missing_tables=$(psql \
  --host="$IMPORT_HOST" \
  --port="$IMPORT_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$TARGET_DATABASE" \
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
  || fail 'Imported database is missing required application tables.'

winner_foreign_key=$(psql \
  --host="$IMPORT_HOST" \
  --port="$IMPORT_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$TARGET_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT count(*)
    FROM pg_constraint AS constraint
    JOIN pg_class AS source_table ON source_table.oid = constraint.conrelid
    JOIN pg_class AS target_table ON target_table.oid = constraint.confrelid
    JOIN pg_attribute AS source_column
      ON source_column.attrelid = source_table.oid
     AND source_column.attnum = constraint.conkey[1]
    WHERE constraint.contype = 'f'
      AND source_table.relname = 'game'
      AND source_column.attname = 'winner_id'
      AND target_table.relname = 'gameside'
  ") || fail 'Could not verify the winner foreign key.'
[ "$winner_foreign_key" = 1 ] \
  || fail 'Imported database does not contain the required winner foreign key.'

wrong_owners=$(psql \
  --host="$IMPORT_HOST" \
  --port="$IMPORT_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$TARGET_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT count(*)
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'S')
      AND pg_get_userbyid(relation.relowner) <> '$APPLICATION_ROLE'
  ") || fail 'Could not verify imported object ownership.'
[ "$wrong_owners" = 0 ] \
  || fail 'Imported application tables or sequences have the wrong owner.'

imported_counts=$(psql \
  --host="$IMPORT_HOST" \
  --port="$IMPORT_PORT" \
  --username="$APPLICATION_ROLE" \
  --dbname="$TARGET_DATABASE" \
  -X -v ON_ERROR_STOP=1 -Atqc "
    SELECT
      (SELECT count(*) FROM game WHERE guild_id = 478571892832206869),
      (SELECT count(*) FROM house),
      (SELECT count(*) FROM player WHERE guild_id = 478571892832206869),
      (SELECT count(*) FROM team WHERE guild_id = 478571892832206869),
      (SELECT count(*) FROM game WHERE id BETWEEN 2286 AND 2288),
      (SELECT count(*) FROM game WHERE id BETWEEN 200 AND 247),
      (SELECT count(*) FROM player WHERE id BETWEEN 163 AND 186)
  ") || fail 'Could not verify bounded imported data counts.'
[ "$imported_counts" = "$EXPECTED_COUNTS" ] \
  || fail 'Imported data counts do not match the digest-bound reviewed archive.'

echo 'Development bundled database import complete.'
echo "verified database: $TARGET_DATABASE"
echo 'verified counts: guild_games=71 houses=4 guild_players=44 guild_teams=15 beta_games=3 showcase_games=48 showcase_players=24'
echo 'The bot remains stopped; this job never starts it.'
