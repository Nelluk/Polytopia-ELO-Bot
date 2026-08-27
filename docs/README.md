# Documentation map

Use this page to distinguish public installation instructions from upstream
operations and current engineering references. Completed migration and release
records are kept in Git history instead of the current documentation tree.

## Independent self-hosting

- [Docker Compose](DOCKER.md) — recommended installation, updates, backups,
  restore drills, and unattended-production baseline.
- [Native self-hosting](SELF_HOSTING.md) — alternative for operators who
  already manage Python and PostgreSQL.
- [Test database setup](DATABASE_SETUP.md) — isolated development bot/database.
- [Privacy policy](../PRIVACY.md), [security policy](../SECURITY.md), and
  [retention schedule](DATA_RETENTION.md) — upstream policies that independent
  operators must review and adapt.

## Current upstream operations

- [GreenCloud production Docker](PRODUCTION_DOCKER.md) — active upstream
  production authority; not the generic self-hosting path.
- [Development Docker](DEVELOPMENT_DOCKER.md) — active upstream beta interface.
- [Application-command deployment](APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md)
  — upstream explicit guild-only planning, inspection, and synchronization.
- [Reporting export](REPORTING_EXPORT.md) — DuckDB reporting snapshot boundary.
- [Privacy request runbook](PRIVACY_REQUEST_RUNBOOK.md) and
  [privacy readiness checklist](PRIVACY_READINESS_CHECKLIST.md).
- [Privileged-intent screenshot guide](PRIVILEGED_INTENT_SCREENSHOT_GUIDE.md).

## Current engineering references

- [Database/slash engineering contract](DATABASE_AND_SLASH_MODERNIZATION.md) —
  current architecture, safety boundaries, compatibility decisions, and work
  protocol.
- [Dynamic guild configuration design](DYNAMIC_GUILD_CONFIGURATION_DESIGN.md).
- Development database-authority runbooks:
  [storage](DEVELOPMENT_GUILD_CONFIGURATION_STORAGE.md),
  [shadow reads](DEVELOPMENT_GUILD_CONFIGURATION_SHADOW.md),
  [authority](DEVELOPMENT_GUILD_CONFIGURATION_AUTHORITY.md),
  [control](DEVELOPMENT_GUILD_CONFIGURATION_CONTROL.md),
  [drafts](DEVELOPMENT_GUILD_CONFIGURATION_DRAFTS.md),
  [command capabilities](DEVELOPMENT_GUILD_COMMAND_CAPABILITIES.md),
  [onboarding](DEVELOPMENT_GUILD_ONBOARDING.md),
  [lifecycle](DEVELOPMENT_GUILD_LIFECYCLE.md), and
  [delegation](DEVELOPMENT_GUILD_CONFIGURATION_DELEGATION.md).
- [Development feedback](DEVELOPMENT_BETA_FEEDBACK.md) and
  [historical mirror](DEVELOPMENT_HISTORICAL_MIRROR.md) runbooks.
- Targeted schema references for [player timezone](PRODUCTION_TIMEZONE_MIGRATION.md),
  [player badges](PLAYER_BADGES_MIGRATION.md), and
  [game keep-active](GAME_KEEP_ACTIVE_MIGRATION.md). New installations should
  use the generic schema workflow documented in the self-hosting guides.

## Git history

Pre-cleanup checkpoint `e99ec18e` preserves the complete modernization ledger,
dependency/PostgreSQL upgrades, adversarial reviews, release candidates,
systemd cutovers, retired wrapper assets, and their tests. They are deliberately
absent from current `master` because none is a supported deployment interface.

Retrieve a specific record when reconstructing history or an emergency path:

```bash
git show e99ec18e:docs/DATABASE_AND_SLASH_MODERNIZATION.md
git show e99ec18e:docs/MODERNIZATION_PRODUCTION_CUTOVER.md
git show e99ec18e:deploy/systemd/polyelo.service
```

Historical commands must be adapted and revalidated against the current Docker
topology before use.
