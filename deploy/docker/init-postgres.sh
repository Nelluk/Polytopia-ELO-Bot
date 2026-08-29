#!/bin/sh

# Official PostgreSQL entrypoint hook for a brand-new bundled volume. It
# creates one restricted application role and its owned database exactly once.

set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POLYBOT_DATABASE_NAME:?POLYBOT_DATABASE_NAME is required}"
: "${POLYBOT_DATABASE_USER:?POLYBOT_DATABASE_USER is required}"
: "${POLYBOT_DATABASE_PASSWORD:?POLYBOT_DATABASE_PASSWORD is required}"

case "$POSTGRES_PASSWORD:$POLYBOT_DATABASE_PASSWORD" in
  *REPLACE_*|*YOUR_*)
    echo 'Bundled database initialization refused: replace password placeholders.' >&2
    exit 2
    ;;
esac

if [ "$POLYBOT_DATABASE_USER" = "$POSTGRES_USER" ]; then
  echo 'Bundled database initialization refused: application and admin roles must differ.' >&2
  exit 2
fi
if [ "$POLYBOT_DATABASE_NAME" = "$POSTGRES_DB" ]; then
  echo 'Bundled database initialization refused: application and maintenance databases must differ.' >&2
  exit 2
fi

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_database="$POLYBOT_DATABASE_NAME" \
  --set=app_password="$POLYBOT_DATABASE_PASSWORD" \
  --set=app_user="$POLYBOT_DATABASE_USER" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION',
    :'app_user',
    :'app_password'
) WHERE NOT EXISTS (
    SELECT FROM pg_roles WHERE rolname = :'app_user'
) \gexec

SELECT format(
    'CREATE DATABASE %I OWNER %I',
    :'app_database',
    :'app_user'
) WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = :'app_database'
) \gexec
SQL

echo "Created bundled application database $POLYBOT_DATABASE_NAME and restricted role $POLYBOT_DATABASE_USER."
