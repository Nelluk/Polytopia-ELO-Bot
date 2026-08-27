# Retired constrained PolyElo production release wrapper

Status: **historical systemd-era record; do not install or invoke**

GreenCloud production now runs through Docker Compose. The current authority is
[PRODUCTION_DOCKER.md](PRODUCTION_DOCKER.md), using ordinary reviewed
`docker compose` operations and separately inspected schema and Discord command
changes.

The installed `/srv/polyelo/bin/polyelo-release` copy is retained temporarily
as rollback-era material only. It controls the disabled `polyelo.service` and
must not be invoked while Compose is the active supervisor. Deciding when to
remove that host copy and its sudoers rule is a separate operational cleanup.

## Historical behavior

Before the Docker cutover, the root-controlled wrapper accepted no arguments,
required a clean production `master` and an active `polyelo.service`, stopped
that service, and delegated to `scripts/production_release.sh` as the
unprivileged `polyelo` account. The runner applied registered additive
migrations and deployed commands only to Main and PolyChampions under the
empty-global guard before the wrapper restarted systemd.

The repository retains the former wrapper, installer, sudoers template,
systemd unit, runner, and regression tests until their separately reviewed
source cleanup. Their presence is historical/rollback evidence, not a supported
release interface or self-hosting requirement.
