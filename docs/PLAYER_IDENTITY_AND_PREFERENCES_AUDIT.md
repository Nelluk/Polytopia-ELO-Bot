# Player identity and preferences audit

Status: P6.0 complete; all six recommendations accepted on 2026-08-04; P6.1
implemented locally with no schema, data, runtime, or command-sync action

Date: 2026-08-04

This audit defines the current player-identity and timezone boundaries before
P6 adds native registration and preference writes. It is subordinate to
`DATABASE_AND_SLASH_MODERNIZATION.md` and does not authorize a production
inventory, schema migration, data backfill, command retirement, or Discord
registration change.

## Conclusions

1. `DiscordMember.polytopia_name` is the recommended canonical Polytopia
   account name during modernization. It is global to a Discord account,
   already has precedence in modernized game flows, and is already displayed
   by `/player show`.
2. `Player.name` is not a Polytopia account name. It is the guild-specific
   Discord display label generated from username/nickname and remains the
   correct name for mentions, cards, search results, and historical records.
3. `DiscordMember.name_steam` and `DiscordMember.polytopia_id` remain
   preserved legacy data. New native workflows do not expose or write them.
   They must not be cleared or copied over conflicting canonical values
   without a separately approved production inventory and migration.
4. Registration consists of a global `DiscordMember`, a unique
   guild-specific `Player`, and a canonical Polytopia name. Existing global
   registration checks that look only for `DiscordMember` are not sufficient
   authority for a guild mutation; P6 workers must revalidate the
   `(discord_member, guild_id)` player row.
5. The existing `timezone_offset` column is PostgreSQL `smallint` and
   represents whole hours in stored data. The prefix parser nevertheless
   accepts `:30` and assigns a fractional value. Reproducing that behavior
   would preserve a bug.
6. The recommended timezone transition is a new nullable
   `timezone_offset_minutes` small integer with readers preferring it and
   temporarily falling back to `timezone_offset * 60`. This supports all
   civil UTC offsets in 15-minute increments without changing the feature
   into a location/IANA-timezone system.

## Current model and meaning

| Field | Scope | Current practical meaning | P6 direction |
|---|---|---|---|
| `DiscordMember.name` | Global Discord account | Last captured Discord username | Retain as Discord metadata |
| `Player.name` | Per guild | Generated Discord username/nickname display label | Retain; never repurpose as Polytopia identity |
| `Player.nick` | Per guild | Last captured guild nickname | Retain |
| `DiscordMember.polytopia_name` | Global Discord account | Mobile-era in-game name; already preferred as canonical | Canonical Polytopia account name |
| `DiscordMember.name_steam` | Global Discord account | Legacy Steam-specific name | Preserve dormant during transition |
| `DiscordMember.polytopia_id` | Global Discord account | Legacy 16-character friend code | Preserve dormant during transition |
| `DiscordMember.timezone_offset` | Global Discord account | Whole-hour UTC offset | Compatibility source only after P6.2 |
| proposed `timezone_offset_minutes` | Global Discord account | UTC offset in minutes | Canonical timezone preference after P6.2 |

`discordmember.discord_id` is unique. The live development schema also has
a unique `player(discord_member_id, guild_id)` index. None of the three
Polytopia identity fields is unique, and P6 should not add uniqueness:
Polytopia aliases may collide, and the legacy command already warns rather
than rejecting a duplicate.

## Current mutation behavior

`$setname`, `$steamname`, and `$setcode` are aliases of one callback but
write different global fields. The callback:

- resolves a live guild member through message text;
- permits self-service or a staff-targeted mention;
- derives a team from current Discord role names;
- calls `Player.upsert()`, whose DiscordMember and Player steps use separate
  transactions;
- writes the selected global identity field outside those transactions;
- writes a guild-zero `GameLog`;
- warns after commit when the selected value is duplicated.

This is not an acceptable P6 worker boundary. Registration, guild-player
upsert, canonical-name write, inferred persisted team, and audit entry need
one synchronous worker-local transaction. Discord member resolution and role
capture happen before submission; public acknowledgement and any Discord
effects happen only after commit.

`$settime` performs synchronous Peewee lookup/save on the event-loop thread.
It allows self-service and staff targeting, parses whole and half-hour text,
and writes the global `timezone_offset` field without an audit entry.

## Current readers and compatibility seams

- `/player show` displays
  `DiscordMember.polytopia_name or Player.name`. This fallback can make a
  Discord display label look like a registered Polytopia name and should
  become an explicit “not set” state.
- Modernized pending-game open/join and game-search paths generally use
  `polytopia_name or name_steam` as a transitional canonical account name.
- The classic game-detail worker still selects `polytopia_name` versus
  `name_steam` from the legacy game platform Boolean.
- `$getname` and aliases expose mobile name, Steam name, and legacy code as
  separate concepts.
- `$getnames` and its `names`/`codes` aliases are a materially useful
  game-setup workflow and should continue returning one canonical account name
  per player even after platform distinctions disappear.
- Player string matching still falls back to legacy code or
  `polytopia_name`; it does not search `name_steam`.
- CSV export and the dormant legacy API representation still expose separate
  fields. The API cog remains outside modernization, but export behavior needs
  an explicit later compatibility decision.
- Several help/error messages still advertise both `setname` and
  `steamname`. P6.1 should route all active guidance to one canonical
  registration path.

## Development-database evidence

The aggregate-only inventory ran through the unchanged integration preflight:

- `POLYBOT_ENV=development`;
- database `polytopia_dev`;
- role `polybot_dev`;
- background tasks and API disabled;
- a second psycopg2 session explicitly set read-only.

No player names, codes, Discord IDs, or attachment data were printed.

| Aggregate | Result |
|---|---:|
| DiscordMember rows | 32 |
| Player rows | 32 |
| Player rows in development guild | 32 |
| Canonical `polytopia_name` populated | 31 |
| Legacy Steam name populated | 0 |
| Legacy code populated | 0 |
| Timezone populated | 0 |
| Registered Player rows with neither account-name field | 1 |
| Both-name conflicts | 0 |
| Case-insensitive duplicate groups in any identity field | 0 |
| DiscordMembers represented in multiple guilds | 0 |
| Duplicate Player/member/guild pairs | 0 |

The development fixture population is intentionally canonical-name-heavy and
does not exercise migration conflicts. These results prove the beta can adopt
the proposed canonical write without a development backfill; they are not
evidence about production data.

## Production-safe canonical-name transition

P6.1 must not read or mutate production data. Before P9 migration, run a
separately approved aggregate-only production inventory and classify records:

1. `polytopia_name` present: preserve it as canonical.
2. `polytopia_name` absent and `name_steam` present: migration candidate,
   but copy only through an explicit reviewed backfill.
3. Both present and equal after trimming/case-folding: canonical field remains
   authoritative; legacy field stays preserved until cleanup.
4. Both present and different: never choose automatically. Require user/staff
   confirmation or leave the record flagged for reconciliation.
5. Legacy code only: it cannot produce a canonical account name; require
   registration.
6. No identity field: retain the Discord/Player rows and require registration
   before workflows that need an account name.

During transition, extract one shared read helper with the temporary behavior
`polytopia_name or name_steam`. New writes set only
`polytopia_name`. Once production migration and canary evidence are complete,
readers can stop falling back to `name_steam`. Legacy columns remain intact
until a later schema-retirement unit proves that no supported reader uses
them.

## Proposed P6.1 — Canonical registration and name

Risk: Tier 3 because it creates/updates a global identity, a guild Player,
persisted team assignment, and audit state.

Native interface:

- `/player register member:[optional]`;
- invocation opens a one-field modal for the canonical Polytopia name;
- requester is the default target;
- targeting another member requires the existing staff level;
- modal submission defers before database work;
- registration and canonical-name updates share the same operation.

The name should be trimmed, bounded to 200 characters for compatibility,
permit Unicode, reject empty/placeholder/control/newline values, and escape
mentions in presentation. Duplicate canonical names produce a warning and
audit evidence rather than a database uniqueness failure.

Worker transaction:

1. reload/create `DiscordMember` by primitive Discord ID;
2. reload/create the unique guild `Player`;
3. update captured Discord username/nickname metadata;
4. resolve at most one matching persisted Team from immutable role-name
   snapshots without passing a Discord object;
5. set only `polytopia_name`;
6. write an actor-attributed `GameLog` using the actual guild ID;
7. commit before public acknowledgement.

Changing a canonical name in one guild affects the DiscordMember in every
guild. The modal and success output must say that it is the account-wide
Polytopia name.

Compatibility recommendation:

- retain `$setname` as a thin adapter over the shared service through the
  production canary because registration is a primary workflow;
- keep `$getnames` plus `$names`/`$codes`/`$getcodes` during the
  transition, but return the canonical account name rather than platform/code
  variants;
- recommend retiring `$setcode`, `$getcode`, and the `$code` alias after
  explicit user approval because old friend codes are no longer an active
  workflow;
- recommend making `$steamname` a temporary deprecation adapter that directs
  users to `$setname` or `/player register`, not a second writable field;
- recommend folding `$getname`/`$name` into the existing
  `/player show` and retained `$player` profile path after explicit user
  approval.

P6.1 does not clear, backfill, or remove legacy fields.

P6.1 implementation evidence (2026-08-04): the local unit branch adds the
accepted `/player register` one-field modal and a dedicated bounded ordinary
write worker. The worker receives only frozen primitive Discord snapshots,
revalidates staff targeting from the captured role-name boundary, owns its
Peewee connection, and commits the DiscordMember/unique guild Player upsert,
canonical write, persisted-team inference, and actual-guild GameLog in one
transaction. `$setname` delegates to the same service; `$steamname` and
`$setcode` remain registered only as non-writing deprecation adapters. Focused
offline coverage includes rollback, duplicate warning, visibility, and
cancellation behavior. No production data, schema, runtime, or Discord
registration was changed; P6.2 remains separate.

## Proposed P6.2 — Timezone preference

Risk: Tier 3 because the preferred design is an additive schema migration.

Native interface:

- `/player timezone member:[optional] offset:[optional] clear:[optional]`;
- no offset/clear displays the current public preference;
- requester is inferred; another member requires staff;
- offset uses bounded autocomplete and accepts normalized `UTC±HH:MM`;
- reject offset plus `clear:true`;
- support `UTC-12:00` through `UTC+14:00` in 15-minute increments.

Schema transition:

1. add nullable `timezone_offset_minutes SMALLINT`;
2. readers prefer minutes, then temporarily fall back to
   `timezone_offset * 60`;
3. a separately gated backfill copies existing whole-hour values;
4. native and shared prefix writes use minutes only;
5. retain the old column until production canary and rollback windows close.

This preserves the current fixed-offset feature. It does not infer geographic
location or add daylight-saving behavior. IANA timezone identifiers would be
more expressive but would broaden privacy, autocomplete, display, and
migration scope without a demonstrated requirement.

Compatibility recommendation: retain `$settime` as a thin shared-service
adapter initially because it is a simple user preference and existing
game-organization workflow. Correct its half-hour parsing through the new
minutes representation rather than preserving the fractional-smallint bug.

## Accepted implementation decisions

The user accepted all six recommendations on 2026-08-04:

1. canonical field: reuse `DiscordMember.polytopia_name`;
2. retain `$setname` through production canary;
3. retire/deprecate `$steamname` and legacy code-specific commands as
   described above;
4. keep batch `$getnames` aliases because they remain useful for game setup;
5. use account-wide public success output for registration/name changes;
6. implement timezone offset-minutes as an additive schema transition and
   retain `$settime` initially.

After those decisions, P6.1 should be implemented before P6.2. Do not combine
the identity worker with the timezone schema migration.
