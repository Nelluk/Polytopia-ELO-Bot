# Dynamic Guild Configuration and Onboarding Design

Status: architecture accepted; migration steps 2 through 5's offline typed
contract, additive development storage/import, shadow comparison, and explicit
development authority switch are implemented. Operator-control-plane work
remains separately bounded.

This document defines a safe replacement for PolyBot's hand-edited
`server_settings.py` / `server_settings_dev.py` guild dictionaries. It is an
architecture and migration contract, not a schema migration, live settings
change, Discord command deployment, or production authorization.

## Goals

The eventual system should let an operator enroll a new Discord guild and
adjust its ordinary bot settings without editing Python on the host. It must
also preserve the properties the static configuration currently provides:

- an explicit set of guilds the bot may serve;
- fail-closed permissions and application-command capabilities;
- development/production separation;
- deterministic startup behavior;
- no secrets or executable configuration in the guild store;
- explicit Discord command synchronization rather than startup side effects;
- auditable, reversible changes; and
- event-loop reads that never wait on PostgreSQL.

The first implementation should optimize for a small bot operated by Nelluk,
not for a general multi-tenant administration product. A web interface may be
added later, but it must use the same authoritative service and permission
rules as Discord rather than becoming a second configuration system.

## Current-state inventory

### Runtime bootstrap: never dynamic guild data

`runtime_config.py` currently loads these process-level values from the
selected ignored INI profile. They remain bootstrap/security configuration:

- exact environment (`development` or `production`);
- Discord token and expected application ID;
- database name, role, password, host, and port;
- owner and configured superuser identities;
- filesystem roots;
- background-task, HTTP API, and Bullet enablement;
- development-versus-production overlap acknowledgements; and
- the server-settings source/migration mode while rollout is in progress.

None belongs in a guild-editing UI or database document. In particular, a
database-backed setting must never be able to select its own database,
application identity, environment, filesystem, or enrollment authority.

### Current guild fields

The tracked examples define 27 default keys. Twenty-six describe real guild
behavior; singular `match_challenge_channel` has no source consumer and is a
legacy typo/predecessor of the plural setting. Current role policies use role
names, while current channel/category policies use Discord IDs.

The proposed authority column describes who may eventually activate a value.
"Local ordinary" means an owner-approved same-guild manager may eventually
edit it. "Local security" changes bot authorization or a private reporting
route and remains owner/superuser-only until the owner separately enables a
more restrictive security delegation for a Discord Administrator. Owner-only
fields affect enrollment, cross-guild visibility, or command exposure.

| Current key | Current shape | Main behavior | Proposed authority |
| --- | --- | --- | --- |
| `display_name` | string | Human-readable guild label in cards and diagnostics | Local ordinary |
| `command_prefix` | string | Retained message-command prefix | Local ordinary |
| `helper_roles` | role-name list | Staff authorization and staff-help relay | Local security, with live role validation |
| `mod_roles` | role-name list | Moderator authorization | Local security, with live role validation |
| `user_roles_level_1` | role-name list | Restricted-user permission tier | Local security |
| `user_roles_level_2` | role-name list | Normal-user permission tier | Local security |
| `user_roles_level_3` | role-name list | Full-user permission tier | Local security |
| `user_roles_level_4` | role-name list | Advanced matchmaking permission tier | Local security |
| `inactive_role` | optional role name | Inactivity, join/leave, and league handling | Local security |
| `require_teams` | boolean | Requires team-backed game sides | Local ordinary |
| `allow_teams` | boolean | Enables Team/House features and commands | Local ordinary |
| `allow_uneven_teams` | boolean | Allows uneven game-side sizes | Local ordinary |
| `max_team_size` | positive integer | Bounds configured side size | Local ordinary |
| `include_in_global_lb` | boolean | Includes guild data in cross-guild leaderboards/records | Owner only |
| `bot_channels` | channel-ID list or unrestricted sentinel | Ordinary command-channel policy | Local ordinary |
| `bot_channels_strict` | channel-ID list or inherited sentinel | Strict/read-only command-channel policy | Local ordinary |
| `bot_channels_private` | channel-ID list | Additional permitted channels omitted from guidance | Local security |
| `newbie_message_channels` | channel-ID list | Background beginner-message destinations | Local ordinary |
| `match_challenge_channels` | channel-ID list | Open-game broadcast destinations | Local ordinary |
| `ranked_game_channel` | optional channel ID | Ranked matchmaking destination | Local ordinary |
| `unranked_game_channel` | optional channel ID | Unranked matchmaking destination | Local ordinary |
| `steam_game_channel` | optional channel ID | Retained legacy matchmaking destination | Local ordinary |
| `log_channel` | optional channel ID | Audit/automatic-confirmation output | Local security |
| `game_announce_channel` | optional channel ID | Start/result announcement destination | Local ordinary |
| `staff_help_channel` | optional channel ID | Per-guild `/staffhelp` relay destination | Local security |
| `game_channel_categories` | category-ID list | Allowed destinations for managed game channels | Local ordinary |
| `match_challenge_channel` | optional channel ID | No current consumer; obsolete singular key | Do not migrate |

The migration should store role IDs, not names, after resolving every legacy
name to exactly one role in the same guild. The UI may display names and accept
typed Discord role options, but the committed identity is the ID. Renaming a
role therefore does not silently revoke or grant bot authority. `@everyone`
remains a special explicit value only for user permission tiers; it is invalid
for helper, moderator, inactive, or delegated-manager authority.

Channel and category IDs must likewise resolve inside the target guild and to
the expected Discord object type. Stored display names are informational only.

### Adjacent static values that are not ordinary guild fields

| Current value | Classification | Disposition |
| --- | --- | --- |
| `server_list` membership | Enrollment/security boundary | Replace with explicit enrollment state, not an editable boolean inside a settings document |
| `server_shortcut_ids` (`main`, `polychampions`, `test`) | Hard-coded topology/product identity | Replace gradually with typed feature/archetype assignments; do not expose arbitrary shortcut editing |
| `application_command_capabilities` | Desired Discord command exposure | Store per guild as owner-only policy; applying the remote tree remains a separate explicit operation |
| `application_command_all_guild_capabilities` | Broad deployment shortcut | Do not carry forward as a mutable wildcard; materialize explicit per-guild assignments |
| `lobbies` | Structured matchmaking presets | Design as a later versioned guild sub-resource, not part of the first settings document |
| `discord_id_ban_list`, `poly_id_ban_list` | Account/game security policy | Keep outside guild configuration and reconcile with authoritative persisted ban state separately |
| generic side names/emojis, `map_types`, `max_game_size` | Product catalogs/rules | Keep repository-backed unless a separate product decision makes a catalog dynamic |
| `league_tiers` | League product catalog | Keep repository-backed initially; later league configuration is a separate design |
| ELO/reset dates and join emoji/regex | Versioned application behavior | Keep in code or explicit process release configuration |

The existing `Configuration` table is also not the new settings store. It has
one row per guild but currently owns the PolyChampions draft JSON document.
Overloading it would mix unrelated lifecycles, make revisions/audits awkward,
and risk turning existing draft behavior into an implicit migration.

## Recommended architecture

### 1. Relational envelope with a strictly typed document

Use PostgreSQL as the authority, with separate tables conceptually equivalent
to:

- `guild_configuration_registry`: guild ID, enrollment state, active revision,
  delegation policy, generation, and enrollment metadata;
- `guild_configuration_revision`: immutable revision number, schema version,
  complete validated document, digest, parent revision, actor, and timestamp;
- `guild_configuration_draft`: expiring operator draft based on one exact
  active revision, with optimistic version metadata; and
- `guild_configuration_audit`: protected structured enrollment, activation,
  suspension, rollback, and delegation events.

The document may be stored as JSONB for evolution and atomic retrieval, but it
is not free-form JSON. A repository-backed schema owns every field, type,
bound, default, null rule, cross-field rule, and schema upgrade. Unknown keys
are rejected. Runtime code receives a frozen typed snapshot, never a raw dict
or live Peewee model.

Each revision contains a complete materialized document. Runtime lookup does
not inherit from a mutable global default row. Changing a code default can
therefore affect a newly created draft without silently changing established
guilds.

Rollback creates a new revision containing a previously accepted document and
records its source revision. Revision/generation values remain monotonic;
operators and caches never move backward ambiguously.

### 2. Immutable in-memory read service

Replace direct `settings.config` and `guild_setting()` dictionary access with
a `GuildConfigurationService` facade. Its hot path is a synchronous lookup in
an immutable process snapshot:

1. bootstrap the selected runtime profile;
2. open a worker-owned database connection and load all enrolled active guild
   revisions before Discord becomes ready;
3. validate and convert them to frozen primitive/value objects;
4. atomically publish one immutable registry snapshot; and
5. let checks, prefix resolution, presenters, and event handlers read that
   snapshot without database access or `await`.

An activation worker locks the registry row, verifies the draft's expected
base revision, validates again, writes the immutable revision and protected
audit in one transaction, commits, reloads a primitive snapshot on its own
connection, then atomically swaps the process cache. Discord messages are sent
only after commit.

If post-commit cache publication fails, the response must say the revision was
committed and runtime reconciliation is required. It must never claim rollback
or invite a duplicate activation. A bounded reload operation can reconcile
the committed active generation.

The first implementation should allow configuration writes only through the
bot process. A future web service must call the same application service. If
multiple writers/readers are later supported, add generation polling or
PostgreSQL notification as cache invalidation; never permit direct table edits
to masquerade as supported configuration changes.

### 3. Enrollment is a state machine, not presence in a dict

Recommended states:

- `pending`: the bot can see the guild but serves no user commands;
- `active`: one complete revision is active and the guild may receive its
  explicitly deployed command capabilities;
- `suspended`: all commands fail closed while data and revisions remain;
- `retired`: intentionally offboarded; reactivation requires owner review.

An unknown guild must not receive defaults, database rows, prefix processing,
or application commands. In the later onboarding phase, replace today's
automatic leave with quarantine: the bot remains inert long enough for the
owner to inspect and enroll an intended invite. Merely inviting the bot never
creates an enrollment or grants authority.

Version-one enrollment is owner-only and initiated from an already trusted
operator context or guarded CLI. It requires:

1. exact target guild ID and confirmation that the authenticated bot sees it;
2. a private preview of guild identity and bot permissions;
3. an explicit archetype/template selection using safe defaults;
4. live validation of every chosen role/channel reference;
5. an exact confirmation bound to the draft digest and current enrollment
   state; and
6. one transaction that creates the registry, first revision, and audit.

Activation does not synchronize Discord commands. It produces a desired-tree
plan and tells the owner that a separate guild-scoped plan/apply is required.
Startup, reconnect, enrollment, and ordinary configuration edits must never
call global or guild synchronization automatically.

Suspension is reversible and leaves application-command removal as a separate
explicit deployment operation. Retirement likewise does not delete games,
players, audits, or configuration history.

### 4. Discord-first control plane

The first implementation should extend the existing owner-protected operator
surface rather than create a web stack immediately. Suggested bounded flow:

- `/operator guild list`: enrolled, pending-visible, suspended, and drifted
  guilds;
- `/operator guild enroll`: owner-only enrollment preview and confirmation;
- `/operator guild settings`: open a private sectioned editor for one guild;
- `/operator guild validate`: type, cross-field, Discord-reference, and bot-
  permission validation without activation;
- `/operator guild activate`: digest-bound activation preview/confirmation;
- `/operator guild history`: immutable revision/audit summaries;
- `/operator guild rollback`: clone an earlier valid document into a new
  confirmed revision; and
- `/operator guild suspend` / `resume`: owner-only lifecycle controls.

Do not put all 26 settings into slash options. Use a private Components v2
workspace divided into Identity, Permissions, Teams, Channels, Background
Destinations, and Command Capabilities. Typed Discord role/channel selectors
should be preferred to raw IDs. Paginate long role/channel sets and show both
name and ID in review output.

Local self-service can follow after owner operation is proven. The owner may
enable ordinary delegation for one guild to explicit role IDs. That manager
may edit only `Local ordinary` fields in the same guild and cannot change bot
permission roles, private channel routes, enrollment state, command
capabilities, cross-guild leaderboard inclusion, delegation, or another guild.
A later security-delegation option, if still useful, requires a separate owner
opt-in and should additionally require Discord Administrator at use and commit
time. Discord Administrator alone never silently becomes PolyBot
configuration authority.

A future web interface authenticates through a separately reviewed identity
system and calls the same preview/validate/activate service. It does not write
tables directly and does not get broader permissions than the Discord flow.

### 5. Permission classes

| Action | Owner | Configured superuser | Delegated guild manager |
| --- | --- | --- | --- |
| Inspect active local settings | Yes | Yes | Own guild only |
| Edit/validate ordinary local settings | Yes | Yes | Own guild only |
| Activate ordinary local settings | Yes | Yes | Own guild only, if owner enabled activation delegation |
| Edit/activate authorization roles or private routes | Yes | Yes | No in the first delegated version |
| Enroll, suspend, resume, retire | Yes | No by default | No |
| Change command capabilities | Yes | No by default | No |
| Change global-leaderboard inclusion | Yes | No by default | No |
| Grant/revoke configuration delegation | Yes | No | No |
| Run remote command-tree apply | Existing explicit deployment policy | Existing explicit deployment policy | No |
| Change bootstrap secrets/process identity | Host configuration only | Host configuration only | No |

The eventual implementation may make a superuser privilege separately
configurable, but it must not infer it merely from a Discord guild role.

## Validation contract

Validation has three layers.

### Pure schema validation

- reject unknown/missing fields and wrong JSON types;
- require positive Discord IDs, unique normalized lists, and bounded list
  lengths;
- require a nonempty bounded display name and prefix;
- bound `max_team_size` to the repository-backed game-size contract;
- reject `require_teams=true` when `allow_teams=false`;
- allow `@everyone` only in user-level role lists;
- permit intentional helper/mod hierarchy overlap, while rejecting duplicates
  within a role list, ambiguous legacy-name resolution, `@everyone` staff
  authority, and future delegation overlap that would broaden authority;
- validate only known repository-backed command capabilities; and
- compute a canonical digest independent of JSON key order.

### Live guild validation

- every stored role/channel/category belongs to the exact target guild;
- every role ID resolves exactly and is not a managed integration role where
  member assignment is required;
- helper/mod/delegated roles are not `@everyone`;
- channel IDs resolve to supported channel types;
- game category IDs resolve to categories the bot can view and manage;
- destination channels permit the effects expected there;
- `staff_help_channel` is usable before `tools_support` is deployed;
- announcement/log/background destinations satisfy their feature
  prerequisites; and
- missing/deleted Discord references block activation rather than silently
  falling back.

### Activation revalidation

Immediately before commit, reload the enrollment row and active revision under
lock, confirm the exact draft base/digest and actor authority, and repeat all
database-controlled rules. Discord objects can change after validation, so
post-commit use must still detect missing permissions/references and report
reconciliation truthfully. Activation itself must not hold a database
transaction across Discord awaits.

## Failure and recovery behavior

- An unavailable database already prevents meaningful bot operation; do not
  silently substitute stale static settings.
- One malformed active guild revision should quarantine that guild and alert
  the owner without granting defaults. Whether startup continues for other
  guilds is an implementation decision to test explicitly.
- Deleted Discord references mark the affected setting/guild as drifted.
  Reads fail closed only for the affected capability where safe; authorization
  references such as helper/mod roles fail closed unconditionally.
- Drafts expire and never affect runtime behavior.
- Stale concurrent activation is rejected with the observed active revision.
- Transaction or audit failure rolls back the entire activation and leaves the
  in-memory snapshot unchanged.
- A committed revision plus failed cache publication is reconciled by exact
  active generation, not by repeating the write.
- No supported operation hard-deletes revision or audit history.

During rollout, an exact process-level source switch may retain the reviewed
static configuration as an emergency rollback. It must be selected explicitly
before process start and reported in redacted runtime diagnostics; automatic
fallback is forbidden. Once database authority is proven and static files are
retired, recovery relies on PostgreSQL backup/restore plus a guarded redacted
configuration export, not an untracked Python copy that can drift.

## Migration sequence

Each step is a separate bounded unit with its own review and evidence.

1. **Contract and inventory (this document).** No runtime or schema change.
2. **Typed schema/service offline implementation (complete in P10.2).** Frozen
   value objects, strict validators, canonical serialization/digests, and
   connection-free legacy materialization exist without changing runtime
   reads. The contract stores exact role IDs, preserves semantic role/channel
   order, canonicalizes command capabilities, rejects incomplete or extended
   documents, and treats `@everyone` as valid only in user permission tiers.
3. **Additive development schema and import tooling (complete in P10.3).**
   Three tables own the registry, immutable revisions, and protected audit;
   draft storage remains deferred until the control plane needs it. A bounded
   read-only Discord snapshot supplies exact same-guild role/channel identity,
   the plan is database- and Discord-connection-free, and the gated apply is
   transactional and digest-bound. Every inherited static value is
   materialized, role names resolve to exact IDs, the effective development
   staff-help mirror is explicit, and exact repeat apply/verify is idempotent.
4. **Development shadow read (complete in P10.4).** On the first ready cycle,
   reduce the live guild cache to bounded role/channel identity, materialize
   the effective static document, and load the stored active graph through a
   bounded read-only worker-owned connection. Publish one immutable
   matched/mismatch/malformed/unavailable result; static remains authoritative
   and only an exact semantic match permits consideration of promotion.
5. **Per-profile authority switch (complete in P10.5).** Development requires
   an exact pre-start `static` or `database` selector. Database mode reuses the
   current-process P10.4 match, gates command dispatch until one immutable
   snapshot is published, uses stable role IDs for authorization/effects, and
   stops startup without fallback on every non-match. Static rollback is an
   explicit selector change plus restart; production cannot select database.
6. **Owner control plane.** P10.6a adds bounded private list, sectioned active
   settings, live validation, and revision/audit history reads for the current
   already enrolled guild. P10.6b1 adds the separately versioned 24-hour
   inactive draft row and private typed preview/editor, with complete-document
   optimistic replacement and no activation. Later units add digest-confirmed
   activation and rollback. Keep enrollment, runtime reload, and command
   deployment separate.
7. **Quarantined onboarding.** Only after the control plane is proven, replace
   automatic leave with inert pending visibility and owner-only enrollment.
8. **Delegated local editing.** Add opt-in same-guild ordinary-setting
   delegation with the permission matrix above.
9. **Production plan/apply/canary.** Separately approve production schema,
   import, shadow comparison, authority switch, and rollback. Do not combine
   this with the current modernization cutover merely for convenience.
10. **Static retirement.** After stable operation, remove guild dictionaries
    from ignored Python while retaining bootstrap/security configuration and
    reviewed export/recovery tooling.

Lobby presets, league catalogs, bans, and a web frontend remain separate later
units. They should not enlarge the first migration merely because they are
currently near guild settings in `settings.py`.

## Required implementation evidence

Before database authority can be enabled, coverage should include:

- a registry test proving all legacy guild-setting literals are classified and
  no runtime consumer bypasses the service;
- strict schema, unknown-field, boundary, canonical digest, and schema-version
  tests;
- exact static materialization and role-name-to-ID ambiguity tests;
- owner/superuser/delegated permission matrices and cross-guild denial;
- unknown/pending/suspended/retired fail-closed behavior;
- stale revision, simultaneous draft, row-lock, audit-failure, and transaction-
  rollback tests;
- worker connection ownership, cancellation draining, immutable results, and
  event-loop responsiveness;
- committed-write/cache-publication reconciliation tests;
- deleted/moved role/channel/category and bot-permission drift tests;
- command-capability plan/apply separation and global-tree protection;
- startup with valid, malformed, missing, and unavailable configuration;
- complete offline discovery;
- explicitly gated development-schema import/shadow/switch/rollback tests; and
- beta onboarding and configuration smoke tests before any production plan.

No schema migration, production read, Discord command apply, or beta lifecycle
operation is authorized by this design.
