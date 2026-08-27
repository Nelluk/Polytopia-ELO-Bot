# Beta-only feature and fixture cleanup record

Status: completed in source; development-guild command removal and replacement
beta deployment are recorded in the implementing commit/deployment evidence.

## Retired scope

The Beta Lab, `/whattotest`, guided personas, self-service lanes, synthetic
result/leaderboard fixtures, and `/operator beta` supported human acceptance of
the slash-command modernization branch. That work is now on public `master`,
so the temporary testing surface and its manifests, scripts, tests, control
socket, and readiness/release workflows were removed.

The superseded root `polybot` wrapper, user-systemd beta service, duplicate
development Dockerfile/Compose definitions, container doctor, recovery jobs,
and historical operator runbooks were removed at the same boundary. The
upstream beta now uses `compose.beta.yaml`, the root `Dockerfile`, root ignored
configuration files, and ordinary Docker Compose commands.

## Live inventory result

The pre-removal inventory proved:

- zero owned result-scenario games;
- zero owned leaderboard-showcase players or games;
- zero active self-service lanes;
- no owned Beta Lab House or Team;
- no owned Beta Lab Discord roles; and
- no persona/session state files in the persistent log volume.

No development database or Discord fixture cleanup was therefore needed. The
incompatible `The Jets`, `The Ronin`, and `The Sparkies` fixture-readiness rows
were retained because they are ordinary mirrored development data, not exact
owned Beta Lab resources. No database backup was created solely for this
source-only cleanup because no database write occurred.

## Controls retained

The cleanup deliberately retains:

- the direct Compose beta deployment and host-PostgreSQL socket mount;
- exact development application, guild, database, and role validation;
- startup schema preflight and disabled startup command synchronization;
- the database-wide advisory writer lock and supervised process lock;
- exact image/checkpoint provenance and no-published-port boundary;
- the normal schema planning service and historical mirror tooling; and
- generic application, database, and Discord regression tests.

Production and its data were outside this cleanup. No production service,
configuration, schema, command tree, or database action was part of it.
