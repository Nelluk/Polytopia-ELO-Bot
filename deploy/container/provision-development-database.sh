#!/bin/sh

set -eu

fail() {
    echo "Development database provisioning refused: $*" >&2
    exit 2
}

[ "${POLYBOT_ENV:-}" = development ] || fail "POLYBOT_ENV must be development"
[ "${PGHOST:-}" = postgres ] || fail "PGHOST must be the bundled postgres service"
[ "${PGDATABASE:-}" = postgres ] || fail "PGDATABASE must be the maintenance database"
[ "${PGUSER:-}" = postgres ] || fail "PGUSER must be the bundled administrative role"

admin_file=${POSTGRES_ADMIN_PASSWORD_FILE:-}
app_file=${POLYBOT_DATABASE_PASSWORD_FILE:-}
[ -n "$admin_file" ] && [ -f "$admin_file" ] || fail "admin password secret is missing"
[ -n "$app_file" ] && [ -f "$app_file" ] || fail "application password secret is missing"

admin_password=$(sed -n '1p' "$admin_file")
app_password=$(sed -n '1p' "$app_file")
[ -n "$admin_password" ] || fail "admin password secret is empty"
[ -n "$app_password" ] || fail "application password secret is empty"

export PGPASSWORD=$admin_password
export POLYBOT_DB_PASSWORD=$app_password

psql --no-psqlrc --set=ON_ERROR_STOP=1 <<'SQL'
\getenv app_password POLYBOT_DB_PASSWORD
SELECT current_database() = 'postgres' AND current_user = 'postgres' AS identity_ok
\gset
\if :identity_ok
\else
  \warn 'Refusing unexpected maintenance database or administrative role.'
  \quit 2
\endif
SELECT current_setting('server_version_num')::integer BETWEEN 180000 AND 189999 AS version_ok
\gset
\if :version_ok
\else
  \warn 'Refusing a PostgreSQL server outside reviewed major version 18.'
  \quit 2
\endif
SELECT pg_advisory_lock(728310812, 39017);
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', 'polybot_dev', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'polybot_dev')
\gexec
SELECT EXISTS (
         SELECT FROM pg_roles AS r
         WHERE r.rolname = 'polybot_dev'
           AND r.rolcanlogin
           AND NOT r.rolsuper
           AND NOT r.rolcreatedb
           AND NOT r.rolcreaterole
           AND NOT r.rolreplication
           AND NOT r.rolbypassrls
       ) AS role_ok
\gset
\if :role_ok
\else
  \warn 'Refusing an absent or over-privileged polybot_dev login role.'
  \quit 2
\endif
SELECT format('CREATE DATABASE %I OWNER %I', 'polytopia_dev', 'polybot_dev')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'polytopia_dev')
\gexec
SELECT EXISTS (
         SELECT FROM pg_database AS d
         WHERE d.datname = 'polytopia_dev'
           AND pg_get_userbyid(d.datdba) = 'polybot_dev'
       ) AS database_ok
\gset
\if :database_ok
\else
  \warn 'Refusing an absent or incorrectly owned polytopia_dev database.'
  \quit 2
\endif
SELECT pg_advisory_unlock(728310812, 39017);
SQL

echo "Development database provisioned: polytopia_dev owned by polybot_dev on PostgreSQL 18."
