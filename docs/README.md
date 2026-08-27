# Documentation map

Use this page to distinguish public installation instructions from upstream
production, development, and policy operations. Completed designs, migrations,
and releases are kept in Git history instead of the current documentation tree.

## Independent self-hosting

- [Docker Compose](DOCKER.md) — recommended installation, updates, backups,
  restore drills, and unattended-production baseline.
- [Native self-hosting](SELF_HOSTING.md) — alternative for operators who
  already manage Python and PostgreSQL.
- [Test database setup](DATABASE_SETUP.md) — isolated development bot/database.
- [Privacy policy](../PRIVACY.md), [security policy](../SECURITY.md), and
  [retention schedule](DATA_RETENTION.md) — upstream policies that independent
  operators must review and adapt.

## Upstream production operations

- [GreenCloud production Docker](PRODUCTION_DOCKER.md) — active upstream
  production authority; not the generic self-hosting path.
- [Application-command deployment](APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md)
  — upstream explicit guild-only planning, inspection, and synchronization.
- [Reporting export](REPORTING_EXPORT.md) — DuckDB reporting snapshot boundary.

## Upstream development operations

- [Development Docker](DEVELOPMENT_DOCKER.md) — active upstream beta interface.
- [Development guild configuration](DEVELOPMENT_GUILD_CONFIGURATION.md) —
  current database-authority model, operator surface, and safety boundary.
- [Development feedback](DEVELOPMENT_BETA_FEEDBACK.md) and
  [historical mirror](DEVELOPMENT_HISTORICAL_MIRROR.md) runbooks.
- [Privileged-intent screenshot guide](PRIVILEGED_INTENT_SCREENSHOT_GUIDE.md).

## Policy and data operations

- [Privacy request runbook](PRIVACY_REQUEST_RUNBOOK.md) and
  [privacy readiness checklist](PRIVACY_READINESS_CHECKLIST.md).
- [Data retention schedule](DATA_RETENTION.md).

## Git history

Checkpoint `a226ade9` preserves the completed dynamic-guild rollout documents
and targeted migration guides. Earlier checkpoint `e99ec18e` also preserves the
complete modernization ledger, dependency/PostgreSQL upgrades, adversarial
reviews, release candidates, systemd cutovers, retired wrapper assets, and
their tests. These records are deliberately absent from current `master`
because none is a supported deployment interface.

Retrieve a specific record when reconstructing history or an emergency path:

```bash
git show e99ec18e:docs/DATABASE_AND_SLASH_MODERNIZATION.md
git show a226ade9:docs/DYNAMIC_GUILD_CONFIGURATION_DESIGN.md
git show e99ec18e:docs/MODERNIZATION_PRODUCTION_CUTOVER.md
git show e99ec18e:deploy/systemd/polyelo.service
```

Historical commands must be adapted and revalidated against the current Docker
topology before use.
