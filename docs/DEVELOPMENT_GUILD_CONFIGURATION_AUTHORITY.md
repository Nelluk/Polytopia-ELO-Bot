# Development database guild-configuration authority

P10.5 lets one development process select either the retained static guild
dictionary or the P10.3 PostgreSQL revision graph before startup. It does not
add a runtime toggle, poll, editor, schema change, command synchronization, or
production database mode.

## Explicit source selection

Set exactly one value in the ignored `config.development.ini` profile:

```ini
guild_configuration_source = static
```

or:

```ini
guild_configuration_source = database
```

Missing, blank, differently cased, or unknown development values stop profile
loading. Production continues to use static authority and rejects `database`;
an older production profile with no selector remains static until the separate
production migration/canary unit.

The selector is read once when the process starts. Changing the file does
nothing to an already running process. There is no automatic fallback:

- `static` starts with the retained dictionary and keeps P10.4's database
  comparison as diagnostics;
- `database` gates prefix and slash dispatch until the current Discord cache,
  effective static document, and stored active graph match exactly;
- an unavailable database, malformed graph, incomplete/deleted Discord
  reference, mismatch, or publication failure closes startup instead of
  serving from static settings; and
- rollback means explicitly selecting `static` and restarting the development
  beta. It is not a hot failover.

## Runtime boundary

One bounded read-only worker owns and closes its PostgreSQL connection, uses
the P10.4 timeouts and cancellation drain, validates the exact development
identity/schema/active graph, and returns frozen documents. After the exact
current-process comparison passes, P10.5 atomically publishes one immutable
in-memory snapshot for all guild-setting and command-capability reads.
Ordinary commands never query PostgreSQL for configuration.

The compatibility facade preserves existing prefixes, messages, channel IDs,
and role-name presentation. Authorization and live inactive/helper role
effects use stored role IDs, so a role rename does not silently revoke or
grant permission. Unknown guilds and missing complete settings fail closed.

P10.5 deliberately requires the stored document to remain semantically equal
to the retained static rollback copy. A later owner-control-plane unit must
replace that transitional comparison before database-only edits can diverge
from the rollback file.

## Validation and beta operation

Offline focused validation:

```bash
POLYBOT_ENV=development .venv/bin/python -m unittest -v \
  tests.test_runtime_config \
  tests.test_guild_configuration_runtime \
  tests.test_guild_configuration_shadow
```

The complete development PostgreSQL gate remains the stopped-writer suite.
The P10.4 live read-only case proves the exact stored graph, connection
ownership, and effective comparison; P10.5 then exercises publication through
the guarded beta startup with `guild_configuration_source = database`.

The isolated read-only publication regression is:

```bash
POLYBOT_ENV=development \
POLYBOT_P10_5_AUTHORITY_INTEGRATION=1 \
POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT=/home/nelluk/PolyBot39-beta/logs/development/guild-configuration/discord-snapshot.json \
  .venv/bin/python -m unittest -v \
  tests.test_guild_configuration_runtime_database
```

For deployment:

1. verify clean reviewed source and exactly one current development
   writer;
2. from `/home/nelluk/PolyBot39-beta`, run `docker compose stop bot` and
   require the host-wide writer audit to be clear;
3. run the complete gated development PostgreSQL suite and the P10.4 verifier;
4. set the ignored development selector to exact `database`;
5. start the durable beta with startup synchronization disabled;
6. verify the authenticated development application, Docker image identity,
   zero restart churn, one writer, and a log line containing
   `source=database status=matched` plus the active generation; and
7. verify representative retained-prefix and native permission/channel
   behavior.

No application-command apply is needed because P10.5 changes no command tree.
This is an operator-visible authority change, not a new tester workflow, so it
does not warrant a tester ping or a `WHAT TO TEST` expansion by itself.

If startup fails, inspect the bounded status/reason and current stored/static
drift. To restore service while investigating, set the selector explicitly to
`static`, restart only the development beta, and confirm the log reports
static authority with its shadow disposition. Never change production state
as part of this recovery.
