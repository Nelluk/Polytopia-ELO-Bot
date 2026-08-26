# Constrained PolyElo production releases

The installed, root-controlled `/srv/polyelo/bin/polyelo-release` wrapper
removes the need for an interactive sudo session during an already-authorized
ordinary release. It accepts no arguments and is the only command granted by
`/etc/sudoers.d/polyelo-release`.

Source preparation remains separate: review and commit the intended release,
leave `/srv/polyelo/PolyBot39` on a clean `master`, and obtain explicit owner
approval for the production service, schema, and Discord command actions. The
sudoers rule is an operating-system capability, not standing authorization for
Codex to deploy every source change.

After approval, run:

```bash
sudo -n /srv/polyelo/bin/polyelo-release
```

The wrapper locks against another release, requires a clean production
`master` and an active `polyelo.service`, stops that service, and drops to the
unprivileged `polyelo` account for `scripts/production_release.sh`. That
tracked runner applies every registered additive migration idempotently and
deploys commands only to Main and PolyChampions with the existing empty-global
guard. The wrapper then starts the canonical service and checks that it is
active.

If migration or Discord deployment fails, the wrapper leaves the service
stopped and reports the failed boundary. Inspect and reconcile the result
before retrying. It never pulls, merges, resets, restores a database, drops a
column, changes configuration, synchronizes globally, or accepts caller
arguments.

## One-time installation

From the canonical clean checkout, run:

```bash
cd /srv/polyelo/PolyBot39
sudo ./scripts/install_polyelo_release.sh
```

The installer validates the tracked sudoers source, installs the wrapper as a
root-owned mode-0755 file, installs the exact no-argument sudoers rule as a
root-owned mode-0440 file, and validates the complete sudoers configuration.
If complete validation fails, it restores the prior sudoers state.

Normal future schema additions belong in the unprivileged tracked
`scripts/production_release.sh`; they do not require broadening sudoers or
reinstalling the root wrapper. Root-wrapper behavior changes do require review
and rerunning the one-time installer.
