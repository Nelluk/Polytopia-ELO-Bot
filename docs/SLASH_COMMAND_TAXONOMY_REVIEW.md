# Slash Command Taxonomy Review

Last updated: 2026-07-30

Status: Revised T-A approved for development

This review covers the bot's complete repository-backed command surface, not
only commands already converted to Discord application commands. The revised
T-A proposal uses one `/game` domain for open, pending, started, completed,
and corrected games. It does not use a separate user-facing `/match` domain.
The user approved this architecture on 2026-07-30. Discord synchronization
remains separately gated.

## Inventory scope

Static inspection found:

- 83 in-scope explicit prefix command handlers;
- one customized framework `help` command;
- nine previously synchronized native commands plus two P4.1d subcommands,
  all now migrated locally into the approved groups;
- many additional prefix aliases;
- a conditional command family for the Bullet cog.

The count describes handlers, not distinct user tasks. Several pairs or aliases
are one capability in a typed interface: `rankset` and `rankunset` become one
Boolean operation, while `wins` and `losses` can become one query with a
result choice.

The seven hidden commands in `modules/api_cog.py` are legacy and explicitly
excluded from this inventory, staff vote, slash-capacity planning, and P4-P8
conversion backlog. Retaining or deleting that cog is a separate cleanup
decision.

Every current handler is classified below. A native conversion is not
automatic merely because a command exists.

### Disposition key

- **Native now**: already beta-tested or implemented locally.
- **Strong candidate**: clear typed slash model; convert in its roadmap phase.
- **Redesign**: worthwhile native capability, but free-form grammar, aliases,
  attachments, pagination, or confirmation needs a deliberate interaction UX.
- **Conditional**: convert only if the optional domain remains enabled and
  actively used.
- **Prefix/operator only**: intentionally keep out of the public slash picker.
- **Retire/review**: hidden test or legacy behavior that needs a retain/retire
  decision before modernization.

## Complete capability map under T-A

T-A, the recommended taxonomy, uses domain roots. Existing prefix names and
aliases remain unchanged during migration.

### Complete game lifecycle and communication

Users treat open, joinable, started, and completed records as games. The
native taxonomy follows that mental model even though legacy code, database
state, and prefix aliases sometimes use “match” or “matchmaking.”

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| `newgame` | `/game create` | Native now; preserve C-001 limits until a custom draft UX is justified |
| `game` | `/game show` | Strong candidate |
| `allgames` | `/game search` | Redesign typed filters and pagination |
| `incomplete` | `/game search status:incomplete|completed` | Fold alias-driven status into typed search filters |
| `wins` | `/game search outcome:win|loss` | Fold alias-driven outcome into typed search filters |
| `win` | `/game win` | Native now |
| `unwin` | `/game unwin` | Native now |
| `delete` | `/game delete` | Native now |
| `confirm` | `/game confirm` | Native now |
| `unconfirmed` | `/game unconfirmed` | Native now; spelling may become `pending-confirmation` |
| `rankset`, `rankunset` | `/game set-ranked` | Native now as one typed Boolean command |
| `rename` | `/game rename` | Strong candidate |
| `setmap` | `/game set-map` | Strong candidate with choices/autocomplete |
| `settribe` | `/game set-tribe` | Redesign bulk grammar; begin with one member and one tribe |
| `getnames` | `/game player-names` | Strong candidate |
| `logs` | `/game logs` | Redesign search scope and pagination |
| `ping`, `pingall` | `/game ping scope:game|all` | Redesign explicit target/message/attachment; require confirmation for hidden mass mode |
| `opengame` | `/game open` | Redesign flexible side shapes and expiry/rules |
| `games` | `/game search status:open` | Fold the joinable-game list into typed game search |
| `join` | `/game join` | Strong candidate |
| `leave` | `/game leave` | Strong candidate |
| `gameside` | `/game set-side` | Redesign role locking and side naming |
| `gamenotes` | `/game notes` | Strong candidate; modal is an option |
| `kick` | `/game kick` | Strong candidate with native member input |
| `start` | `/game start` | Strong candidate after lifecycle worker separation |
| `extend` | `/game extend` | Native now; staff-only |
| `unstart` | `/game unstart` | Native now; staff-only |

The combined inventory has 28 legacy capability rows. Four overlapping list
and history handlers (`allgames`, `incomplete`, `wins`, and `games`) become
one `/game search` command, while `ping` and `pingall` become one typed
command. That leaves at most 24 named `/game` subcommands if every other
candidate is eventually exposed, within Discord's 25-child group limit.
Low-value hidden or bulk operations should remain prefix/operator-only or be
folded into typed options before the group reaches that limit.

### Effect on the current implementation

Checkpoint `63af179` implements the approved surface locally. The bot now
registers only `/game` and `/elo` at the top level: all previously synchronized
native game commands moved beneath `/game`, ELO maintenance moved beneath
`/elo`, and the never-synchronized `/match` group was removed. Prefix commands
and aliases remain unchanged. No compatibility aliases were added because no
native name has reached production. Development-guild synchronization and
live smoke testing remain separately gated.

### Players, teams, squads, and ratings

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| `player` | `/player show` | Strong candidate |
| `setname` | `/player set-name` | Redesign platform and staff-target behavior as typed options |
| `getname` | `/player game-name` | Strong candidate |
| `settime` | `/player set-time` | Strong candidate with UTC-offset choices |
| `team` | `/team show` | Strong candidate |
| `squad` | `/squad show` | Redesign one-to-three member search |
| `squadname` | `/squad set-name` | Strong candidate |
| `lb` | `/leaderboard players` | Redesign filters and pagination |
| `lbrecent` | `/leaderboard activity` | Fold hidden command into a typed leaderboard view |
| `lbteam` | `/leaderboard teams` | Strong candidate |
| `lbsquad` | `/leaderboard squads` | Strong candidate |
| `roleelo` | `/leaderboard roles` | Redesign roles, any/all matching, sorting, and file export |
| `recalc_games_from` | `/elo recalculate` | Native now; owner-only and confirmed |
| active job status (slash-only) | `/elo status` | Native now; staff-only |

### Team and league administration

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| `team_add` | `/team create` | Redesign staff options, including junior-team behavior |
| `team_emoji` | `/team set-emoji` | Strong candidate |
| `team_image` | `/team set-image` | Redesign URL versus attachment |
| `team_name` | `/team rename` | Strong candidate |
| `team_server` | `/team set-server` | Strong candidate, staff-only |
| `team_edit` | `/team set-house`, `/team set-tier` | Split aliases with different meanings |
| `tutorial` | `/league guide` | Strong candidate |
| `newfreeagent` | `/league post-free-agents` | Redesign channel and message options; moderator-only |
| `tokens` | `/league tokens` | Redesign view/update permission behavior |
| `imalive` | `/league mark-active` | Strong candidate |
| `season` | `/league season` | Strong candidate |
| `novas` | `/league join-novas` | Strong candidate |
| `promote` | `/league promote`, `/league trade` | Split alias-driven image modes |
| `draft` | `/league draft` | Strong candidate with native member/team options |
| `tradeprice` | `/league trade-price` | Retain/review hidden read command before exposing |
| `league_export` | `/league export` | Redesign staff-only deferred file generation |
| `deactivate_players` | `/league deactivate-players` | Redesign confirmation and preview |
| `kick_inactive` | `/league kick-inactive` | Redesign confirmation, preview, and reconciliation |
| `house` | `/house show` | Strong candidate |
| `houses` | `/house list` | Strong candidate |
| `house_add` | `/house create`, `/house rename`, `/house set-image` | Split three alias-selected operations |
| `gtest` | none | Retire/review hidden hard-coded test command |

### Bullet tournament

The Bullet cog is conditionally loaded and uses an external spreadsheet.
These names belong in the taxonomy only if the feature remains enabled and is
included in a later modernization unit.

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| `bullet` | `/bullet join` | Conditional |
| `nobullet` | `/bullet leave` | Conditional; separate role removal option |
| `bulletstart` | `/bullet start` | Conditional, director-only |
| `bulletsub` | `/bullet substitute` | Conditional with two native members |
| `bullettoggle` | `/bullet automation` | Conditional operator control; retain/review before exposure |

### General utilities and support

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| customized `help` | `/help` or native command discovery | Redesign after taxonomy selection |
| `guide` | `/guide` | Strong candidate |
| `tribepoints` | `/tools tribe-points` | Strong candidate with map/mode choices |
| `rtribes` | `/tools random-tribes` | Redesign bans, seed, free-tribe count, and duplicates |
| `credits` | `/about credits` | Strong candidate |
| `stats` | `/about stats` | Strong candidate after bounded read work |
| `staffhelp` | `/support request` | Redesign message, game ID, and attachment options |

### Operator and repair commands

These commands should not inflate the public taxonomy. “Prefix/operator only”
does not mean their internals are exempt from database and event-loop review.

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| `restart` | none | Prefix/operator only; service lifecycle remains separately approved |
| `purge_game_channels` | none | Prefix/operator only; destructive bulk Discord operation |
| `tribe_emoji` | none initially | Prefix/operator only; rare owner configuration |
| `ptrophies` | none | Retire/review hidden repair |
| `boost_from` | none | Prefix/operator only; owner bulk repair |
| `migrate_player` | none initially | Prefix/operator only; sensitive cross-record migration |
| `delete_player` | none | Prefix/operator only; destructive owner repair |
| `backup_db` | none | Prefix/operator only; operational backup |
| `test` | none | Retire hidden diagnostic |

## Three complete taxonomy choices

The capability inventory above remains the same under all three options. The
vote decides how native commands are addressed.

### T-A — Domain roots (recommended)

Examples:

- `/game open`, `/game join`, `/game start`;
- `/game show`, `/game win`, `/game set-map`;
- `/player show`, `/player set-name`;
- `/leaderboard players`, `/leaderboard teams`;
- `/team show`, `/house list`, `/league season`;
- `/elo recalculate`, `/elo status`;
- `/tools random-tribes`, `/support request`.

This produces roughly eleven understandable roots and keeps each family
within Discord's subcommand limits. `/game` spans the full game lifecycle;
permissions remain command-specific, so placing a staff operation under
`/game` does not make it public.

The main cost is converting current hybrids into unchanged prefix commands
plus thin slash wrappers. Bare `/game` cannot also mean “show a game” once it
is a group, so that capability becomes `/game show`. The unified group also
requires the query and notification consolidation described above to preserve
headroom below Discord's 25-child limit.

### T-B — One application umbrella

Use one root, preferably `/poly`, with domain subcommand groups:

- `/poly game show`, `/poly game unwin`;
- `/poly game open`, `/poly game join`, `/poly game start`;
- `/poly player show`, `/poly leaderboard teams`;
- `/poly league season`, `/poly team rename`;
- `/poly elo recalculate`, `/poly tools random-tribes`.

`/elo` or `/bot` could replace `/poly`, but `/elo` is semantically misleading
for unranked games, matchmaking, support, and league administration.

This gives the bot one obvious entry point and keeps Discord's top-level
picker compact. Its costs are longer three-word invocations, one allowable
subcommand-group level, and a very broad root whose internal families must
remain below Discord limits.

### T-C — Flat names with systematic prefixes

Keep existing beta names where possible and name future commands with
hyphenated domains:

- `/game-info`, `/game-search`, `/game-rename`, `/game-set-map`;
- `/game-open`, `/game-join`, `/game-start`;
- `/player-info`, `/player-set-name`;
- `/leaderboard-players`, `/leaderboard-teams`;
- `/league-season`, `/team-rename`, `/elo-recalculate`.

This minimizes changes to beta-tested registrations and permits the current
hybrid decorators to remain. It is a complete naming rule, but dozens of
top-level commands become a long alphabetical picker, related commands are
less visibly grouped, and the design approaches Discord's top-level command
limit sooner.

## Taxonomy decision record

The alternatives considered were:

1. T-A — domain roots;
2. T-B — one umbrella;
3. T-C — systematic flat names.

The user selected T-A with a unified `/game` domain on 2026-07-30. The other
options remain documented as design history, not open implementation choices.
Any later change requires an explicit compatibility decision.

Future spelling reviews should remain separate from architecture changes:

- `create` versus `new`;
- `open` versus `host`;
- `show` versus `info`;
- `unconfirmed` versus `pending-confirmation`;
- `player-names` versus `codes`.

## Implementation and migration plan

1. Use the approved T-A domain roots for new development.
2. Treat later taxonomy changes as explicit compatibility decisions,
   especially after the first production synchronization.
3. Build reusable slash groups/wrappers without changing prefix command names,
   aliases, permissions, workers, or transaction boundaries.
4. Preserve checkpoint `63af179`'s `/game` and `/elo` registration structure
   and extend it through bounded units with prefix-registration and slash-path
   tests.
5. Synchronize only the development guild in a separately approved beta
   session and run the existing mutation/permission smoke matrix.
6. Because no names are in production, prefer a clean beta rename. Retain old
   top-level slash wrappers for at most one beta cycle only if staff need them.
7. Continue P4-P8 in small transactional units, using the capability map to
   assign names and dispositions. Do not convert operator commands by default.
8. Before P9, audit the actual registered tree, descriptions, permissions,
   autocomplete cost, compatibility ledger, and Discord limits.

The legacy API cog is outside this plan and should not receive slash wrappers.

Changing slash placement later remains technically manageable because command
adapters should stay thin over shared application/worker logic. Once names
reach production they should be treated as a public API and changed through an
explicit deprecation window.
