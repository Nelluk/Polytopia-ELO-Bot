# Production guild-configuration cutover

## Status and boundary

This document records the completed replacement of production's stable static
guild settings with the database-backed authority already exercised by beta,
including the separately authorized guild-only Discord command release.

The source retains the exact production Discord snapshot, offline import plan,
digest-bound command planning, and explicitly acknowledged atomic production
import. That migration was bound to the reviewed production topology; current
runtime storage validates each installation's explicit environment,
application, database, role, guild inventory, and live Discord snapshot.

Verified on 2026-08-28:

- production runs from `/srv/polyelo/PolyBot39` as Compose project
  `polyelo-production`;
- the authenticated application ID is `484067640302764042`;
- the database identity is `polytopia2` / `polyelo` over the local socket;
- guild configuration authority is `database` with 25 published generation-one
  guilds on reviewed commit `b692bb1b`; and
- all five active guild-configuration tables contain the independently
  verified 25-guild bundle
  `2c3659b76702f327e3b679c0b3da2b59a21deb3fb8f19ecd5024581b00584d37`.

The final cutover started the reviewed image with zero unexpected restarts and
one production writer. All 25 documents published before recurring tasks or
League cache initialization. Two earlier attempts were rolled back cleanly
after their startup-order gaps were detected; no Discord command apply began
during either attempt. The later guild-only command release reconciled all 25
guilds, left the global tree empty, and verified with no remaining command
diffs.

## Exact migration policy

The static list was stable during migration, so this was a one-time
deterministic import, not a dual-write system.

| Imported type | Guilds | Result |
| --- | ---: | --- |
| Standard | 23 | Core user, Squad, and same-guild administration commands; no persistent Team, league, or house commands. |
| Team | PolyChampions Plus (`1289762588346814495`) | Persistent Team commands using the existing PolyChampions-owned Team records and the retained PCPLUS routing override. |
| League | PolyChampions (`447883341463814144`) | Persistent Team, league, and house commands. |

For all existing guilds the import preserves static permission roles, access
levels, side-size controls, channel destinations, command prefix, display name,
and global-leaderboard participation. It derives command capabilities from the
type above, preserves explicit operator/ELO-maintenance overlays, and enables
`/staffhelp` wherever a staff-help destination is configured.

The import proved these invariants before its write:

- exactly 25 static guilds, 25 allowed guilds, and 25 Discord snapshots;
- exactly one League guild and one Team guild;
- exactly three existing global-leaderboard participants;
- `allow_teams=true` only for PolyChampions and PCPLUS;
- `require_teams=true` only for PolyChampions;
- PCPLUS has Team capability but no league/house capability;
- all remaining guilds have Squad capability and no Team capability; and
- every configured role and channel resolves uniquely in the live guild.

## Completed runtime cutover and command release

1. Update the clean production checkout and build the reviewed image while the
   existing bot continues running from its current immutable image. Retain the
   running image ID, verify the new image still selects
   `guild_configuration_source = static`, and do not recreate the bot or sync
   commands.
2. Through one-off containers using the reviewed image, capture one bounded
   live Discord role/channel snapshot plus a private guild-owner inventory,
   then produce the offline import bundle and categorized cleanup report.
   Record its digest and the invariant summary above. Because the static list
   is stable, recapture only if the static file, guild ownership, or a
   referenced Discord role/channel changes.
3. Inspect the exact per-guild Discord command diff from the digest-bound
   import plan.
   Expected policy effects are `/squad` and `/guild` on all active guilds,
   `/team` only on PolyChampions and PCPLUS, `/league` and `/house` only on
   PolyChampions, and staff help only where a destination exists.
4. The separately approved online-static stage created all five additive
   tables and imported the 25 revision-one documents while the existing bot
   remained on static authority. Independent verification matched every row
   and digest; an exact repeat is a verified no-op.
5. Immediately before cutover, take and verify a fresh production backup. In
   one short approved maintenance action, stop the production bot, prove zero
   production writers, verify the staged bundle again, change the bound
   `config.ini` selector to `database`, and start the reviewed image.
6. Verify the authenticated identity, all 25 published runtime documents,
   stable restart count, and one writer. Smoke-test PolyChampions, PCPLUS,
   Polytopia Main, and one former legacy-Team house guild.
Steps 1–7 completed on 2026-08-28. The command release applied the
digest-bound guild-only plans to all 25 active guilds and an independent
read-back reported zero creates, updates, or removals. The global command tree
remained empty. No PostgreSQL write, bot restart, or downtime occurred during
that command release.

No generalized federation, duplicate PCPLUS established-Team records,
dual-write service, or soak period is required by the demonstrated risk.
PCPLUS's hidden generic game-side rows (for example Home and Away) are not
persistent named organizations and remain historical application data.

## Effective-reference cleanup

The legacy file contains role names and channel IDs that no longer resolve in
Discord. The import preserves effective static behavior rather than granting
new access by guessing replacements:

- missing and case-only role names are dropped;
- exact duplicate role names preserve every unmanaged matching role ID;
- managed integration roles cannot become permission roles;
- missing channels and categories are dropped;
- a non-null bot-channel restriction remains non-null even if cleanup leaves
  it empty; and
- `/staffhelp` is omitted when its configured destination no longer exists.

The private cleanup report groups findings by guild administration access,
ordinary-user access, bot-channel routing, operational destinations, inactive
status, and game categories. Each guild includes its live owner name/ID,
remaining valid mapping counts, and a severity of `review_before_cutover`,
`partial_cleanup`, `informational`, or `none`. Owner identity is migration
contact data only and is not stored in guild-configuration tables.

## Prepared command shape

After the reviewed image is built while the selector remains `static`, the
read-only preparation commands run from `/srv/polyelo/PolyBot39`. Use one-off
containers because the existing bot intentionally remains on its prior image:

```bash
docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_guild_configuration_storage.py snapshot

docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_guild_configuration_storage.py plan \
  --output logs/production/guild-configuration/import-plan.json

docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_application_commands.py \
  --environment production \
  --mode inspect \
  --guild-ids all \
  --guild-configuration-plan \
  logs/production/guild-configuration/import-plan.json
```

The snapshot and command inspection cross the live-Discord inspection boundary
and require that approval, but none of these commands writes PostgreSQL or
Discord.

The migration-era cleanup evidence can still generate its historical private
notification plan. This offline tool has no send operation, does not import
Discord, and groups multiple guilds owned by the same account into one bounded
proposed DM sequence:

```bash
docker compose run --rm --no-deps --entrypoint python bot \
  scripts/plan_guild_owner_notifications.py plan \
  --scope review \
  --guild-ids all \
  --output \
  logs/production/guild-configuration/owner-notification-plan.json
```

Scopes are `review`, `access`, `routing`, and `all`. This artifact remains
review evidence only; it cannot ping or DM an owner. The current one-time
rollout notice is instead previewed from **Owner notices** in
`/operator guild list`; that runtime workflow uses the published database
graph and a fresh Discord snapshot rather than this pre-cutover import plan.
Its test DM, production deployment, and final owner delivery remain separately
authorized external effects.

The following online staging command is deliberately only a command shape. It
must not be run until the backup and database-write approvals have been given
and the placeholder has been replaced with the exact
`online_static_staging_confirmation` printed by the reviewed plan. The command
refuses production profiles whose bound selector is not exactly `static`:

```bash
docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_guild_configuration_storage.py stage \
  --snapshot logs/production/guild-configuration/discord-snapshot.json \
  --confirm 'PRODUCTION GUILD CONFIGURATION ONLINE STATIC STAGE <bundle-digest>'

docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_guild_configuration_storage.py verify \
  --snapshot logs/production/guild-configuration/discord-snapshot.json
```

The completed runtime cutover used this separately approved downtime and
configuration-edit boundary:

```bash
docker compose stop bot

docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_guild_configuration_storage.py verify \
  --snapshot logs/production/guild-configuration/discord-snapshot.json

# Edit the bound config.ini selector to guild_configuration_source = database.

docker compose up -d bot
```

The completed Discord-write boundary was intentionally separate from runtime
cutover. This is the exact command shape that was reviewed and applied:

```bash
docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_application_commands.py \
  --environment production \
  --mode apply \
  --guild-ids all \
  --guild-configuration-plan \
  logs/production/guild-configuration/import-plan.json \
  --confirm-guild-configuration-plan '<bundle-digest>' \
  --confirm-environment production \
  --confirm-guild-ids all \
  --confirm-scope guild \
  --confirm-no-global-sync
```

## Rollback

Before command apply, retain the exact pre-cutover remote guild-command
snapshots and rollback plans. If runtime publication or smoke testing fails:

1. stop the bot and verify zero writers;
2. restore `guild_configuration_source = static`;
3. restore the saved pre-cutover guild commands if command apply had begun;
4. start the same reviewed image and verify static authority and one writer.

The five additive tables and imported rows can remain in place. Rollback does
not require dropping schema or deleting data.

## Explicit authorization stops

The following are separate production actions and must not be inferred from
source approval:

- deploying/recreating the production container while it remains static;
- capturing or inspecting live Discord state;
- sending any guild-owner notification;
- staging or changing the five tables and 25 imported configurations while
  static;
- editing `config.ini` to select database authority;
- applying guild-scoped Discord commands; and
- stopping or starting the production bot.

Immediately before the first database write or stop, the operator must state
plainly that the next command modifies production or takes it offline and wait
for approval.
