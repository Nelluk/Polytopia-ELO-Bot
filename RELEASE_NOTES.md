# Release notes

## Unreleased — adaptable Compose examples

### Operator action required

- Active deployment files are now operator-owned and ignored. Before updating
  an existing checkout, preserve its current tracked Compose definition
  outside the checkout. After updating, restore it as the private
  `compose.yaml` and remove any `COMPOSE_FILE` value that names a retired
  tracked file. Running containers are unaffected while these files are
  prepared. New installations copy `compose.example.yaml` or
  `compose.external-postgres.example.yaml`.
- New recommended installations take all application database connection
  values from `.env`; they are no longer duplicated in `config.ini`.
- New installations default to database-backed guild configuration and must
  explicitly bootstrap the first guild before starting the bot.

### Explicit operations

- Normal startup still performs no schema changes and no Discord command
  synchronization.
- Existing databases require no schema change for this source release.
- A fresh installation follows the schema and first-guild plan/apply steps in
  `docs/DOCKER.md`.
- Existing Discord command trees require no synchronization solely because of
  this release-topology change.

Tracked examples can change between releases; ignored private deployment files
are never overwritten by Git. Compare examples and adopt only relevant
changes.
