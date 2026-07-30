# Slash Command Taxonomy Review

Last updated: 2026-07-29

Status: Awaiting staff vote

This review covers the bot's complete repository-backed command surface, not
only commands already converted to Discord application commands. It is a
design record; it does not authorize registration changes or synchronization.

## Inventory scope

Static inspection found:

- 83 in-scope explicit prefix command handlers;
- one customized framework `help` command;
- 10 native registrations, including the uncommitted P4.1b `/extend`;
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

- **Native now**: already implemented or present in the current P4.1b work.
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

### Games and game communication

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| `newgame` | `/game create` | Native now; preserve C-001 limits until a custom draft UX is justified |
| `game` | `/game show` | Strong candidate |
| `allgames` | `/game search` | Redesign typed filters and pagination |
| `incomplete` | `/game list status:incomplete` | Redesign; aliases currently also request completed games |
| `wins` | `/game results outcome:wins|losses` | Redesign alias-driven outcome as a choice |
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
| `ping` | `/game ping` | Redesign explicit game ID, message, and optional attachment |
| `pingall` | `/game ping-all` | Redesign with confirmation; hidden mass-notification operation |

### Matchmaking lifecycle

| Current prefix handler(s) | Recommended native home | Disposition / note |
|---|---|---|
| `opengame` | `/match open` | Redesign flexible side shapes and expiry/rules |
| `games` | `/match list` | Strong candidate with pagination |
| `join` | `/match join` | Strong candidate |
| `leave` | `/match leave` | Strong candidate |
| `gameside` | `/match set-side` | Redesign role locking and side naming |
| `gamenotes` | `/match notes` | Strong candidate; modal is an option |
| `kick` | `/match kick` | Strong candidate with native member input |
| `start` | `/match start` | Strong candidate after lifecycle worker separation |
| `extend` | `/match extend` | Native now in P4.1b work; not yet beta-synchronized |
| `unstart` | `/match unstart` | Strong candidate, but destructive post-commit Discord effects require its own unit |

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

- `/game show`, `/game win`, `/game set-map`;
- `/match open`, `/match join`, `/match start`;
- `/player show`, `/player set-name`;
- `/leaderboard players`, `/leaderboard teams`;
- `/team show`, `/house list`, `/league season`;
- `/elo recalculate`, `/elo status`;
- `/tools random-tribes`, `/support request`.

This produces roughly a dozen understandable roots and keeps each family
within Discord's subcommand limits. Permissions remain command-specific:
placing a staff operation under `/game` does not make it public.

The main cost is converting current hybrids into unchanged prefix commands
plus thin slash wrappers. Bare `/game` cannot also mean “show a game” once it
is a group, so that capability becomes `/game show`.

### T-B — One application umbrella

Use one root, preferably `/poly`, with domain subcommand groups:

- `/poly game show`, `/poly game unwin`;
- `/poly match join`, `/poly match start`;
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
- `/match-open`, `/match-join`, `/match-start`;
- `/player-info`, `/player-set-name`;
- `/leaderboard-players`, `/leaderboard-teams`;
- `/league-season`, `/team-rename`, `/elo-recalculate`.

This minimizes changes to beta-tested registrations and permits the current
hybrid decorators to remain. It is a complete naming rule, but dozens of
top-level commands become a long alphabetical picker, related commands are
less visibly grouped, and the design approaches Discord's top-level command
limit sooner.

## Recommended voting procedure

Staff should vote first on architecture:

1. T-A — domain roots;
2. T-B — one umbrella;
3. T-C — systematic flat names.

Ranked-choice voting is preferable. For T-B voters, collect a secondary root
preference among `/poly`, `/elo`, and `/bot`.

After the architecture vote, run a short spelling review rather than mixing
dozens of word choices into the first ballot:

- `create` versus `new`;
- `match` versus `lobby`;
- `show` versus `info`;
- `unconfirmed` versus `pending-confirmation`;
- `player-names` versus `codes`.

## Implementation and migration plan

1. Freeze new slash naming after P4.1b while the vote is open.
2. Record the winning architecture and final spellings in the modernization
   decision log.
3. Build reusable slash groups/wrappers without changing prefix command names,
   aliases, permissions, workers, or transaction boundaries.
4. Move the current native surface in one bounded registration unit and add
   tests for old prefix registration plus new slash paths.
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
