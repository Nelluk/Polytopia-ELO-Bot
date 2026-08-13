# Development Beta Lab

Status: compact dashboard plus guided self-service scenarios

The Beta Lab turns the existing development fixtures and PolyChamps-shaped
guild resources into one coherent testing surface. It is not a production
clone and never reads production configuration, Discord state, or data.

## Available packs

The lab recognizes five exact packs:

- `server-structure`: the reviewed WB1.3b two-House/three-Team setup and its
  pinned existing Team-role bindings;
- `leaderboard-showcase`: the separately owned 24-player/48-game synthetic
  player-leaderboard family; and
- `game-results`: the three exactly marked Ready, Unconfirmed, and Completed
  games retained for operator readiness and reset checks; and
- `self-service-game-lanes`: up to three concurrent requester-owned bundles,
  each with fresh Ready, Unconfirmed, and Completed games for a human tester;
  and
- `guided-personas`: one exactly owned `Beta Lab House` / `Beta Lab Team`
  database fixture and two dedicated zero-permission Discord roles used only
  during an active guided session.

Status is ready only when every pack is canonical. A missing or ambiguous pack
is reported rather than inferred from unrelated guild or database objects.
The lab never modifies channels, existing Team roles, or arbitrary member
roles. The pinned `testers` role supplies lab access only. A separately owned
`Beta Lab Team` role makes Team/House inference realistic, while `Beta Lab
Staff` temporarily supplies the bot's development Helper classification. Both
roles carry zero Discord permissions and are assigned only after a database
session is active.

## Compact tester workspace

`/whattotest` remains a no-option development-only root, but opens one private
requester-bound Components v2 workspace. The initial view shows:

- overall and per-pack readiness;
- current result-scenario names and game IDs;
- participant display names, with IDs retained only as diagnostics; and
- a **Give me a 5-minute test** action, a tester-only guided-session action,
  and a direct **Report problem** action.

The tracked `docs/BETA_WHAT_TO_TEST.md` remains the full checklist authority.
The dashboard parses its `##` sections and presents at most five bounded items
at a time through a category selector and Previous/Next controls. It never
posts the complete checklist as a chain of public followups. Controls expire
after ten minutes and are usable only by the requester. A lane survives panel
expiry, but its temporary persona is revoked; rerunning `/whattotest` reopens
the lane and revalidates the persona.

### Human tester flow

1. Run `/whattotest` in the beta guild.
2. Choose **Give me a 5-minute test** for one short read-oriented assignment.
   Repeated clicks rotate among player, leaderboard, Team/House, game-search,
   and league workspaces.
3. Members with the pinned `testers` role may choose **Start guided session**.
   The bot creates three fresh 1v1 games owned by that requester and temporarily
   assigns the owned Team and staff-persona roles.
4. Choose one of **Team & House**, **Win claim**, **Confirm result**, or **Undo
   result**. Each page gives the exact slash fields and observable expected
   result; no tester is expected to complete all four.
5. Return to the private panel and choose **Refresh results** after a game
   command. Rerun `/whattotest` if the panel expires.
6. Choose **Finish and clean up** whenever done. It removes the temporary roles,
   reverses lane ELO, and deletes exactly the three marked games. Use **Report
   problem** to open `/staffhelp` with the lane and game IDs already filled in.

There are at most three active lanes and each lease lasts 30 minutes. Expired
lanes are reclaimed by the next claim. A tester may own only one active lane.
Removing the tester role prevents a new claim but does not prevent the owner
from cleaning up an already-owned lane. A bot start or Discord reconnect
revokes every owned persona role; an active session is reauthorized only after
the tester reopens `/whattotest` and the database lease is revalidated.

The tracked manifest at `data/development/beta_lab_manifest.json` pins the
development guild, tester role, two already registered fallback opponents,
capacity, and lease. The running worker revalidates the exact development
profile and live database identity. Every lane stores matching versioned
ownership markers in both `Game.name` and `Game.notes`; partial, duplicated,
oversized, participant-divergent, or otherwise damaged state fails closed.
Claims, expired cleanup, confirmed-game ELO work, protected GameLog audit, and
terminal immutable snapshot share one ELO-coordinated transaction. Discord
publishers receive frozen primitive snapshots, never ORM models.

## Operator CLI

For either supported Compose deployment, use the repository-owned wrapper from
the exact clean source checkpoint. It validates the configured image label and
running-container checkpoint, then executes the control CLI inside the running
bot so it reaches the protected mode-0600 socket in the persistent
`polybot_logs` volume. It never logs in a second Discord client:

```bash
./polybot beta-lab status
./polybot beta-lab plan
```

Pass `--mode external` immediately after `beta-lab` for the reviewed external-
database Compose definition. Direct host invocation is intentionally refused
when the profile selects the Compose supervisor because a host process sees a
different socket and writer-lock inode. Direct Python invocation remains a
legacy non-Compose/systemd path only and must explicitly set the reviewed
`POLYBOT_RESTART_SUPERVISOR=systemd` and
`POLYBOT_BETA_OPERATOR_CONTEXT=host-systemd` pair. Missing, mixed, or unknown
context values fail before profile or socket/database access. Compose context
also proves the `/app` image root, image interpreter, root-owned embedded image
checkpoint, and the shared log-volume mount; ordinary environment strings are
not sufficient. Every supported durable launcher and one-shot database mutation
also acquires the same PostgreSQL session advisory lock keyed to the fixed
development database. This database-scoped boundary remains shared across
bundled Compose, external Compose, and host-systemd even when their filesystem
lock volumes differ.

`status` returns primitive pack DTOs. `plan` adds the bounded action for each
pack and explicitly reports which live mutations are implemented. The
operator socket can refresh only `game-results`; `self-service-game-lanes` is
applied only by authenticated tester interactions through the running bot:

```bash
./polybot beta-lab refresh \
  --pack game-results \
  --confirm REFRESH-game-results
```

Refresh uses the existing owner identity, immutable preview fingerprint, exact
game IDs, worker-local connections, ELO coordinator, row locks, atomic fixture
and GameLog transaction, and terminal immutable status reload. It preserves
the current two participants and never touches ordinary games. A missing pack
still requires `/operator beta prepare` because participant choice is a human
input. An ambiguous pack is never refreshed.

The separate leaderboard and WB1.3b CLIs retain their stopped-writer seed and
cleanup boundaries in this foundation. The lab plan points to those boundaries
when a pack is absent; it does not bypass them.

### One-time guided-persona preparation

The Discord and PostgreSQL resources use two explicit stages because those
systems cannot share a transaction. With the reviewed beta running, inspect
and create only the two owned roles through its authenticated local socket:

```bash
./polybot beta-lab roles-status
./polybot beta-lab roles-setup \
  --confirm PREPARE-BETA-LAB-PERSONAS
```

Role setup refuses to adopt a matching name without ownership evidence. If a
prior setup created both roles but lost its private state publication, do not
delete or recreate them. After reviewing that there is exactly one role for
each manifest name and both are unmanaged, assignable, zero-permission,
unhoisted, unmentionable, and have no members, reconcile only their ownership
record:

```bash
./polybot beta-lab roles-reconcile \
  --confirm RECONCILE-BETA-LAB-PERSONAS
```

Reconciliation performs no Discord mutation and refuses duplicates, changed
permissions/flags, unassignable roles, or any member assignment.

Then stop the durable beta, verify the writer gate is clear, and seed only the
owned House/Team rows:

```bash
./polybot beta-lab database-status
./polybot beta-lab database-seed \
  --confirm PREPARE-BETA-LAB-PERSONAS
```

Database operations retain the local filesystem guard but rely on the fixed
PostgreSQL advisory lock as the universal writer identity. Bundled Compose,
external Compose, host-systemd, and supported one-shot mutations therefore
contend even when their filesystem volumes differ. Mutation still requires the
durable beta to be stopped; a running bot makes the database lock fail closed
before inspection or mutation. Seed writes schema-v2 private pending ownership
evidence inside the database transaction. That evidence includes a canonical
snapshot and SHA-256 digest of every House, Team, and usage field in the
pristine predicate. After commit, and again during crash recovery or adoption,
the operation rereads the full predicate under the same writer lock.

Before replacing the pending filesystem record, the same lock-owning PostgreSQL
session transactionally publishes its canonical evidence document and digest
to `development_writer_fence`. The filesystem record is a projection, not the
sole authority: readiness requires the file, complete live baseline, and
database-backed authority to agree. If the session dies before database
publication, pending evidence remains. If it dies after database publication
but around the filesystem rename, any later conflicting mutation makes the
baseline fail closed rather than accepting stale published evidence.

If a commit or publication outcome is unknown, do not retry seed. With the beta
still stopped, use:

```bash
./polybot beta-lab database-reconcile \
  --confirm RECONCILE-BETA-LAB-PERSONAS
```

Reconciliation promotes evidence only for an exact committed match or removes
created pending evidence only when both candidate rows are absent. Published+
pending conflicts, malformed/forged evidence, or any changed baseline or usage
requires manual review. The same operation safely upgrades legacy schema-v1
evidence or recovers missing ownership publication for one pre-existing pair
only when the House and Team remain exact, pristine, and unused: no players,
game sides, bids, preferences, or additional House teams. It writes private
evidence only and makes no database-row mutation.

## Safety and remaining expansion boundary

Every path requires exact `development`, guild `478571892832206869`, database
`polytopia_dev`, role `polybot_dev`, disabled background tasks/API, and the
expected beta application/control socket. Reads and mutations run off the
Discord event loop. Cancellation drains already-started worker work.

Discord role assignment and database lane creation cannot be atomic. The lane
commits first; persona failure triggers exact lane compensation, and ambiguous
compensation tells the tester not to retry. Cleanup removes bot authority
before deleting the lane. Startup revocation is deliberately stronger than
session restoration, so a crash cannot preserve temporary staff authority.
Future expansion can add more short assignments and database-owned scenarios,
but the lab cannot manufacture Discord accounts.
