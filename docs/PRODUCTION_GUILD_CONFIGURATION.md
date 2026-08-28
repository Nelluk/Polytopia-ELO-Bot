# Production guild-configuration cutover

## Status and boundary

This document is the review packet for replacing production's stable static
guild settings with the database-backed authority already exercised by beta.
It does not authorize or perform a production database write, Discord command
sync, configuration edit, deploy, stop, or restart.

The source permits an exact production Discord snapshot, an offline import
plan, digest-bound command planning, and an explicitly acknowledged atomic
production import. Runtime database authority and the operator workers accept
only the exact reviewed production application/database topology. None of
those source paths runs at startup or from an ordinary deployment while the
selector remains `static`.

Verified on 2026-08-28:

- production runs from `/srv/polyelo/PolyBot39` as Compose project
  `polyelo-production`;
- the authenticated application ID is `484067640302764042`;
- the database identity is `polytopia2` / `polyelo` over the local socket;
- guild configuration authority is `static` with 49 allowed guilds; and
- none of the five guild-configuration tables exists.

## Exact migration policy

The static list is stable, so the migration is a one-time deterministic import,
not a dual-write system.

| Imported type | Guilds | Result |
| --- | ---: | --- |
| Standard | 47 | Core user, Squad, and same-guild administration commands; no persistent Team, league, or house commands. |
| Team | PolyChampions Plus (`1289762588346814495`) | Persistent Team commands using the existing PolyChampions-owned Team records and the retained PCPLUS routing override. |
| League | PolyChampions (`447883341463814144`) | Persistent Team, league, and house commands. |

For all existing guilds the import preserves static permission roles, access
levels, side-size controls, channel destinations, command prefix, display name,
and global-leaderboard participation. It derives command capabilities from the
type above, preserves explicit operator/ELO-maintenance overlays, and enables
`/staffhelp` wherever a staff-help destination is configured.

The import must prove these invariants before any write:

- exactly 49 static guilds, 49 allowed guilds, and 49 Discord snapshots;
- exactly one League guild and one Team guild;
- exactly four existing global-leaderboard participants;
- `allow_teams=true` only for PolyChampions and PCPLUS;
- `require_teams=true` only for PolyChampions;
- PCPLUS has Team capability but no league/house capability;
- all remaining guilds have Squad capability and no Team capability; and
- every configured role and channel resolves uniquely in the live guild.

## Minimal release and cutover

1. Merge the reviewed source and deploy it while production remains on
   `guild_configuration_source = static`. Do not sync commands. Verify identity,
   schema plan, stable container, and the one-writer census.
2. Capture one bounded live Discord role/channel snapshot and produce the
   offline import bundle. Record its digest and the invariant summary above.
   Because the static list is stable, recapture only if the static file or a
   referenced Discord role/channel changes.
3. Inspect the exact per-guild Discord command diff from the digest-bound
   import plan.
   Expected policy effects are `/squad` and `/guild` on all active guilds,
   `/team` only on PolyChampions and PCPLUS, `/league` and `/house` only on
   PolyChampions, and staff help only where a destination exists.
4. In one short approved maintenance action: take the normal production
   backup, stop the production bot, verify zero production writers, atomically
   create all five additive tables and import the 49 revision-one documents,
   verify every row/digest, change the bound `config.ini` selector to
   `database`, apply the reviewed guild command plans, and start the bot.
5. Verify the authenticated identity, all 49 published runtime documents,
   stable restart count, and one writer. Smoke-test PolyChampions, PCPLUS,
   Polytopia Main, and one former legacy-Team house guild.

No generalized federation, duplicate PCPLUS Team records, dual-write service,
or soak period is required by the demonstrated risk.

## Prepared command shape

After the reviewed source is deployed while the selector remains `static`, the
read-only preparation commands run from `/srv/polyelo/PolyBot39`:

```bash
docker compose exec -T bot python \
  scripts/manage_guild_configuration_storage.py snapshot

docker compose exec -T bot python \
  scripts/manage_guild_configuration_storage.py plan \
  --output logs/production/guild-configuration/import-plan.json

docker compose exec -T bot python \
  scripts/manage_application_commands.py \
  --environment production \
  --mode inspect \
  --guild-ids all \
  --guild-configuration-plan \
  logs/production/guild-configuration/import-plan.json
```

These commands still cross the live-Discord inspection boundary and require
that approval, but they do not write PostgreSQL or Discord.

The following is deliberately only a command shape. It must not be run until
the backup, database-write, Discord-apply, configuration-edit, and downtime
approvals have been given and the printed digest placeholders have been
replaced with the exact reviewed values:

```bash
docker compose stop bot

docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_guild_configuration_storage.py apply \
  --snapshot logs/production/guild-configuration/discord-snapshot.json \
  --confirm 'PRODUCTION GUILD CONFIGURATION APPLY <bundle-digest>' \
  --production-maintenance

docker compose run --rm --no-deps --entrypoint python bot \
  scripts/manage_guild_configuration_storage.py verify \
  --snapshot logs/production/guild-configuration/discord-snapshot.json

# Edit the bound config.ini selector to guild_configuration_source = database.

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

docker compose up -d bot
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
- writing the five tables and 49 imported configurations;
- editing `config.ini` to select database authority;
- applying guild-scoped Discord commands; and
- stopping or starting the production bot.

Immediately before the first database write or stop, the operator must state
plainly that the next command modifies production or takes it offline and wait
for approval.
