# Slash Command Taxonomy Review

Last updated: 2026-07-30

Status: Taxonomy v2.1 proposed for user/staff review; attribute-command and
Components v2 interaction rules accepted; approved `/leaderboard` paths
implemented locally

This review covers the bot's complete repository-backed command surface, not
only commands already converted to Discord application commands. Taxonomy v2.1
retains the approved domain-root architecture and the single user-facing
`/game` domain, but revises command placement around common user journeys:

- common game actions stay short and directly discoverable;
- useful attributes use focused commands that read by default and edit when a
  new value is supplied;
- less-common corrections and management actions use deeper, coherent paths;
- read commands say what they return rather than using developer terms such
  as “getter”;
- open, pending, started, completed, and corrected records are all games.

This is a naming and interaction proposal. It does not authorize a beta
launch, command synchronization, or a code rename. The current locally
implemented registration remains the source of truth until this revision is
approved and implemented.

Implementation is intentionally paused while staff continue reviewing command
names, command-registration scope, and whether some rare administration
workflows belong in Discord or a later web interface. The pause does not
invalidate checkpoint `63af179`; it prevents an unsettled public surface from
being synchronized.

## Inventory scope

Static inspection found:

- 83 in-scope explicit prefix command handlers;
- one customized framework `help` command;
- nine locally implemented `/game` subcommands and two `/elo` subcommands;
- many additional prefix aliases;
- a conditional command family for the Bullet cog.

The count describes handlers, not distinct user tasks. Alias-selected
behaviors and overlapping list commands should become typed choices rather
than duplicate slash commands.

The seven hidden commands in `modules/api_cog.py` are legacy and explicitly
excluded from the inventory, slash-capacity planning, and P4-P8 conversion
backlog. Retaining or deleting that cog is a separate cleanup decision.

### Disposition key

- **Native now**: implemented locally, although its path may change if v2 is
  approved before the next development sync.
- **Strong candidate**: clear typed slash model; convert in its roadmap phase.
- **Redesign**: useful native capability whose existing free-form grammar,
  aliases, attachments, pagination, or confirmation need deliberate UX.
- **Conditional**: convert only if the optional domain remains enabled and
  actively used.
- **Prefix/operator only**: intentionally omit from the public slash picker.
- **Retire/review**: hidden test or legacy behavior needing an explicit
  retain/retire decision.

## Taxonomy v2.1 design rules

### 1. Optimize the common journeys

The shortest paths should support what ordinary users do most often:

1. find or open a game;
2. join or leave it;
3. start it;
4. record a game that already exists in Polytopia;
5. see a game or its player names;
6. report a winner.

Those actions stay directly under `/game`. An uncommon staff correction may
take one additional word if that makes the picker easier to scan.

### 2. Group by user concept, not code implementation

- A useful property such as `/team emoji` or `/game map` reads its current
  value when the replacement value is omitted and edits it when supplied.
- `/game result ...` is for reviewing or correcting a reported result.
- `/game manage ...` is for uncommon lifecycle or membership administration.
- `/game show`, `/game search`, `/game players`, and `/game history` are
  descriptive reads.

There is no `/game get ...` group. “Get” is API vocabulary and makes the next
word do all the explanatory work. It would also create awkward paths such as
`/game get players` when `/game players` is both shorter and clearer.

There is also no general `set` subgroup for attributes that users reasonably
inspect on their own. For these commands:

- omit the replacement value to view the current setting;
- provide the replacement value to update it, subject to mutation permissions;
- use an explicit `clear:true` option where clearing is supported, because an
  omitted value already means “view”;
- infer an omitted target only when the requester has one unambiguous target;
  otherwise require a typed/autocompleted selection;
- expose the read path only when the current value is safe for that audience.

The same slash command may therefore have broad read permission while checking
staff/host permission only when an edit or clear option is supplied. Actions
such as win, join, confirm, delete, and unstart do not use this pattern because
omission cannot naturally mean “view.”

### 3. Names describe effects

- Use `/game record`, not `/game create`, for `newgame`: the command records
  a game that users already created in Polytopia.
- Reserve `/game open` for advertising an unstarted game with available
  places.
- Use `/game players` for the draft-ordered in-game names currently returned
  by `$getnames`, `$names`, and `$codes`.
- Use `/game notes` as a focused read-or-edit attribute. Notes also remain
  visible in the normal game display.
- Use `/game search status:unconfirmed`, not `/game unconfirmed`.

### 4. Preserve migration safety

Prefix names and aliases remain unchanged. Slash wrappers should stay thin
over shared application/worker services so a pre-production spelling change
does not alter permissions, transactions, or Discord-effect ordering. Once a
path reaches production, rename it only through an explicit compatibility
decision.

### 5. Use modal components for forms, not simple actions

Discord's current modal component model and the repository's locked
discord.py 2.7.1 support more than free-form text. A modal may include typed
user, role, mentionable, and channel selectors, string/radio choices,
checkboxes, file uploads, text inputs, labels, and explanatory text.

That makes modals a strong fit when a command needs several related values,
long text, an attachment, or a reviewable draft. It also removes the earlier
assumption that a custom game modal would require users to type ambiguous
member names.

Use modals/components selectively:

- keep one-step commands such as win, join, leave, confirm, extend, and
  delete as direct typed slash commands;
- prefer a modal for `/game notes`, `/support request`, team/house image
  replacement, and multi-field create/register forms;
- use a modal plus buttons/selects for an arbitrary-side `/game record`
  draft, with native member selection, review, and explicit confirmation;
- use message buttons/selects rather than a modal for pagination, repeated
  edits, previews, and destructive confirmation;
- treat a modal submission as a new interaction: collect input first, then
  defer the submission before worker/database work;
- keep all existing permission checks, primitive worker inputs, transaction
  boundaries, and post-commit Discord effects.

Modal state should remain ephemeral and short-lived unless a concrete workflow
needs restart persistence. A modal is a presentation layer over the same
application/worker service, not a second mutation implementation.

### 6. Keep invocation short; move exploration into Components v2

Slash options should identify the task, target, and safety-critical choices.
When several options merely select filters, views, pages, or iterative edits,
prefer a short command that opens a Components v2 workspace.

The accepted P7.5 player-leaderboard experiment is the reference:

- `/lb2` took no options and opened the standard local/current/active view;
- one select exposed common local/global, current/peak, and
  current/all-time presets;
- a button toggled active/all players;
- page, numeric jump, and requester-rank controls explored the cached result;
- only uncached view changes submitted a bounded worker read;
- the result stayed public while controls remained requester-only;
- desktop and mobile beta testing found it strictly preferable to the
  four-option `/leaderboard players` interface.

The production-intended path remains `/leaderboard players`; `lb2` is a
temporary experiment, not a taxonomy root or slash alias. P7.6 promotes the
accepted renderer to that path, adds an Advanced filters interaction for all
sixteen legacy combinations, and removes `lb2`.

Use this approach for leaderboards, searches, profile/detail workspaces,
multi-step drafts, and preview/confirmation flows. Do not bury a simple
one-step action behind a workspace, and do not move an operation's required
target or safety confirmation out of the invocation merely to reduce its
option count.

## Proposed game command tree

Discord permits a root, one optional subcommand-group level, and a command.
The following paths fit that model. `/game` would have nineteen immediate
children—seventeen direct commands and two groups—leaving six slots below
Discord's 25-child limit. Before adding later game capabilities, prefer typed
options or a coherent existing group over consuming the remaining headroom.

### Common and read flows

| Proposed native path | Current prefix handler(s) | Purpose / notes |
|---|---|---|
| `/game open` | `opengame` | Advertise an unstarted game and recruit players |
| `/game join` | `join` | Join an available game using typed game/side options |
| `/game leave` | `leave` | Leave an unstarted game |
| `/game start` | `start` | Move a filled/open game into play |
| `/game record` | `newgame` | Record an already-created Polytopia game; Native now as `/game create` |
| `/game show` | `game` | Display one game's full summary |
| `/game search` | `games`, `allgames`, `incomplete`, `wins`, no-arg `confirm` | Typed, paginated discovery across lifecycle and result states |
| `/game players` | `getnames` | Return draft-ordered in-game player names/codes |
| `/game win` | `win` | Report the winner; common enough to remain a short direct action |
| `/game history` | `logs` | View/search audit history with permission-aware scope |
| `/game notify` | `ping`, `pingall` | Typed target/scope, message, attachment, and confirmation options |

Recommended `/game search` options include:

- `status: open | active | completed | unconfirmed | all`;
- `player`, `team`, or free-text `query`;
- `outcome: win | loss | any`;
- `size`;
- pagination controls or interaction components.

`status:unconfirmed` is staff-gated and replaces the current standalone
`/game unconfirmed`. This keeps “unconfirmed” where users already look for
lists of games rather than presenting a read-only status as an action.

### Focused game attributes

| Proposed native path | Current prefix handler(s) | Purpose / notes |
|---|---|---|
| `/game name` | `rename` | View the tracked name; optional `name` edits it |
| `/game map` | `setmap` | View map type; optional `map` edits it |
| `/game tribe` | `settribe` | View one/all player tribes; optional typed player/tribe edits one |
| `/game notes` | `gamenotes` | View notes; optional text/modal edits them |
| `/game side` | `gameside` | View a side; optional name/assignment edits it |
| `/game ranked` | `rankset`, `rankunset` | View ranked state; optional Boolean changes it with staff permission; Native now as `/game set-ranked` |

Bulk tribe assignment remains on the prefix path initially. A later native
bulk editor should be interaction-driven rather than a long opaque argument.
Commands that support clearing use an explicit `clear` option rather than
overloading an omitted value.

### Result review and correction

| Proposed native path | Current prefix behavior | Purpose / notes |
|---|---|---|
| `/game result undo` | `unwin` | Remove a reported winner while preserving player-versus-staff behavior; Native now as `/game unwin` |
| `/game result confirm` | `confirm GAME_ID` | Staff-confirm one result and finalize ELO; Native now as `/game confirm` |
| `/game result auto-confirm` | `confirm auto` | Rare staff batch action with explicit preview/confirmation |

The staff suggestion to group winner operations is sound for the uncommon
review/correction paths. `/game win` remains direct because reporting a winner
is a primary user journey; treating it as a generic property setter would
describe the database implementation rather than the user's action.

### Lifecycle and membership management

| Proposed native path | Current prefix handler | Purpose / notes |
|---|---|---|
| `/game manage kick` | `kick` | Host/staff removal from an open game |
| `/game manage extend` | `extend` | Staff extension of an open-game deadline; Native now as `/game extend` |
| `/game manage unstart` | `unstart` | Staff return of a started game to open/pending; Native now as `/game unstart` |
| `/game manage delete` | `delete` | Permission-sensitive game deletion; Native now as `/game delete` |

`manage` is intentionally not named `staff`: some operations may also be
available to a host or participant, and permissions belong to each command
rather than its spelling.

## Complete system-wide capability map

The same conventions apply outside `/game`: common reads/actions stay direct,
independently useful attributes use read-or-edit commands, and dangerous
operator repair commands stay out of the public tree.

### Players, teams, squads, and ratings

| Current prefix handler(s) | Taxonomy v2.1 native home | Disposition / note |
|---|---|---|
| `player` | `/player show` | Strong candidate |
| `setname`, `steamname` alias behavior | `/player register` | Redesign typed platform, name, and optional staff target |
| `getname` | `/player game-name` | Strong candidate; returns the selected member's in-game name/code |
| `settime` | `/player timezone` | Strong candidate with UTC-offset choices |
| `team` | `/team show` | Strong candidate |
| `team_add` | `/team create` | Redesign staff options, including junior-team behavior |
| `team_emoji` | `/team emoji` | View by default; optional emoji edits with staff permission |
| `team_image` | `/team image` | View by default; optional attachment/URL edits |
| `team_name` | `/team name` | View exact name; optional name edits |
| `team_server` | `/team server` | View when safe; optional configured server edits with staff permission |
| `team_edit` aliases | `/team house`, `/team tier` | View by default; optional typed value edits |
| `squad` | `/squad show` | Redesign one-to-three member search |
| `squadname` | `/squad name` | View by default; optional name edits |
| `lb` | `/leaderboard players` | Components v2 workspace defaults to local/current/active and exposes common views, population, paging, and requester-rank controls in-message; preserve the full prefix matrix |
| `lbrecent` | `/leaderboard activity` | Native now with explicit server-30-days and global-all-time views |
| `lbteam` | `/leaderboard teams` | Strong candidate |
| `lbsquad` | `/leaderboard squads` | Native now with current/all-time eligibility choices |
| `roleelo` | `/leaderboard roles` | Redesign role filters, sorting, and export |
| `recalc_games_from` | `/elo recalculate` | Native now; owner-only and confirmed |
| active job status (slash-only) | `/elo status` | Native now; staff-only |

`/player register` is preferred over `/player set name` because the common
flow establishes or updates the user's bot registration, not merely a display
field. Staff targeting can be an optional typed member rather than a separate
public command.

The intended team option shape is attribute-focused:

- `/team emoji team:[optional] emoji:[optional] clear:[optional]`;
- `/team image team:[optional] image:[optional] clear:[optional]`;
- `/team server team:[optional] server:[optional] clear:[optional]`.

With no replacement or `clear` option, the command displays the current
setting. An omitted team is inferred only when the requester has one
unambiguous team; otherwise autocomplete/selection is required. Equivalent
safe patterns apply to team name/house/tier, squad name, house name/image,
player timezone, and focused game attributes.

#### Player leaderboard interaction matrix

`$lb` is not one fixed leaderboard. Its four independent filter dimensions
produce sixteen valid combinations. P7.1 initially exposed all four as slash
options; accepted P7.5 testing instead treats them as interactive refinements:

| Dimension | Values | Components v2 treatment |
|---|---|---|
| scope | `local`, `global` | Common-view preset selector |
| rating | `current`, `peak` | Common-view preset selector |
| era | `current`, `all-time` | Common-view preset selector |
| population | `active`, `all` | Focused active/all toggle |

Examples:

- `$lb` maps to local/current/current-era/active;
- `$lb global max` maps to global/peak/current-era/active;
- `$lb global alltime allplayers max` maps to
  global/peak/all-time/all.

The initial workspace exposes common presets rather than placing all sixteen
combinations in the invocation. An **Advanced filters** interaction exposes
all four dimensions for less-common combinations, while `$lb` remains
available during transition. The preserved prefix aliases are `$leaderboard`,
`$leaderboards`, `$lbglobal`, and `$lbg`. Slash uses one canonical
`/leaderboard players` command rather than a separate `/lb` alias.

The current model has a subtle fallback: when fewer than ten eligible rows
match the ranked/activity query, it returns all registered players for that
scope. P7.1 preserves that behavior by delegating selection to the unchanged
model method rather than silently redefining leaderboard membership. A later
rules decision may remove or label the fallback.

`/leaderboard activity` exposes the two existing activity views without
inventing unsupported combinations:

- **This server — past 30 days** preserves `$lbrecent`, `$recent`, and
  `$active`;
- **Global — all time** preserves `$lbactivealltime`.

`/leaderboard squads` preserves `$lbsquad` and `$squadlb`, with **Current
eligibility** retaining the 365-day cutoff and **All time** preserving the
legacy `alltime` argument. Both native commands use the same public component
pagination foundation as `/leaderboard players`. The shared **Page X/Y**
button opens a numeric jump modal, so large results do not require stepping
through every intermediate page.

`$lbteam` and `$roleelo` remain separate later units because their Discord-role
dependencies, graph/export behavior, filters, and permissions differ.

`$lbteamjr` is legacy. It remains prefix-only until a later prefix-retirement
decision and receives no slash conversion; its documented junior-team
distinction is not implemented by the current callback.

### League and house workflows

| Current prefix handler(s) | Taxonomy v2.1 native home | Disposition / note |
|---|---|---|
| `tutorial` | `/league guide` | Strong candidate |
| `newfreeagent` | `/league free-agents post` | Redesign channel/message options; moderator-only |
| `tokens` | `/league tokens` | Redesign view/update permission behavior |
| `imalive` | `/league mark-active` | Strong candidate |
| `season` | `/league season` | Strong candidate |
| `novas` | `/league join-novas` | Strong candidate |
| `promote` | `/league roster promote` | Split alias-driven image modes |
| `trade` alias | `/league roster trade` | Split from promote |
| `draft` | `/league roster draft` | Strong candidate with member/team options |
| `tradeprice` | `/league roster price` | Retain/review hidden read before exposure |
| `league_export` | `/league maintenance export` | Redesign staff-only deferred generation |
| `deactivate_players` | `/league maintenance deactivate` | Preview and confirmation required |
| `kick_inactive` | `/league maintenance kick-inactive` | Preview, confirmation, reconciliation |
| `house` | `/house show` | Strong candidate |
| `houses` | `/house list` | Strong candidate |
| `house_add` | `/house create` | Split create from alias-selected edits |
| `house_rename` alias | `/house name` | View by default; optional name edits |
| `house_image` alias | `/house image` | View by default; optional attachment/URL edits |
| `gtest` | none | Retire/review hidden hard-coded test command |

The deeper `roster` and `maintenance` paths reserve the short `/league`
surface for ordinary league participants. They also make destructive batch
operations harder to invoke accidentally without pretending that nesting is
a permission control.

### Bullet tournament

The Bullet cog is conditional and relies on an external spreadsheet. These
paths apply only if the feature remains active.

| Current prefix handler(s) | Taxonomy v2.1 native home | Disposition / note |
|---|---|---|
| `bullet` | `/bullet join` | Conditional |
| `nobullet` | `/bullet leave` | Conditional |
| `bulletstart` | `/bullet manage start` | Conditional, director-only |
| `bulletsub` | `/bullet manage substitute` | Conditional with two members |
| `bullettoggle` | `/bullet manage automation` | Conditional operator control; retain/review |

### General utilities and support

| Current prefix handler(s) | Taxonomy v2.1 native home | Disposition / note |
|---|---|---|
| customized `help` | `/help` or native discovery | Redesign after the registered tree stabilizes |
| `guide` | `/guide` | Strong candidate |
| `tribepoints` | `/tools tribe-points` | Strong candidate with map/mode choices |
| `rtribes` | `/tools random-tribes` | Redesign bans, free-tribe count, and duplicates |
| `credits` | `/about credits` | Strong candidate |
| `stats` | `/about stats` | Strong candidate after bounded read work |
| `staffhelp` | `/support request` | Redesign message, game ID, and attachments |

### Best modal/component candidates

| Capability | Recommended interaction | Why |
|---|---|---|
| Player leaderboards | `/leaderboard players` opens the accepted Components v2 workspace with presets, population toggle, paging, page modal, and requester-rank jump | Replaces four presentation-oriented slash options with a discoverable mobile/desktop UI |
| Game/player detail | Show the primary record immediately, then offer contextual history, players, attributes, and permitted actions | Keeps common lookup short while making secondary information discoverable |
| Search and history | Essential target/query at invocation; Components v2 filters and pages refine the immutable result | Avoids large option lists and repeated commands while keeping reads bounded |
| Arbitrary game recording | `/game record` opens a short-lived draft; modal collects name/options and native user selects fill sides; buttons add/edit sides and confirm | Restores large and multi-side coverage without message-content intent |
| Game notes | `/game notes` reads directly; an Edit button opens a paragraph modal | Long text is awkward as a slash option and benefits from prefilled review |
| Player registration | `/player register` modal with platform choice, name text, and optional staff-selected member | Several related fields form one understandable task |
| Team/house creation | Modal for name and typed attributes, followed by a review/confirm view | Multi-field creation is clearer than many optional slash arguments |
| Team/house image | Focused attribute command; Edit opens a modal file upload with explicit replace/clear choice | Native file upload avoids URL-only workflows |
| Support request | Modal with summary/details, game reference, and optional file upload | Supports structured reports and screenshots |
| Game notification | Modal for longer message and optional upload, then a public preview/confirm message | Separates authoring from potentially broad notification |
| League bulk maintenance | Buttons/selects for preview and confirmation; modal only for a reason/note | Bulk target selection and result review are iterative, not a one-shot form |

Search filters, leaderboards, and audit-history pagination should use minimal
essential slash inputs plus Components v2 refinement rather than large option
matrices or form modals. ELO recalculation and other long jobs should retain
immediate confirmation/defer behavior; a modal adds little to a two-option
command.

### Prefix/operator-only and repair commands

These operations should not inflate the public slash tree. Their internals
still require the same database, transaction, and event-loop review when
touched.

| Current prefix handler(s) | Native home | Disposition / note |
|---|---|---|
| `restart` | none | Prefix/operator only; service lifecycle separately approved |
| `purge_game_channels` | none | Destructive bulk Discord operation |
| `tribe_emoji` | none initially | Rare owner configuration |
| `ptrophies` | none | Retire/review hidden repair |
| `boost_from` | none | Owner bulk repair |
| `migrate_player` | none initially | Sensitive cross-record migration |
| `delete_player` | none | Destructive owner repair |
| `backup_db` | none | Operational backup |
| `test` | none | Retire hidden diagnostic |

## Proposed top-level roots

Taxonomy v2.1 uses these domain roots:

- `/game`
- `/player`
- `/team`
- `/squad`
- `/leaderboard`
- `/league`
- `/house`
- `/elo`
- `/bullet` when enabled
- `/tools`
- `/about`
- `/support`
- `/guide` and possibly `/help`

This remains the earlier T-A domain-root architecture. T-B's one `/poly`
umbrella and T-C's flat hyphenated commands remain rejected design history:
the umbrella makes common commands unnecessarily long, while flat names make
dozens of capabilities harder to scan and organize.

## Effect on the current implementation if approved

Checkpoint `63af179` currently registers:

- `/game create|win|unwin|delete|confirm|unconfirmed|set-ranked|extend|unstart`;
- `/elo recalculate|status`.

Taxonomy v2.1 would change only the slash registration/adapters:

| Current local path | Taxonomy v2.1 path |
|---|---|
| `/game create` | `/game record` |
| `/game win` | unchanged |
| `/game unwin` | `/game result undo` |
| `/game delete` | `/game manage delete` |
| `/game confirm` | `/game result confirm` |
| `/game unconfirmed` | `/game search status:unconfirmed` |
| `/game set-ranked` | `/game ranked` |
| `/game extend` | `/game manage extend` |
| `/game unstart` | `/game manage unstart` |
| `/elo recalculate` | unchanged |
| `/elo status` | unchanged |

The unified tree has not been synchronized to Discord, so approval now allows
a clean development rename without compatibility aliases. Prefix commands,
permissions, workers, transactions, and post-commit effects would remain
unchanged. `/game search` itself belongs to the P7 bounded-read work; until
that exists, the already-tested top-level `/unconfirmed` prefix behavior
remains available and the slash list can be temporarily omitted rather than
shipping a name already marked for replacement.

## Implementation and migration plan

1. Review and approve or revise taxonomy v2 before changing registration code.
2. If approved, create a narrow P4.1d follow-up that changes only slash groups,
   paths, audit attribution, registration tests, and the beta runbook.
3. Preserve prefix commands, aliases, permissions, workers, transactions, and
   Discord-effect ordering.
4. Do not implement placeholder read commands merely to fill the proposed
   tree. Add `/game search` in P7 with its bounded read/pagination design.
5. Synchronize only the development guild in a separately approved beta
   session and verify the exact registered tree.
6. Extend the taxonomy through bounded P4-P8 units, checking Discord's group
   and option limits before each registration.
7. Before P9, audit the actual tree, descriptions, permissions,
   autocomplete cost, compatibility ledger, and naming consistency.

Changing slash placement remains technically manageable because adapters stay
thin over shared application/worker logic. Once paths reach production they
should be treated as a public API and changed only through a deliberate
deprecation window.
