# Self-hosting PolyBot

This guide is for an independent installation using its own Discord
application, guild, PostgreSQL database, and credentials. Documents mentioning
GreenCloud, `/srv/polyelo`, fixed PolyElo guild IDs, or modernization release
candidates describe the upstream deployment and are not required here.

For the recommended self-contained installation using bundled PostgreSQL,
follow the root [Docker Compose guide](DOCKER.md). The native procedure below
is for operators who already manage Python and PostgreSQL themselves. Upstream
maintainers use the separate host-PostgreSQL beta definition in
`compose.beta.yaml`; independent deployments do not need it.

## Native requirements

- A Unix-like host with CPython 3.12, PostgreSQL, Git, and
  [uv](https://docs.astral.sh/uv/).
- A Discord application with a bot user.
- The Discord **Server Members Intent** and **Message Content Intent** enabled.
- An invite using both the `bot` and `applications.commands` OAuth scopes.

Use a dedicated PostgreSQL role/database and a dedicated Discord application
for each production or development installation.

## Discord permissions and role placement

Do not grant Discord Administrator merely to avoid selecting permissions.
For the default `core_user` capability, grant the bot role:

- View Channels, Send Messages, and Send Messages in Threads;
- Embed Links, Attach Files, and Read Message History;
- Add Reactions and Use External Emojis;
- Manage Messages for reaction-driven game and cleanup workflows;
- Manage Channels for private game-channel creation, permission overwrites,
  archival, and deletion; and
- Manage Roles for configured player, achievement, team, and inactivity roles.

Place the bot's role above every ordinary role it is expected to add or remove.
Discord will refuse role changes across or above the bot's highest role even
when Manage Roles is granted. Also allow the bot to view and send in every
configured bot, announcement, log, and staff-help channel.

Optional features require additional permissions:

- Moderate Members and Manage Messages allow the anti-scam listener to remove
  a detected burst and apply its timeout; without them it logs the failed
  enforcement.
- Kick Members is required only for configured league inactivity removal.

After inviting the bot, inspect its effective permissions in the configured
channel rather than relying only on the server-wide role checkboxes.

## Install dependencies

```bash
git clone YOUR_FORK_OR_UPSTREAM_URL PolyBot39
cd PolyBot39
uv sync --locked --no-dev --python 3.12
```

## Create PostgreSQL resources

The names below are examples; the schema tool uses the names in your config and
does not require upstream PolyElo names.

```sql
CREATE ROLE polybot LOGIN;
\password polybot
CREATE DATABASE polybot OWNER polybot;
```

Test the login before continuing:

```bash
psql -h localhost -U polybot -d polybot \
  -c 'SELECT current_database(), current_user;'
```

## Configure one Discord guild

```bash
cp config.ini-EXAMPLE config.ini
cp server_settings-EXAMPLE.py server_settings.py
chmod 600 config.ini server_settings.py
```

In `config.ini`, replace the bot token, bot user ID, owner user ID, and database
credentials. Keep background tasks, the API, and Bullet disabled for the first
start. Bullet additionally requires a Google service account and is tailored
to the upstream PolyChampions tournament workflow.

In `server_settings.py`, replace `SERVER_GUILD_ID` and `BOT_CHANNEL_ID`.
Discord's Developer Mode exposes **Copy ID** for users, guilds, and channels.
The example assigns only the `core_user` slash-command capability. Available
capability families are defined in `modules/application_command_policy.py`.

### Optional staff-help route

`/staffhelp` is disabled by default because an independent installation has no
safe delivery destination yet. To enable it:

1. create a private staff channel and a non-`@everyone` Helper role;
2. set `staff_help_channel` to that channel ID and make the first
   `helper_roles` entry match the role's exact name;
3. set `polyelo_feedback_route` to a mapping with `guild_id` and `channel_id`
   for a private bug/feature destination controlled by this installation's
   operator; and
4. add `tools_support` beside `core_user` in
   `application_command_capabilities`, then redeploy the guild's commands.

Independent operators should not send their users' reports to upstream
PolyELO channels. Publish instance-specific privacy, retention, security, and
support information before inviting real users.

Validate without connecting to Discord or PostgreSQL:

```bash
POLYBOT_ENV=production .venv/bin/python scripts/check_runtime_config.py
```

The output is redacted. Confirm the environment, bot ID, database/role, guild,
disabled optional features, and storage paths.

## Bootstrap or upgrade the schema

Ordinary startup never creates or alters schema. The same configured-target
tool handles both an empty database and additive upgrades:

```bash
POLYBOT_ENV=production .venv/bin/python scripts/manage_schema.py
```

Planning connects read-only and prints the exact configured database and role,
required operations, and confirmation token. Before applying, stop every bot
process connected to this database and take a PostgreSQL backup. Then use the
token printed by the plan:

```bash
POLYBOT_ENV=production .venv/bin/python scripts/manage_schema.py \
  --apply \
  --confirm 'APPLY PRODUCTION SCHEMA TO polybot AS polybot'
```

The apply verifies the live database identity, obtains a transaction-scoped
advisory lock, creates only missing model tables, applies known additive column
changes, creates the deferred winner foreign key if absent, commits atomically,
and verifies the complete startup contract through a new read-only connection.
It refuses incompatible existing columns. Repeating it is idempotent.

For upgrades, run the plan after pulling code and before starting the new
version. Do not substitute the upstream `*_production.py` migration scripts;
those remain fixed release controls for the upstream PolyElo database.

## Seed reference data

```bash
POLYBOT_ENV=production .venv/bin/python bot.py --add_default_data --skip_tasks
```

This adds missing Polytopia tribes and exits. It is safe to repeat.

## Deploy slash commands

First inspect the offline desired plan, replacing the example guild ID:

```bash
POLYBOT_ENV=production .venv/bin/python \
  scripts/manage_application_commands.py \
  --environment production \
  --mode plan \
  --guild-ids 123456789012345678
```

Remote inspection reads Discord state but does not change it:

```bash
POLYBOT_ENV=production .venv/bin/python \
  scripts/manage_application_commands.py \
  --environment production \
  --mode inspect \
  --guild-ids 123456789012345678
```

Apply only after reviewing both plans:

```bash
POLYBOT_ENV=production .venv/bin/python \
  scripts/manage_application_commands.py \
  --environment production \
  --mode apply \
  --guild-ids 123456789012345678 \
  --confirm-environment production \
  --confirm-guild-ids 123456789012345678 \
  --confirm-scope guild \
  --confirm-no-global-sync
```

The tool manages guild-scoped commands only and refuses to apply while global
commands exist. Repeat this process after changing command definitions or
capability assignments; normal bot startup intentionally does not synchronize
commands.

## First start and service operation

Start initially without background tasks:

```bash
POLYBOT_ENV=production .venv/bin/python bot.py --skip_tasks
```

Verify that the authenticated bot ID, guild membership, prefix commands, and
slash commands are correct. Stop with `Ctrl-C`. When ready, set
`background_tasks_enabled = true` and run without `--skip_tasks` under a process
supervisor such as systemd. Ensure only one writer uses a database at a time.

Start from `deploy/self-hosting/polybot.service.example` for a generic systemd
service. Replace its service user/group and `/opt/polybot` paths, ensure its
read-write paths match `image_root` and `log_root`, inspect the resulting unit,
and then install it under `/etc/systemd/system`. The other tracked production
units contain upstream absolute paths and should not be installed unchanged.

## Persistent data and backups

Back up all of the following:

- the PostgreSQL database;
- `data/images/`;
- private `config.ini` and `server_settings.py` files;
- any separately configured integration credentials.

Do not commit private configuration, Discord tokens, database passwords,
service-account JSON, logs, exports, or user data. Test database restores
periodically. `PRIVACY.md`, `SECURITY.md`, and `docs/DATA_RETENTION.md` document
the upstream service and are useful templates, but an independent operator
must review and adapt them to accurately describe their own infrastructure,
retention, subprocessors, and contact path before inviting real users.

## Development profile

For code testing, copy `config.development.ini-EXAMPLE` and
`server_settings_dev-EXAMPLE.py`, use an isolated bot/guild/database, and follow
`docs/DATABASE_SETUP.md`. The optional `production_*` fields add denylist
identifiers when the development checkout cannot read its own ignored
production configuration. A new standalone installation may leave them blank.
