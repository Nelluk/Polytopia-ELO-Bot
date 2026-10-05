# Release notes

## Unreleased — Polytopia-name profile lookup

- `/player show name:POLYTOPIA_NAME` searches registered players in the current
  server. Case-insensitive exact matches take precedence over partial matches.
  This option searches stored Polytopia names, while prefix commands retain
  their existing Discord-name-first matching behavior.
- A single match opens the existing public player profile. Multiple matches
  open a private chooser showing Polytopia and Discord names. Only the requester
  can select; the chooser expires after two minutes. More than 25 matches require
  a narrower name. Selecting a player reloads their current server registration.
- `/player show member:@MEMBER` and the default self-profile remain supported.
  Supply either `member` or `name`; supplying both returns a private error.
- No schema changes are required. Deploying this command change requires the
  explicit guild-only command plan/apply process described in
  `docs/APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md`; startup does not synchronize
  commands.

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
