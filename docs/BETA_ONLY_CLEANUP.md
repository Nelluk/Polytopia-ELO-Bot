# Beta-only feature and fixture cleanup

Status: designated follow-up; documentation only. No source removal, Discord
mutation, or development-database cleanup is authorized by this document.

## Why this exists

The Beta Lab, `/whattotest`, and their purpose-built fixtures supported human
acceptance of the slash-command modernization branch. That branch is now
merged. Missing Beta Lab packs no longer indicate a bad beta deployment, and
restoring those packs solely to report `ready` would create work and synthetic
data without a current product purpose.

The ordinary beta deployment remains useful. This cleanup must not remove or
weaken its Compose deployment, development application/guild/database identity
guards, startup schema preflight, PostgreSQL writer lock, exact image/checkpoint
tracking, disabled startup command synchronization, or no-published-port
boundary.

## Candidate source cleanup

Treat this as one reviewed source unit after live resources are inventoried:

- remove the development-only `/whattotest` command and its dashboard,
  catalog, guide, session, persona, worker, and manifest layers;
- remove Beta Lab control operations and wrapper commands that exist only to
  report, refresh, reconcile, or notify for those packs;
- remove the tracked Beta Lab manifests, management scripts, dedicated docs,
  and their focused tests;
- remove fixture creation and operator flows only where read-only dependency
  tracing proves they have no remaining independent development purpose; and
- update command policy, startup persona revocation, Compose environment, and
  cross-cutting tests that currently integrate the retired surface.

Likely files include `modules/beta_lab_*`, `modules/beta_testing_*`,
`scripts/manage_beta_lab*.py`, `data/development/beta_lab_*.json`, and their
tests. `modules/dev_fixtures.py`, `modules/operator_beta_fixtures*`, and
`scripts/manage_dev_fixtures.py` also contain beta-only scenarios, but they
must be classified by dependency and ownership rather than deleted by filename.
Generic test helpers, runtime safety controls, and ordinary beta deployment
assets are out of scope.

The superseded wrapper/user-systemd deployment is a second classification
group. Review the root `polybot` wrapper, `deploy/container`,
`deploy/systemd/polybot-development-beta@.service`, readiness/control scripts,
and their tests and docs for removal once no rollback or reusable maintenance
function depends on them. Do not remove `compose.beta.yaml`, `Dockerfile`,
`scripts/run_development_beta.py`, the database writer audit/lock, schema
preflight, or other assets used by the current direct-Compose beta merely
because their names contain `beta` or `development`.

Removing `/whattotest` from source does not remove the already published guild
command. A reviewed development-guild command synchronization is a separate,
explicitly authorized operation. Startup synchronization remains disabled.

## Candidate live-resource cleanup

Live cleanup is destructive and requires separate authorization after a
read-only inventory proves exact ownership. Candidate resources include:

- active self-service session games identified by their exact Beta Lab
  ownership markers;
- the exact game-results, leaderboard-showcase, and server-structure fixtures,
  but only when their existing ownership predicates distinguish them from
  mirrored or historical development data;
- the dedicated `Beta Lab House`, `Beta Lab Team`, `Beta Lab Team` role, and
  `Beta Lab Staff` role, including only their owned member assignments; and
- Beta Lab persona/session state files in the persistent log volume after no
  live session depends on them.

Do not delete the development database, Compose volumes, normal configuration,
mirrored data, unrelated games or players, existing Team roles, the general
tester-access role, or the private control socket merely because Beta Lab uses
the same runtime. Ambiguous ownership means retain and report, not infer.

## Safe execution order

1. Inventory source dependencies and live database, Discord, and persistent
   state read-only. Record exact IDs, ownership markers, and dependency edges
   without printing secrets or broad data dumps.
2. Decide independently which live fixture families should be removed. Take
   and validate a fresh logical `pg_dump` before an authorized database write.
3. Use the current authenticated beta and existing reviewed cleanup paths to
   remove exact live resources before deleting the code that understands their
   ownership. Stop the beta writer wherever an out-of-process database cleanup
   requires its advisory lock. Never run a second bot writer.
4. Verify ordinary beta data and Discord resources are intact, retain the
   backup and pre-cleanup image for rollback, and record any ambiguous residue.
5. Implement and test the bounded source cleanup. Synchronize the development
   guild command tree only with separate authorization so `/whattotest` is
   actually unpublished.
6. Build and deploy the exact clean checkpoint through ordinary Compose, then
   verify application identity, database transport, schema, checkpoint,
   restart count, no published ports, and the one-writer census.

Production and its data are outside every step of this cleanup. No production
service, checkout, command tree, configuration, schema, or database operation
is implied.
