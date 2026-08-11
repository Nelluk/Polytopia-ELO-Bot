# Development Beta Lab

Status: foundation implementation

The Beta Lab turns the existing development fixtures and PolyChamps-shaped
guild resources into one coherent testing surface. It is not a production
clone and never reads production configuration, Discord state, or data.

## Foundation packs

The first foundation recognizes three exact packs:

- `server-structure`: the reviewed WB1.3b two-House/three-Team setup and its
  pinned existing Team-role bindings;
- `leaderboard-showcase`: the separately owned 24-player/48-game synthetic
  player-leaderboard family; and
- `game-results`: the three exactly marked Ready, Unconfirmed, and Completed
  games used for win, confirmation, undo, ELO, role, and trade-price testing.

Status is ready only when every pack is canonical. A missing or ambiguous pack
is reported rather than inferred from unrelated guild or database objects.
The foundation does not yet create Discord roles or channels. The expansion
unit will add only manifest-owned resources after a reviewed collision,
partial-failure, and cleanup design; it must not adopt the guild's many
pre-existing same-purpose resources merely by name.

## Compact tester workspace

`/whattotest` remains a no-option development-only root, but opens one private
requester-bound Components v2 workspace. The initial view shows:

- overall and per-pack readiness;
- current result-scenario names and game IDs;
- participant display names, with IDs retained only as diagnostics; and
- a five-item quick release pass.

The tracked `docs/BETA_WHAT_TO_TEST.md` remains the full checklist authority.
The dashboard parses its `##` sections and presents at most five bounded items
at a time through a category selector and Previous/Next controls. It never
posts the complete checklist as a chain of public followups. Controls expire
after ten minutes and are usable only by the requester.

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
pack and explicitly reports which live mutations are implemented. In the
foundation, only `game-results` has a live refresh path:

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

## Safety and expansion boundary

Every path requires exact `development`, guild `478571892832206869`, database
`polytopia_dev`, role `polybot_dev`, disabled background tasks/API, and the
expected beta application/control socket. Reads and mutations run off the
Discord event loop. Cancellation drains already-started worker work.

Discord and PostgreSQL cannot share a transaction. The expansion unit must
therefore use idempotent staged reconciliation with durable exact IDs and
fail-closed repair, not claim cross-system atomic rollback. It should add
production-shaped synthetic scenario packs for games, Teams/Houses, league,
squads, permissions, safe maintenance previews, and presentation assets. Real
member-dependent workflows will still require explicitly selected development
members; the lab cannot manufacture Discord accounts.
