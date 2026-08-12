# Development container secrets

Create both local files in this directory before using the bundled PostgreSQL
stack:

- `postgres-admin-password.txt`: a strong password used only by the bundled
  `postgres` administrative role;
- `polybot-database-password.txt`: the password for the non-superuser
  `polybot_dev` application role.

The second file must contain exactly the same single-line value as
`psql_password` in the ignored `deploy/container/config.development.ini`.
Neither file is tracked. Keep each file to one nonempty line and mode `0600`.
The deployment doctor validates presence, mode, single-line shape, and the
cross-file password match without printing secret content.
