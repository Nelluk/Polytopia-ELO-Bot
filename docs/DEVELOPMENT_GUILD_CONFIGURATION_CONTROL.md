# Development guild-configuration control plane

P10.6a adds the first Discord control-plane surface for the already enrolled
development guild. It is deliberately read-only: it creates no revision,
changes no registry state, performs no activation or rollback, and never
synchronizes application commands.

## Command surface

The P10.6a commands are guild-only, owner-only, ephemeral, and available only while
the process is running with exact `development` / `database` guild-
configuration authority:

- `/operator guild list` shows the bounded registry with a prominent lifecycle
  state, active revision, generation, and latest suspension/resumption actor
  and timestamp when present;
- `/operator guild settings` shows one compact section for the invocation
  guild: Overview, Permissions, Teams, Channels, Destinations, or Command
  capabilities;
- `/operator guild validate` checks exact database identity and schema, the
  active document and digest, current Discord role/channel references, and
  equality with the running immutable revision/generation/digest; and
- `/operator guild history` shows bounded newest-first revision and protected
  audit summaries.

The current guild is the target. P10.6a does not accept a raw guild ID and
cannot inspect an unknown or unconfigured Discord guild. Registry listing is
bounded at 100 guilds. Database history reads are bounded at 25 revisions and
50 audit events; Discord output shows at most the newest 10 of each and marks
truncated storage results.

## Safety and ownership boundary

The interaction adapter denies non-owners before deferring. The worker repeats
the configured-owner check, validates exact development database/application
identity, opens one dedicated PostgreSQL connection, sets it read-only and
repeatable-read for a consistent multi-query snapshot, applies the existing P10
statement/connect/lock timeouts, rolls back that read transaction, and closes
the connection before returning immutable results. Cancellation drains the
worker so connection ownership cannot outlive the operation. Discord
publishers receive no ORM models or live database objects.

`validate` captures only the bounded, member-free role/channel identity already
defined by P10.4. Missing roles, managed permission roles, missing channels,
category/channel type errors, malformed documents, digest drift, schema drift,
database identity drift, and a stored revision newer than the running snapshot
all fail visibly without changing configuration.

P10.6a originally retained P10.5's startup requirement that the stored active
document match the static rollback copy. Later P10 units add separately
reviewed editing, activation, rollback, onboarding, command capability, and
lifecycle boundaries. P10.9 later adds separately stored, owner-controlled
ordinary-setting delegation through `/guild edit`; its complete boundary and
operator procedure are in `docs/DEVELOPMENT_GUILD_CONFIGURATION_DELEGATION.md`.
Production authority remains separate.

## Suspension and resumption

P10.8 adds owner-only `/operator guild suspend` and `/operator guild resume`.
Both commands must be invoked from a *different* active guild, take one exact
visible target guild ID, and show a private digest-bound plan. Suspension
preserves the active revision, drafts, and complete history; it changes only
the registry state, increments generation once, appends a protected lifecycle
audit, publishes a fail-closed runtime graph, and then removes the exact
target's application-command tree. The bot remains connected to the target,
but prefix, application-command, and listener dispatch are inert there.

Resume performs the reverse transition only after all saved role and channel
references pass current Discord validation. It publishes the restored runtime
policy before restoring only that guild's saved command capabilities. The
global command tree must be empty throughout and neither command has any
global synchronization path. Repeating an already completed action can repair
the exact Discord tree without a database write. Retirement remains separate.

The full operator and recovery procedure is in
`docs/DEVELOPMENT_GUILD_LIFECYCLE.md`.

## Validation and deployment

Focused offline validation:

```bash
POLYBOT_ENV=development .venv/bin/python -m unittest -v \
  tests.test_operator_guild_configuration \
  tests.test_guild_configuration_runtime \
  tests.test_guild_configuration_storage \
  tests.test_slash_taxonomy
```

The explicitly gated read-only real-graph case requires the reviewed absolute
Discord snapshot path and exact development database gate:

```bash
POLYBOT_ENV=development \
POLYBOT_P10_6A_CONTROL_INTEGRATION=1 \
POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT=/home/nelluk/PolyBot39-beta/logs/development/guild-configuration/discord-snapshot.json \
  .venv/bin/python -m unittest -v \
  tests.test_operator_guild_configuration_database
```

P10.6a changes the development-guild command tree by adding the `guild`
subgroup under the existing `operator` root. Deployment must therefore stop
only the guarded development beta, run the required stopped-writer database
gate, review the offline desired tree, inspect the empty global tree, apply
only guild `478571892832206869` with the exact no-global-sync confirmations,
and restart the durable beta with startup synchronization disabled. Verify all
four commands privately as the owner before any announcement decision.

This owner-only operational surface is not a tester workflow and does not
warrant a tester ping or a `WHAT TO TEST` expansion by itself.
