# Slash Command Taxonomy Review

Last updated: 2026-08-04

Status: Taxonomy v2.2 provisionally accepted as the working implementation
taxonomy; minor pre-deployment wording refinements remain allowed

This review covers the bot's complete repository-backed command surface, not
only commands already converted to Discord application commands. Taxonomy v2.2
retains the approved domain-root architecture and the single user-facing
`/game` domain, but revises command placement and invocation design around
common user journeys:

- common game actions stay short and directly discoverable;
- useful attributes use focused commands that read by default and edit when a
  new value is supplied;
- less-common corrections and management actions use deeper, coherent paths;
- read commands say what they return rather than using developer terms such
  as “getter”;
- open, pending, started, completed, and corrected records are all games.
- slash invocation supplies only the task and essential target; Components v2
  is an opt-in enhancement for exploratory filters, long-form authoring,
  attachments, previews, paging, and iterative refinement when it provides a
  concrete current usability benefit.

Taxonomy v2.2 is now the working implementation contract. This acceptance does
not authorize a beta launch, command synchronization, production deployment,
or wholesale registration. Minor staff-requested wording refinements may be
made before P9 as thin adapter changes, but they no longer block bounded
workflow units using the agreed domain roots and interaction rules.

## Inventory scope

Static inspection found:

- 78 active-target explicit prefix command handlers;
- one customized framework `help` command;
- seventeen locally implemented `/game` commands (sixteen immediate children,
  including the `manage` group, plus its nested `kick`) and two `/elo`
  subcommands;
- three locally implemented `/leaderboard` subcommands plus temporary `/lb2`;
- many additional prefix aliases;
- five Bullet prefix handlers now classified as legacy/out of scope;
- the command-free anti-scam listener, also classified as legacy/out of
  scope.

The count describes handlers, not distinct user tasks. Alias-selected
behaviors and overlapping list commands should become typed choices rather
than duplicate slash commands.

The seven hidden commands in `modules/api_cog.py`, five commands in
`modules/bullet.py`, and the listener-only `modules/antiscam.py` are legacy
and explicitly excluded from slash-capacity planning and the P4-P8 conversion
backlog. They may remain loaded and receive narrowly necessary operational,
security, privacy, or dependency-compatibility fixes. Retaining, disabling,
or deleting them is a separate decision and is not implied by this taxonomy.

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

## Taxonomy v2.2 design rules

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
- `/game show`, `/game search`, `/game players`, and `/game logs` are
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
- Keep `/game show` and `/player show` as the explicit detail commands.
  Discord command groups do not provide a default no-subcommand action, so
  retaining the familiar `show` verb is clearer than inventing another
  lookup name. `/player show` defaults to the requester; `/game show` infers
  a game only from unambiguous context and otherwise requests a game ID.
- Use `/game ping`, not `/game notify`, for the notification workflow. “Ping”
  matches established user vocabulary and the current prefix commands.
- Use `/game logs`, not `/game history`, for the permission-aware game audit
  trail. “Logs” matches the current prefix command and distinguishes audit
  records from broader player or game history views.
- Use `/game notes` as a focused read-or-edit attribute. Notes also remain
  visible in the normal game display.
- Use `/game search status:unconfirmed`, not `/game unconfirmed`.
- Treat a player's Polytopia name as one canonical value in the native
  interface. Do not ask slash users to choose among mobile name, Steam name,
  or legacy friend code.
- Keep the established, discoverable top-level `/staffhelp` name instead of
  replacing it with `/support request`.

### 4. Preserve migration safety

Prefix preservation is the default recommendation only for high-frequency or
day-to-day workflows, power-user bulk entry, or capabilities not yet matched
natively. Each unit records an explicit `legacy recommendation: retain` or
`legacy recommendation: retire` with its rationale and user decision. Existing
user-approved preserved commands remain unchanged; explicitly approved
retirements are not restored. Slash wrappers should stay thin over shared
application/worker services so a pre-production spelling change does not alter
permissions, transactions, or Discord-effect ordering. Once a path reaches
production, rename or retire it only through an explicit compatibility
decision.

Compatibility is evaluated in layers. Database semantics, permissions,
transactions, audit attribution, and post-commit effects require parity.
Retained prefix invocation remains available during transition. Presentation may
move from embeds/reactions to Components v2 only when a concrete current usability
benefit justifies it and desktop/mobile evidence covers the replacement; simple
reads should retain the proven common view.
Do not maintain parallel classic and modern mutation implementations.

The production canary must use one bot process: retain prefix commands while
enabling the new native/component surface only in the approved PolyChampions
guild through explicit capability policy. Do not connect a beta bot as a
second writer to the production database; current coordinators and
reconciliation are process-local.

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
- prefer a modal for `/game notes`, `/staffhelp`, team/house image
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
and interaction provides a concrete current benefit, prefer a short command
that opens a Components v2 workspace. Do not add interaction for hypothetical
future needs when the classic result is already clear.

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

The production-intended path is `/leaderboard players`; P7.6 promoted the
accepted renderer to that path, added an Advanced filters interaction for all
sixteen legacy combinations, and removed temporary `lb2`.

Use this approach for leaderboards, searches, profile/detail workspaces,
multi-step drafts, and preview/confirmation flows. Do not bury a simple
one-step action behind a workspace, and do not move an operation's required
target or safety confirmation out of the invocation merely to reduce its
option count.

Taxonomy v2.2 applies this rule system-wide:

| Capability | Essential invocation input | Interactive refinement |
|---|---|---|
| `/leaderboard players` | none | Implemented Components v2 presets, all 16 advanced-filter combinations, cached paging, and requester rank |
| `/game search` | optional initial query or player | Implemented Components v2 status/outcome/common-size filters and paging; arbitrary side shapes remain accepted in the query grammar |
| `/game show` | optional game ID when it cannot be inferred from the channel | Implemented P7.9 classic production-style card over a bounded immutable read; numeric prefixes share it, while Components remain opt-in for a separately justified future need |
| `/game ping` | optional game ID when it cannot be inferred | audience/scope, long message, multiple uploads, preview, confirmation |
| `/game record` | game name, one roster string, and optional ranked state | parsed arbitrary sides, native side/member editing, preview, confirmation |
| `/player show` | optional member; requester by default | Accepted Components v2 overview, ratings, recent/incomplete/completed/season games, results, teams, and permitted profile edits; legacy analytics remain deferred under C-002 |
| `/player register` | optional staff-selected member | one canonical Polytopia name and review |
| `/player timezone` | optional member, normalized UTC offset, or clear; requester/member defaults as defined by permission | no options reads the effective fixed offset; bounded UTC-offset autocomplete and explicit clear/write semantics |
| `/team show` | optional team when requester context is unambiguous | roster, history, attributes, and permitted edits |
| `/staffhelp` | none | game reference, long description, multiple uploads, review, submit |

This table is an interaction contract rather than permission to combine
unrelated application services. Each bounded implementation unit still owns
its worker, authorization, transaction, and post-effect review.

WB1.1 implements the `/staffhelp` row locally on the locked discord.py 2.7.1
environment: the native command has no options and opens a modal with a
radio-group category, bounded summary/details/context inputs, and a 10-file
Components v2 upload field. The legacy `$staffhelp`/`$helpstaff` adapters are
intentionally retired by explicit user approval before integration; native
`/staffhelp` is the development wider-beta replacement and the sole WB1.1
feedback intake. The native submission uses the development-only append-only
JSONL authority, and the configured staff-channel message is a post-write
mirror. It is not a production-ready replacement; before P9, the project must
separately approve a production-safe authoritative intake/retention path or
another production relay design. Production communities continue using their
currently deployed support/moderator route until then. This implementation is
review-pending and does not authorize command synchronization or beta launch.

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
| `/game record` | `newgame` | Record an already-created Polytopia game; one roster string reuses the `vs` grammar, previews inferred sides, and provides native side/member editing before confirmation |
| `/game show` | `game` | Display one game's full summary |
| `/game search` | `games`, `allgames`, `incomplete`, `wins`, no-arg `confirm` | Typed, paginated discovery across lifecycle and result states |
| `/game players` | `getnames` | Return draft-ordered canonical Polytopia names |
| `/game win` | `win` | Report the winner; common enough to remain a short direct action |
| `/game logs` | `logs` | View/search audit history with permission-aware scope |
| `/game ping` | `ping`, `pingall` | Optional inferred/typed game target opens a notification composer; audience, message sections, uploads, preview, and confirmation are interactive |

Recommended `/game search` options include:

- `status: open | active | completed | unconfirmed | all`;
- `player`, `team`, or free-text `query`;
- `outcome: win | loss | any`;
- `size`;
- pagination controls or interaction components.

`status:unconfirmed` is staff-gated and replaces the current standalone
`/game unconfirmed`. This keeps “unconfirmed” where users already look for
lists of games rather than presenting a read-only status as an action.

#### Game ping composer

`/game ping` consolidates `ping` and `pingall` without reproducing their
argument grammar as slash options. The platform-specific `pingmobile` and
`pingsteam` aliases are obsolete under full cross-play and receive no native
equivalent:

1. Infer the current game when invoked in an unambiguous game channel;
   otherwise accept or request a game selection.
2. Open a requester-only draft with audience choices for one game or, when
   permitted, the requester's incomplete games.
3. Collect long-form text in one or more modal sections and accept multiple
   uploads.
4. Show a public-effect preview with the resolved game count, recipient
   summary, text, and attachments.
5. Require explicit confirmation before notifications are delivered and
   audit-log only the confirmed delivery.

Components improve the current limits but do not make Discord messages
unlimited. A single modal text input accepts at most 4,000 characters and a
file-upload component accepts at most 10 files. The implementation should
support a high, explicit aggregate text limit by allowing additional draft
sections, then deliver a bounded multi-message notification packet when the
rendered text or files exceed one Discord message. It must define abuse,
rate-limit, file-size, component-count, partial-delivery, and retry behavior
before implementation. “Unlimited” is not an exit criterion. See Discord's
[component reference](https://docs.discord.com/developers/components/reference)
for the current platform limits.

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

P4.2e now implements `/game side` locally with the typed shape described by
this table: `game_id` and side selector are required, while role, name, and
clear are optional. The command reads publicly when replacement inputs are
omitted and routes edits through the existing host/staff and role-restricted
join behavior. `$gameside` and its aliases remain registered. No intentional
native compatibility compromise was introduced.

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
| `/game manage kick` | `kick` | Native now in P5.3: typed host/staff removal from an open game; uses the shared atomic pending-game worker |
| `/game manage extend` | `extend` | Staff extension of an open-game deadline; Native now as `/game extend` |
| `/game manage unstart` | `unstart` | Staff return of a started game to open/pending; Native now as `/game unstart` |
| `/game manage delete` | `delete` | Permission-sensitive game deletion; Native now as `/game delete` |

`manage` is intentionally not named `staff`: some operations may also be
available to a host or participant, and permissions belong to each command
rather than its spelling.

P5.3 implementation state: `/game manage kick game_id member` is implemented
locally and remains pending Tier-3 review. It defers before the shared worker,
keeps validation/permission failures ephemeral, and publishes committed
competitive-state output publicly. `$kick GAME_ID PLAYER` and its existing
bot-channel/registration checks remain unchanged. This is a direct typed
mutation, not a Components workspace; the interaction rules above do not make
Components a default presentation for simple lifecycle actions.

P5.4 implementation state: `/game start game_id name` is implemented locally
pending Tier-3 review. It is a direct required integer/string action that
defers before the shared bounded preflight/transition workers and uses the
classic dense production-style game card and post-commit lifecycle effects;
it has no modal, platform selector, or Components workspace. `$start` and
`$startgame` remain the canonical prefix entry points and share the same
bounded transition service.

## Complete system-wide capability map

The same conventions apply outside `/game`: common reads/actions stay direct,
independently useful attributes use read-or-edit commands, and dangerous
operator repair commands stay out of the public tree.

### Players, teams, squads, and ratings

| Current prefix handler(s) | Taxonomy v2.2 native home | Disposition / note |
|---|---|---|
| `player` | `/player show` | Implemented locally as public Components v2 workspace |
| `setname`, `steamname`, `setcode` alias behavior | `/player register` | Tier-3 reviewed and integrated with one canonical Polytopia name; requester by default, optional staff target; do not expose platform/name/code type. `$setname` shares the worker; `$steamname`/`$setcode` are non-writing deprecation adapters. Beta command deployment remains pending. |
| `getname` | `/player show` | Fold the useful canonical name into the normal profile workspace rather than preserving a name/code-specific lookup |
| `settime` | `/player timezone` | P6.2 implemented locally: native normalized UTC±HH:MM input, bounded 15-minute autocomplete, explicit clear, and a shared worker-backed prefix adapter retaining self/staff-target grammar; schema gate and beta smoke remain pending |
| `team` | `/team show` | P8.6 implemented locally in `7716398`, with Tier-2 correction `644ff95`: optional team with unambiguous requester-team inference; preserve the complete dense card/ELO graph, retain `$team` and `$team TEAM completed`, move database/plot work off-loop, sort each rendered roster by its displayed metric with stable ties, use an object-owned pyplot-free Agg renderer, and add only a requester-bound recent/completed roster-activity control; focused 27 and offline 810/22 gated skips; gated schema validation and integration remain pending |
| `team_add` | `/team create` | P8.5 Tier-3 reviewed, real-schema validated, integrated, and development-beta deployed with one required name option, the effective mod plus `allow_teams` boundary, a worker-local Team+GameLog transaction, exact-role membership guidance, and private validation/conflict failures; `$team_add` and `$team_add_junior` are intentionally retired because the alias has no distinct junior behavior; wider-beta acceptance remains pending |
| `team_emoji` | `/team emoji` | Implemented locally in P8.1: view by default; optional emoji/clear edits with the preserved team-enabled and mod boundary; beta not run |
| `team_image` | `/team image` | Implemented locally in P8.3: public effective-image read, typed attachment replacement, and explicit clear; direct URL replacement remains on the retained prefix path |
| `team_name` | `/team name` | Implemented locally in P8.2: public read/actor-attributed edit, legacy five-character and unique-name boundary, and an explicit exact-role rename warning |
| `team_server` | `/team server` | Implemented locally in P8.2: raw integer read/edit and explicit nullable clear without requiring external-guild membership |
| `team_edit` aliases | `/team house`, `/team tier` | `/team tier` remains implemented with the effective legacy mod plus PolyChampions/test scope, configured choices, mutation-only house/archive/exact-role gates, worker-owned Player/preference reconciliation, and post-commit role reconciliation. P8.4 implements native `/team house` read/assign/clear and intentionally retires the `$team_house` alias/branch; `$team_edit ... ARCHIVE` remains retained. |
| `squad`, `squads` | `/squad show` | P7.11 Tier-2 reviewed, integrated, and deployed: the only invocation is optional integer `squad_id`; omission defaults to requester membership; requester-only one-to-three Discord member selector, paged/selectable snapshot results, and dense card; both prefix registrations retire under C-012. The corrected no-match path is beta-accepted; exact card/member-search acceptance awaits an owned squad fixture |
| `squadname` | `/squad name` | P7.12 integrated as `0b8541f` from implementation `f04d017`: required squad ID; omission reads, optional name edits, explicit clear removes; authorized `/squad show` cards gain a shared-service Edit Name modal; the hidden prefix command is retired without an adapter under C-013. Development deployment is complete; wider identity-edit acceptance awaits an owned squad fixture |
| `lb` | `/leaderboard players` | Components v2 workspace defaults to local/current/active and exposes common views, population, paging, and requester-rank controls in-message; preserve the full prefix matrix |
| `lbrecent` | `/leaderboard activity` | Native now with explicit server-30-days and global-all-time views |
| `lbteam`, `teamlb` | `/leaderboard teams` | P7.10 implemented locally: no required slash options; current active all-tier results by default, with one common tier/population control, public requester-controlled pagination/page jump, bounded graph exploration, and the preserved prefix matrix |
| `lbsquad` | `/leaderboard squads` | Native now with current/all-time eligibility choices |
| `roleelo`, `roleeloany`, `freeagents` | `/leaderboard roles` | P7.13 implemented locally: no required slash options; the configured Free Agent preset is broadly accessible, while staff and House Leader/Co-Leader requesters receive a requester-bound 1–5-role All/Any Components selector with four sorts, global/local ELO scope, inactive exclusion, paging, and page jump. Retire `$roleelo`/`$roleeloany` without adapters; retain `$freeagents` through the shared bounded read service. CSV/file export is explicitly deferred |
| `recalc_games_from` | `/elo recalculate` | Native now; owner-only and confirmed |
| active job status (slash-only) | `/elo status` | Native now; staff-only |

P6.0 audited the underlying field meanings, mutation boundaries, compatibility
paths, and aggregate development data. The user accepted all six
recommendations on 2026-08-04. The detailed implementation contract is in
`PLAYER_IDENTITY_AND_PREFERENCES_AUDIT.md`; production inventory, production
data work, final legacy-field retirement, and deployment remain separately
gated.

`/player register` is preferred over `/player set name` because the common
flow establishes or updates the user's bot registration, not merely a display
field. It opens a small registration modal containing one canonical Polytopia
name. Staff targeting can be an optional typed member rather than a separate
public command.

The native interface deliberately omits the mobile-name, Steam-name, and
legacy-code distinction. P6.0 selects `DiscordMember.polytopia_name` as the
canonical account-wide field while preserving the other fields dormant until
production inventory and conflict review. `/player show` displays the
canonical name and can offer an authorized **Edit name** control; `/game
players` uses that same value. Existing database fields and selected prefix
aliases may remain temporarily for data migration and compatibility, but no
new slash command should ask which account-name type is being set. The P6.0
audit and a later approved data-cleanup unit must decide
which existing value becomes canonical when records disagree; taxonomy work
must not silently discard stored values.

#### Unified player workspace

`/player show member:[optional]` now opens one Components v2 workspace
rather than making users learn separate slash commands for the same player's
rating and game lists. It defaults to the requester and initially displays
**Overview**. A section selector and contextual controls expose:

- Overview/profile and canonical Polytopia name;
- current, peak, local/global, and all-time rating information;
- recent games;
- incomplete games;
- completed games, with all/win/loss refinement;
- season games, with current/recent season selection where applicable;
- team/squad context and permitted profile actions.

This does not eliminate the existing prefix entry points during transition.
They become deep links into the same renderer and bounded read service:

| Prefix entry | Initial workspace section |
|---|---|
| `$player`, `$elo`, `$rank` | Overview/ratings |
| `$incomplete` | Incomplete games |
| `$complete`, `$completed` | Completed games |
| `$wins` | Completed games filtered to wins |
| `$loss`, `$losses` | Completed games filtered to losses |
| `$allgames PLAYER` | All/recent games when exactly one player resolves |

Complex searches remain `/game search`: multiple players, teams, title/notes,
game size, `all`, or any query that does not resolve to exactly one player.
The player and game-search workspaces may share immutable game-row DTOs,
pagination, and rendering primitives, but they remain separate application
services with their own query semantics.

There is no slash `/elo PLAYER` alias. `$elo` is historical prefix vocabulary;
the native player-detail home is `/player show`, while `/elo` remains reserved
for rating maintenance and job status. A slash `section` option should be
added only if real direct-link demand is demonstrated; routine navigation
belongs in the components.

#### Current `/game show` implementation state

P7.9 implements the D-025-approved `/game show game_id:[optional integer]`
shape. An omitted ID is resolved only through one unambiguous current
game-channel association; ambiguity or absence requests an explicit ID
ephemerally. The public result is the production-style classic card over one
immutable, worker-loaded snapshot, containing the common game name,
status/result, dates, map/tribes, sides/players, ELO, notes/season/footer,
series summary, and safe winning-player/team imagery. Numeric `$game` and
`$match` use the same renderer; nonnumeric prefix input retains the existing
game-search delegation. The implementation deliberately adds no mutation
controls or game-log query to this bounded read unit. Pending cross-guild
lookups retain only the legacy server-association error; nonpending cross-guild
cards use plain names and suppress source member/role/channel identifiers.
Pending open/full cards retain join, start, friend-name, and balanced-draft
guidance with the configured prefix supplied by the display adapter. The
Components game-detail experiment was beta-rejected and removed; Components
remain an opt-in choice for a future unit only if a concrete current benefit is
demonstrated.

The intended team option shape is attribute-focused:

- `/team emoji team:[optional] emoji:[optional] clear:[optional]`;
- `/team name team:[optional] name:[optional]`;
- `/team server team:[optional] server_id:[optional integer] clear:[optional]`;
- `/team tier team:[optional] tier:[optional configured choice]`;
- `/team image team:[optional] image:[optional typed attachment] clear:[optional]`;

With no replacement or `clear` option, the command displays the current
setting. An omitted team is inferred only when the requester has one
unambiguous team; otherwise autocomplete/selection is required. Equivalent
safe patterns apply to team name/house/tier, squad name, house name/image,
player timezone, and focused game attributes.

#### P8.1 `/team emoji` implementation state

The code-only implementation registers `/team emoji` under the existing
`team` capability with the exact option shape
`team:string?`, `emoji:string?`, `clear:boolean?`. Omitted `team` is resolved
only from one persisted requester team; zero or multiple matches stay private
and request an explicit target. Omitted replacement reads publicly, while
successful emoji edits and `clear:true` removals are public and identify the
actor. Conflicts, malformed values, permission failures, ambiguous lookup,
and database failures remain private. `$team_emoji` remains registered with
its existing `allow_teams` and mod decorators and uses the same service.

The implementation checkpoint is `0bbfae0` on
`codex/p8-1-team-emoji`, based on exact clean base
`f93855b0eac3fb1e1f42119578c02ecac4213cd4`. Development capability
assignment was not edited, so the root remains default-deny and no Discord
command synchronization occurred. This unit deliberately does not add team
image, server, name, house, or tier behavior.

Compatibility accounting: no unapproved compromise was introduced. The
legacy Unicode-validator bug is intentionally corrected; ordinary Unicode
emoji now work, valid serialized static/animated custom emoji are accepted
without cache lookup, and malformed values previously admitted by the broad
`'<:'` substring test are rejected safely. Prefix wording, registration,
lookup semantics, and permission boundary remain compatible.

#### P8.2 `/team name`, `/team server`, and `/team tier` implementation state

The code-only implementation extends the existing default-deny `team`
capability/root with optional-target `/team name`, `/team server`, and
`/team tier` subcommands. All four team attributes share one guild-scoped,
worker-bounded autocomplete that excludes hidden/archived teams and returns at
most 25 choices. Omitted targets use persisted requester-team inference only
when exactly one team resolves; ambiguity and no-match errors remain private.

Name, server, and tier reads are public when their replacement is omitted.
Successful native edits are public and identify the actor; permission,
validation, ambiguity, conflict, and database failures remain ephemeral. Name
keeps the legacy five-character minimum and composite guild uniqueness, cannot
be cleared, does not rename the Discord role automatically, and warns that the
role must be renamed to the exact new team name. Server uses a typed raw
integer `server_id`, reads the configured value without requiring membership
in the external guild, and supports an explicit clear because the model field
is nullable. Tier accepts configured numeric choices/names and retains the
effective legacy mod plus PolyChampions/test scope in both native and prefix
forms; the League cog check, worker, and native pre-defer check enforce that
same boundary. A tier read within the allowed scope does not require the
mutation-only house, archived-team, or exact team-role preconditions; a tier
edit retains all three, updates persisted Player/team and house-preference
state inside the worker transaction, and reconciles current Discord member
roles only after the audited database transaction commits. Tier clear is
intentionally not exposed because the legacy workflow does not define a safe
clear/reconciliation contract.

`$team_name`, `$team_server`, and `$team_tier` remain registered and route
through the shared worker/service. `$team_tier` remains behind the original
League cog scope, then enters the bounded preflight before any legacy direct
Peewee path. At the later P8.4 checkpoint, `$team_house` was intentionally
retired and `$team_edit` no longer routes house changes; `$team_edit ...
ARCHIVE` remains retained. No
native-interface compatibility compromise was identified for P8.2, so no new
ledger row is required. The combined P8.1/P8.2 beta gate,
development-guild capability assignment, command synchronization, and beta
smoke remain pending separate approval.

#### P8.3 `/team image` implementation state

The code-only implementation extends the existing default-deny `team`
capability/root with `team:string?`, `image:attachment?`, and
`clear:boolean?`. It reuses the P8.1/P8.2 guild-scoped autocomplete and
requester-team inference. Omitted replacement reads the effective image
publicly; a typed Discord attachment replaces it; `clear:true` removes every
effective source. An attachment and `clear:true` are mutually exclusive.

Effective source precedence is the existing canonical local team file first,
then `Team.image_url`, then no image. The native command intentionally accepts
one typed attachment rather than a free-form URL because Discord already
provides a bounded upload interface and the existing URL path is a direct
prefix/operator workflow with no download semantics. `$team_image` remains
registered with its required team name, optional direct URL, attachment-wins
behavior, and existing mod/team-enabled decorators; its mutation now uses the
same staged publication and audited worker boundary. A stale lookup example
was corrected to use the retained command's `team_image` name.

The native read and committed mutation messages are public, and native
mutation success identifies the actor. Native validation, permission,
ambiguity/conflict, attachment/download, database, and pre-commit filesystem
failures remain ephemeral. A post-commit publication failure is a committed
state requiring public, actor-attributed reconciliation through the established
public sender; only failure of that public delivery uses an ephemeral fallback.
Local upload inspection and staging run off the Discord event loop. The
database change and `GameLog` row commit synchronously on a worker-local
connection; only after commit does the service publish a staged replacement or
quarantine/remove the old local override. A post-commit publication failure
retains a recoverable staged file when possible.

The implementation and focused tests are on `codex/p8-3-team-image` from exact
clean checkpoint `c009e5a`; no capability assignment, command synchronization,
beta launch, production action, dependency installation, push, or merge is
implied by this code-only checkpoint. The focused evidence and final commit
hash are recorded in the modernization roadmap.

#### P8.4 `/team house` implementation state

The code-only implementation extends the existing default-deny `team`
capability/root with `team:string?`, `house:string?`, and `clear:boolean?`.
Omitting `house` and `clear` publicly reads the current House affiliation;
providing `house` assigns it; `clear:true` removes it; and `house` plus
`clear:true` is rejected. Team and House suggestions are bounded to 25
choices, use the shared worker/executor boundary, and only infer a requester
team when exactly one persisted team resolves.

House reads are public only within the existing team-enabled
PolyChampions/test scope. Assignment and clear preserve the effective legacy
mod boundary and that same scope. Committed native mutations publish publicly
with actor attribution; validation, ambiguity, permission, conflict, and
database failures remain private. The worker reloads the team and selected
House inside one worker-local transaction, updates the team, reconciles the
captured Player/team and House-preference rows, and writes the GameLog audit
entry atomically. Discord role reconciliation occurs only after commit and
reports bounded public warnings when managed House roles are absent or a
member edit fails.

The legacy `$team_house` alias and its `team_edit` branch are intentionally
retired by explicit approval. `$team_tier` and `$team_edit ... ARCHIVE` remain
registered and materially unchanged, with archive guidance updated to use
`/team house ... clear:true`. This is recorded as compatibility ledger C-008;
the old message-only House mutation path is no longer available, while the
ordinary House workflow is covered natively.

Implementation/tests checkpoint: `9d72507b2913b7c842224e6ea624fc108404ad28`
on `codex/p8-4-team-house`, based on exact clean base
`e4de007be33bd1b6d7b29efc7dd79cf9024ad22e`. No schema migration, command
synchronization, beta/service lifecycle action, production action,
dependency installation, push, or merge is implied by this checkpoint.
Gated development-database validation is explicitly deferred while the
durable beta is running; the review task must not stop, restart, inspect, or
otherwise disturb that beta. Development Houses intentionally have no
Discord House roles, so live role reconciliation smoke remains a separately
approved fixture/oversight step.

#### P8.5 `/team create` implementation state

The code-only implementation registers `/team create` under the existing
default-deny `team` capability/root with exactly one required string option,
`name`. The input is trimmed and bounded to Discord's 1–100-character role
name boundary; empty, unsafe/invisible-control, and reserved broadcast names
remain private validation failures. The native interaction preserves the
effective mod plus `allow_teams` boundary both before defer and from the
captured primitive snapshot in the worker.

`$team_add` and `$team_add_junior` are intentionally retired by removing
their registrations. The alias has no distinct junior behavior, so the native
command deliberately has no `junior` option. There is no compatibility
adapter. The public committed success identifies the actor and Team, explains
that an exact matching Discord role is the existing membership convention,
and points staff to the focused `/team emoji`, `/team image`, `/team name`,
`/team server`, `/team house`, and `/team tier` commands. Validation,
permission, duplicate/conflict, and database failures remain private.

The worker uses frozen primitive request/result DTOs and the existing bounded
team executor. It opens a worker-local Peewee connection and keeps exactly
one synchronous `db.atomic()` around `Team(name, guild_id, is_hidden=False)`
with model defaults plus the actual-guild actor-attributed `GameLog`. Unique
constraint failures cover pre-existing duplicates and concurrent races; audit
failure rolls the Team back. No Discord await or role/channel/House/tier/
emoji/image/server/player/ELO/fixture effect occurs in that transaction or
after a failed operation. Cancellation drains without blocking the event
loop.

Implementation/tests checkpoint: `eafe219` on `codex/p8-5-team-create`, based
on exact clean base `d406dee5478360a097e381b5aff20e24d9b5fb9b`; accumulation
merge `37f2a47`. Independent affected validation passed 109 tests and complete
offline discovery passed 789 tests with 21 intentional gated skips. With the
guarded beta stopped and the writer audit clear, the unchanged gated suite
passed 20 real-schema tests with one intentional operator-fixture skip,
including the Team+GameLog commit/rollback case. The exact development-guild
`team` update was subsequently applied, the guarded beta restarted
successfully, and native acceptance remains open.

This is compatibility ledger C-011: the prefix retirement is intentional and
requires no native junior replacement. The stopped-writer schema gate and
Tier-3 review passed; native beta sync/smoke remains a separate deployment
action.

#### P8.6 `/team show` implementation state

The local implementation registers `/team show team:[optional string]` under
the existing default-deny `team` root and reuses the bounded team autocomplete.
An omitted target is resolved only from exactly one persisted requester team;
zero or multiple matches remain private. The retained `$team TEAM` and
`$team TEAM completed` adapters use the same frozen primitive request, worker
read, graph-byte renderer, and dense-card presentation path. The completed
prefix form starts on the all-completed roster metric.

The card preserves the production title/House/ELO/W-L layout, roster columns,
exact team-role membership, inactive-role exclusion, leadership fields,
recent-game summaries, local/URL team thumbnail, missing-role warning, and
ELO history graph. Each render stably sorts the roster by the metric it shows;
the worker preserves captured role-member order as the tie basis. The graph is
an owned `team-elo-<id>.png` attachment backed by in-memory bytes; the shared
`graph.png` path is not used, and the corrected renderer uses an object-owned
`FigureCanvasAgg` path without pyplot, style mutation, or global plotting calls.
One button only switches the already-loaded roster display between recent
30-day and all completed metrics. It is requester-bound and expiry-safe; native
success and refreshes are public, and native lookup/permission/ambiguity/
database or expired-control failures are private.

Implementation/tests checkpoint: `7716398`; Tier-2 correction checkpoint:
`644ff95` on
`codex/p8-6-team-show`, based on exact clean base
`8d6d469787475210002098697f0395af0bed5f4a`. Focused P8.6/taxonomy validation
passed 27 tests and complete offline discovery passed 810 tests with 22
intentional gated database skips. Compileall and diff checks passed after the
correction. The required development-worktree setup passed; the offline run
used a non-writing in-memory settings load scoped to this worktree because the
shared development settings name a newer `beta_testing` capability absent from
this branch's policy vocabulary. The read-isolated real-schema case exists but
was not run while the durable beta is active; no successful database evidence
is claimed. Prefix retention introduced no compatibility compromise and
therefore no new ledger row.

#### P7.11 `/squad show` implementation state

The local implementation defines the reserved `/squad` root with exactly one
subcommand, `/squad show squad_id:[optional integer]`. An exact ID loads only a
guild-affiliated squad; omission searches eligible squads containing the
requester. The public Components v2 workspace provides a requester-only
one-to-three-member Discord UserSelect, a bounded multi-match result select,
snapshot-only paging/page-jump controls, and dense cards containing squad
ID/name, member names and team emoji, current ELO, confirmed ranked W/L,
current leaderboard rank/length, and up to ten recent-game summaries. Result
selection and paging reuse the loaded immutable snapshot; a member selection
is the only control that performs a new bounded worker read.

Reads use a dedicated bounded executor, worker-local Peewee connection scopes,
and frozen primitive DTOs. The matching path preserves
`Squad.get_all_matching_squads` eligibility, ordering, and the 50-result
ceiling while adding the explicit guild boundary required by native exact-ID
and discovery behavior. Successful initial views, member searches, result
selections, and page changes are public; preflight, lookup, database,
component-validation, and expired-control failures are private. The command
defers privately before the worker read and publishes successful workspaces
through the established public transparency pattern.

The implementation checkpoint is `bdaa930` on
`codex/p7-11-squad-show`, based on exact clean base
`0f02aa3451192bf67cf72e01bac6a0e637ff65d0`. `$squad` and `$squads` were
removed without a prefix adapter; P7.11 retained `$squadname` only until the
approved P7.12 identity unit. P7.12 is integrated in `0b8541f` from
implementation `f04d017` on `codex/p7-12-squad-identity`, based on exact base
`ec347cad25f8bcaa059ee6c1d54e7744b63ef8f8`, and retires `$squadname` without
an adapter. `/leaderboard squads` plus `$lbsquad`/`$squadlb` remain unchanged.
The development-only `squad` capability was assigned and the root was
applied only to guild `478571892832206869`; the beta was restarted from
`7f4cb11` and announced for wider acceptance. Independent review passed 46
focused tests and complete offline discovery passed 875 tests with 25 gated
skips; the gated database suite passed 22 tests with 3 intentional skips.
The P7.12 identity-mutation case remains deferred because no persisted squad
fixture exists in `polytopia_dev`. The later discovery-stall correction adds
a passing real-schema no-match gate using a registered development player and
does not require creating a squad fixture.

#### P7.12 `/squad name` implementation state

The local implementation adds the exact `/squad name squad_id:<required
integer> name:[optional string] clear:[optional boolean]` command under the
existing reserved `squad` root. Omission reads the current squad name
publicly; a supplied name edits it; `clear:true` explicitly clears it; and
contradictory name-plus-clear input stays private. The hidden `$squadname`
command is completely retired under C-013 with no compatibility adapter.

`/squad show` dense cards capture member/staff edit eligibility and render an
Edit name modal only for eligible snapshots. The modal and direct slash
mutation call the same frozen-DTO service and one bounded ordinary-write
worker. The worker reloads the guild-scoped squad and current membership,
revalidates role-snapshot staff authority, and keeps the normalized 50-character
name plus actor-attributed `GameLog` entry in one synchronous atomic boundary.
Public actor attribution is sent only after commit. Modal commits reload the
dense card through the existing bounded squad-show path; refresh failure is
reported as committed-but-needs-reconciliation rather than a database failure.

Focused identity/show/taxonomy validation passed 20/18/8 tests, and complete
offline discovery passed 875 tests with 25 intentional gated skips. The
real-schema identity commit/rollback test remains behind the unchanged
development / `polytopia_dev` / `polybot_dev` gate and is skipped because no
squad fixture exists; requester discovery is now covered by a passing gated
no-match test. The development `squad` capability was assigned, the guild-only
root was applied, and the beta checklist/release announcement are complete;
wider acceptance remains open.

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

`$lbteam` and the now-implemented P7.13 role workspace remain separate native
surfaces because their Discord-role dependencies, graph/export behavior,
filters, and permissions differ. P7.13 uses every ordinary current-guild role
without a maintained allow-list, rejects `@everyone` and managed/cross-guild
roles at the interaction boundary, and leaves CSV/file export deferred.

`$lbteamjr` is legacy. It remains prefix-only until a later prefix-retirement
decision and receives no slash conversion; its documented junior-team
distinction is not implemented by the current callback.

P7.10 resolves the native team-leaderboard interface without adding a slash
alias or reproducing its small prefix argument matrix as invocation options:

| Prefix form | Native workspace state |
|---|---|
| `$lbteam` or `$teamlb` | Current ELO, active teams, all configured tiers |
| `$lbteam TIER` | Current ELO, active teams, selected tier |
| `$lbteam old` | Current ELO, active plus archived teams, all tiers |
| `$lbteam old TIER` | Current ELO, active plus archived teams, selected tier |

`/leaderboard teams` opens the first state directly. A bounded common-filter
control changes tier and active/archive population; pagination and page jump
remain in-message. The removed pre-reset/all-time path stays removed.
`$lbteamjr` remains an unchanged legacy prefix alias and receives no native
state or filter.

#### P7.13 Role leaderboard workspace

P7.13 implements the accepted native role-leaderboard taxonomy on isolated
branch `codex/p7-13-role-leaderboard` from exact base
`6e38c36e4ca865b952fa5e71a416ccd3fef9609c` (implementation/test commit
`40fbcf2`). The canonical invocation is exactly `/leaderboard roles` with no
required options. Its initial state is the configured Free Agent preset, so
ordinary users retain the broadly accessible convenience flow; staff and
configured House Leader/House Co-Leader requesters additionally receive a
requester-bound native RoleSelect for one to five roles and an All/Any matcher.

The workspace moves exploration into Components rather than creating a slash
option matrix. It exposes global ELO, local ELO, total games, and recent games
over the last 14 days as sort choices, plus global/local ELO-and-W/L display
scope, deterministic descending rows with stable Discord-ID ties, pagination,
and page jump. Only the selected scope's ELO and W/L appear in a row. The
configured Inactive role is excluded unless explicitly selected. Successful
initial and loaded-snapshot refinements are public; unauthorized, expired,
invalid, permission, and load failures are private.

Role eligibility is intentionally data-driven from the current guild rather
than a maintained allow-list. At the interaction boundary the selector
rejects `@everyone`, managed bot/integration roles, cross-guild roles,
duplicates, and more than five roles; ordinary current-guild roles remain
eligible. The event loop freezes primitive role/member data before a dedicated
bounded worker performs batched read-only Peewee aggregates, and all component
refinements reuse the immutable result without a database requery. The
existing role-lookup server/channel policy remains in force.

Compatibility is explicit: `$roleelo` and `$roleeloany` are retired without
adapters, `$freeagents` remains as a broadly accessible shared-service
convenience command, and CSV/file export is deferred. Other leaderboard roots,
player/team/squad workspaces, ELO semantics, and stored data are unchanged.
The gated real-schema read case is present but deferred to the next approved
stopped-writer window while the durable beta is active; no command sync or
beta lifecycle action is part of this unit.

### League and house workflows

| Current prefix handler(s) | Taxonomy v2.2 native home | Disposition / note |
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

### Legacy modules outside the modernization target

| Module / current behavior | Native home | Disposition / note |
|---|---|---|
| Bullet tournament: `bullet`, `nobullet`, `bulletstart`, `bulletsub`, `bullettoggle`, and results listener | none | Legacy. Keep current behavior if still operated, but do not add `/bullet`, `/bullet log`, or other native conversions |
| Anti-scam cross-channel message/image listener | none | Legacy listener with no command taxonomy. Do not redesign it as a slash/component workflow |
| Legacy API cog | none | Existing exclusion remains |

Legacy means “not an active migration or modernization target,” not
“immediately disabled.” These modules may be removed in the near future by a
separately approved retirement unit. Until then, narrowly necessary
operational, security, privacy/retention, and dependency-compatibility fixes
remain allowed and their current runtime/cutover documentation remains
authoritative.

### Per-unit legacy compatibility decisions

Prefix preservation is the default recommendation only for commands crucial
to day-to-day workflows, high-frequency or power-user bulk entry, or
capabilities not yet matched natively. Low-use, redundant, administrative, or
clearly superseded prefix commands may be retired with explicit user approval.
For each newly selected unit, oversight records a concise
`legacy recommendation: retain` or `legacy recommendation: retire` with its
rationale, and the user approves or revises that recommendation before
implementation. Existing user-approved preserved commands are not
retroactively removed by this policy; revisit them only when naturally touched
or during a later explicit prefix-retirement phase.

### General utilities and support

| Current prefix handler(s) | Taxonomy v2.2 native home | Disposition / note |
|---|---|---|
| customized `help` | `/help` or native discovery | Redesign after the registered tree stabilizes |
| `guide` | `/guide` | Strong candidate |
| `tribepoints` | `/tools tribe-points` | Strong candidate with map/mode choices |
| `rtribes` | `/tools random-tribes` | Redesign bans, free-tribe count, and duplicates |
| `credits` | `/about credits` | Strong candidate |
| `stats` | `/about stats` | Strong candidate after bounded read work |
| `staffhelp` | `/staffhelp` | Legacy recommendation: **retire** — explicit user-approved retirement of the low-use, redundant prefix adapter; the no-option native modal is the development wider-beta replacement for game reference, long description, and multiple uploads. The native JSONL intake is development-only and not production-ready; a separate approved production-boundary decision is required before P9. |

### Best modal/component candidates

| Capability | Recommended interaction | Why |
|---|---|---|
| Player leaderboards | `/leaderboard players` opens the accepted Components v2 workspace with presets, population toggle, paging, page modal, and requester-rank jump | Replaces four presentation-oriented slash options with a discoverable mobile/desktop UI |
| Unified player workspace | `/player show` opens Overview and lets the requester move among ratings, recent/incomplete/completed/season games, results, teams, and permitted edits; legacy prefix commands deep-link the matching section | Replaces several overlapping outputs without multiplying slash commands |
| Game detail | `/game show` displays the primary record, then offers players, logs, attributes, and permitted actions | Keeps common lookup short while making secondary information discoverable |
| Search and history | Essential target/query at invocation; Components v2 filters and pages refine the immutable result | Avoids large option lists and repeated commands while keeping reads bounded |
| Arbitrary game recording | `/game record` parses one roster string using the established `vs` grammar, then opens a short-lived preview with native side/member editing plus Confirm/Cancel | Restores uneven, larger, and multi-side coverage without message-content intent while keeping the initial fast text path |
| Game notes | `/game notes` reads directly; an Edit button opens a paragraph modal | Long text is awkward as a slash option and benefits from prefilled review |
| Player registration | `/player register` modal with one canonical Polytopia name and optional staff-selected member | Removes an obsolete platform/name/code distinction while keeping onboarding short |
| Team/house creation | Modal for name and typed attributes, followed by a review/confirm view | Multi-field creation is clearer than many optional slash arguments |
| Team/house image | Focused attribute command; one typed attachment is a direct replacement and `clear` is explicit | Native file upload avoids URL-only workflows; a modal remains optional for a future multi-field house/image workflow |
| Staff help | `/staffhelp` modal with summary/details, game reference, and up to 10 uploads per upload component | Native development-wider-beta replacement after explicit retirement of the redundant prefix adapter; supports structured reports and screenshots. It is not a production-ready intake until a separate P9 production-boundary decision is approved. |
| Game notification | `/game ping` composer with audience controls, repeatable text sections, multiple uploads, and public-effect preview/confirm | Separates authoring from potentially broad notification and supports bounded multi-message delivery |
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

Taxonomy v2.2 uses these domain roots:

- `/game`
- `/player`
- `/team`
- `/squad`
- `/leaderboard`
- `/league`
- `/house`
- `/elo`
- `/tools`
- `/about`
- `/staffhelp`
- `/guide` and possibly `/help`

This remains the earlier T-A domain-root architecture. T-B's one `/poly`
umbrella and T-C's flat hyphenated commands remain rejected design history:
the umbrella makes common commands unnecessarily long, while flat names make
dozens of capabilities harder to scan and organize.

## Effect on the current implementation if approved

The current modernization stack registers:

- `/game record|open|join|leave|search|show|win|unwin|delete|confirm|unconfirmed|set-ranked|extend|unstart`;
- `/elo recalculate|status`;
- `/leaderboard players|activity|squads` with temporary `/lb2` removed;
- `/player show|register|timezone`.

P6.1 implements `/player register member:[optional]` on the accepted
P6.0 identity contract. It opens exactly one modal field for the account-wide
canonical Polytopia name, rechecks the existing staff boundary for an
optional target, and publishes the committed result with actor attribution.
The reviewed unit is integrated, but the native command has not been
synchronized to Discord; `$setname` remains
the retained compatibility adapter and the legacy platform/code fields remain
stored but dormant.

P6.2 implements `/player timezone member:[optional] offset:[optional]
clear:[optional]` locally. Native offset values are canonical `UTC±HH:MM`
strings with at most 25 bounded autocomplete results; the retained `$settime`
adapter shares the minutes-backed worker and keeps compatible self/staff-target
grammar. The additive schema migration and real-schema validation are gated
separately, so the native command has not been synchronized to Discord.

For the already implemented game/ELO surface, Taxonomy v2.2 would change only
the slash registration/adapters:

| Current local path | Taxonomy v2.2 path |
|---|---|
| `/game record` | unchanged |
| `/game show` | unchanged |
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

The accumulation-branch tree has been synchronized only to the development
guild, and
none of these slash paths has reached production. Approval therefore still
allows a clean development rename without production compatibility aliases,
although the beta runbook must explicitly verify that obsolete guild commands
were pruned. Prefix commands, permissions, workers, transactions, and
post-commit effects would remain unchanged. `/game search` itself belongs to
the P7 bounded-read work; until that exists, the already-tested argument-free
`$confirm` behavior remains available and the slash list can be temporarily
omitted rather than shipping a name already marked for replacement.

## Guild capability policy and root-scope implications

P8.0 makes registration default-deny and repository-backed. The policy groups
top-level roots into explicit capability families:

| Capability | Current/reserved top-level roots | Intended scope |
|---|---|---|
| `core_user` | `game`, `leaderboard`, `player` | public user surface |
| `elo_maintenance` | `elo` | staff/maintenance |
| `team` | `team` | code-defined focused attribute root; remains default-deny until explicitly assigned |
| `league` | `league` | reserved future family |
| `house` | `house` | reserved future family |
| `squad` | `squad` | reserved future family |
| `tools_support` | `about`, `guide`, `help`, `staffhelp`, `support`, `tools` | reserved future family |
| `operator_only` | none | never an application command |

Each server-settings profile may assign only known capabilities to guild IDs
already present in that runtime profile's allowlist. Missing or empty
assignments register nothing. Unknown capabilities/roots, duplicate or
conflicting root definitions, operator-only assignments, and out-of-profile
guild IDs fail before remote work.

Discord filters application commands at top-level-root granularity. A policy
can therefore keep the `elo` root out of a user guild, but cannot hide one
staff subcommand inside a root that is otherwise public. A future command with
different visibility must use runtime permission checks or receive a separate
top-level root through a taxonomy decision; P8.0 does not rename roots or add
placeholder commands. The manager's desired-state plan includes creates,
updates, unchanged roots, and removals for each selected guild, and the only
supported remote scope is explicit guild scope. There is no global fallback.

The normal bot launch does not synchronize commands. Operators stop the beta,
run/review the offline plan, explicitly inspect/apply the exact development
guild when approved, then launch the beta without startup synchronization.

## Implementation and migration plan

1. Continue reviewing and approving or revising unsettled Taxonomy v2.2
   paths before their corresponding registration units.
2. Keep already integrated paths and accepted interaction experiments
   accurately distinguished from proposed renames in this document.
3. Preserve prefix commands, aliases, permissions, workers, transactions, and
   Discord-effect ordering.
4. Do not implement placeholder read commands merely to fill the proposed
   tree. Add `/game search` in P7 with its bounded read/pagination design.
5. Use the P8.0 default-deny capability policy and offline desired-state plan
   before any separately approved guild-scoped inspection/apply session. The
   bot's normal startup performs no command synchronization; launch it only
   after the explicit registration step and verify the exact registered tree.
6. Extend the taxonomy through bounded P4-P8 units, checking Discord's group,
   option, component, text, and attachment limits before each registration.
   Each unit must justify every slash option that remains instead of moving it
   into an interaction workspace.
7. Keep the API, Bullet, and anti-scam modules outside the modernization
   backlog unless the user separately authorizes retirement or reactivation
   as a target.
8. Before P9, audit the actual tree, descriptions, permissions,
   autocomplete cost, compatibility ledger, and naming consistency.
9. Implement and review a default-deny guild capability policy before the
   production canary. Keep prefixes available, use only explicit guild scope,
   and do not use a second bot process against the production database.

Changing slash placement remains technically manageable because adapters stay
thin over shared application/worker logic. Once paths reach production they
should be treated as a public API and changed only through a deliberate
deprecation window.
