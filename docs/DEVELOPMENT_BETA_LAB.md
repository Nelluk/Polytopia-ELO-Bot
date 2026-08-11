# Development Beta Lab

Status: compact dashboard plus self-service game lanes

The Beta Lab turns the existing development fixtures and PolyChamps-shaped
guild resources into one coherent testing surface. It is not a production
clone and never reads production configuration, Discord state, or data.

## Available packs

The lab recognizes four exact packs:

- `server-structure`: the reviewed WB1.3b two-House/three-Team setup and its
  pinned existing Team-role bindings;
- `leaderboard-showcase`: the separately owned 24-player/48-game synthetic
  player-leaderboard family; and
- `game-results`: the three exactly marked Ready, Unconfirmed, and Completed
  games retained for operator readiness and reset checks; and
- `self-service-game-lanes`: up to three concurrent requester-owned bundles,
  each with fresh Ready, Unconfirmed, and Completed games for a human tester.

Status is ready only when every pack is canonical. A missing or ambiguous pack
is reported rather than inferred from unrelated guild or database objects.
The lab does not create Discord roles or channels. The development guild
already has the pinned `testers` role, three exact Team roles, and the reviewed
House/Team database setup. Broad automatic role reassignment would be unsafe
for the current human pool because `testers` also supplies Helper access.

## Compact tester workspace

`/whattotest` remains a no-option development-only root, but opens one private
requester-bound Components v2 workspace. The initial view shows:

- overall and per-pack readiness;
- current result-scenario names and game IDs;
- participant display names, with IDs retained only as diagnostics; and
- a **Give me a 5-minute test** action, a tester-only game-lane action, and a
  direct **Report problem** action.

The tracked `docs/BETA_WHAT_TO_TEST.md` remains the full checklist authority.
The dashboard parses its `##` sections and presents at most five bounded items
at a time through a category selector and Previous/Next controls. It never
posts the complete checklist as a chain of public followups. Controls expire
after ten minutes and are usable only by the requester. A lane survives panel
expiry; rerunning `/whattotest` reopens it.

### Human tester flow

1. Run `/whattotest` in the beta guild.
2. Choose **Give me a 5-minute test** for one short read-oriented assignment.
   Repeated clicks rotate among player, leaderboard, Team/House, game-search,
   and league workspaces.
3. Members with the pinned `testers` role may choose **Create my game lane**.
   The bot creates three fresh 1v1 games owned by that requester and shows
   participant names, exact game IDs, and the command to exercise each state.
4. Choose **Finished** after completing the flow, or **Release lane** when
   stopping early. Both reverse any lane ELO and delete exactly those three
   marked games. Use **Report problem** to open `/staffhelp` with the lane and
   game IDs already filled in.

There are at most three active lanes and each lease lasts 30 minutes. Expired
lanes are reclaimed by the next claim. A tester may own only one active lane.
Removing the tester role prevents a new claim but does not prevent the owner
from releasing an already-owned lane.

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

The CLI connects only to the protected mode-0600 socket of the already running
development beta. It never logs in a second Discord client:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_beta_lab.py --json status

POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_beta_lab.py --json plan
```

`status` returns primitive pack DTOs. `plan` adds the bounded action for each
pack and explicitly reports which live mutations are implemented. The
operator socket can refresh only `game-results`; `self-service-game-lanes` is
applied only by authenticated tester interactions through the running bot:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_beta_lab.py --json refresh \
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

## Safety and remaining expansion boundary

Every path requires exact `development`, guild `478571892832206869`, database
`polytopia_dev`, role `polybot_dev`, disabled background tasks/API, and the
expected beta application/control socket. Reads and mutations run off the
Discord event loop. Cancellation drains already-started worker work.

No Discord resource mutation is part of a lane, so the self-service path does
not claim cross-system atomicity. Future expansion can add more short
assignments and database-owned scenarios. Paired-user queues, temporary
permission personas, or automatic Discord role/channel setup remain separate
units because they require explicit human coordination or durable staged
Discord reconciliation. The lab cannot manufacture Discord accounts.
