# Documentation map

Use this page to distinguish public installation instructions from upstream
operations and historical engineering evidence. A historical document may
contain commands that were correct for its checkpoint; its presence is not a
recommendation or authorization to run them now.

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

- [Database/slash modernization ledger](DATABASE_AND_SLASH_MODERNIZATION.md) —
  current architectural rules plus a large append-only execution history.
- [Modernization collaboration workflow](MODERNIZATION_COLLABORATION_WORKFLOW.md)
  — worktree and handoff rules for planned database/slash work.
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

## Historical migrations and release evidence

These files preserve completed decisions and checkpoint evidence. Do not use
them as current deployment authority:

- [Python/dependency upgrade handoff](DEPENDENCY_UPGRADE_HANDOFF.md),
  [PostgreSQL upgrade plan](POSTGRESQL_UPGRADE_PLAN.md),
  [production cutover](PRODUCTION_CUTOVER.md), and
  [post-upgrade cleanup](POST_UPGRADE_CLEANUP.md).
- [Modernization readiness audit](MODERNIZATION_PRODUCTION_READINESS_AUDIT.md),
  [adversarial review](MODERNIZATION_PRE_PRODUCTION_REVIEW.md),
  [release-candidate evidence](MODERNIZATION_RELEASE_CANDIDATE.md),
  [modernization cutover](MODERNIZATION_PRODUCTION_CUTOVER.md), and
  [final-review prompt](MODERNIZATION_FINAL_ADVERSARIAL_REVIEW_PROMPT.md).
- [Slash taxonomy review](SLASH_COMMAND_TAXONOMY_REVIEW.md),
  [player identity audit](PLAYER_IDENTITY_AND_PREFERENCES_AUDIT.md), and
  [beta-only cleanup record](BETA_ONLY_CLEANUP.md).
- Release-specific schema records for
  [player timezone](PRODUCTION_TIMEZONE_MIGRATION.md),
  [player badges](PLAYER_BADGES_MIGRATION.md), and
  [game keep-active](GAME_KEEP_ACTIVE_MIGRATION.md). New installations should
  use the generic schema workflow documented in the self-hosting guides.
- [Retired systemd release wrapper](PRODUCTION_RELEASE_WRAPPER.md) — historical
  behavior only; do not install or invoke it.
