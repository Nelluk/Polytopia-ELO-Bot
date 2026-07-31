# Guild Application-Command Deployment Runbook

P8.0 makes application-command registration an explicit, guild-scoped
operation. The bot does not synchronize commands from `on_ready`; launching a
beta cannot change the Discord command tree.

## Policy

Add assignments only to the selected profile's tracked settings template (or
the ignored profile settings file when an operator has separately prepared
it):

```python
application_command_capabilities = {
    478571892832206869: ('core_user',),
}
```

The guild ID must already be in that profile's `server_list`. The policy is
default-deny. Current families are `core_user`, `elo_maintenance`, and
reserved `team`, `league`, `house`, `squad`, and `tools_support`; operator-only
work has no application-command capability. See
`modules/application_command_policy.py` for the authoritative membership.

Discord can filter only top-level roots. A capability cannot hide an
individual staff subcommand inside an otherwise-enabled public root. Use a
runtime permission check or a separately approved root for that case.

## Required sequence

1. Stop the development beta and confirm host-wide that only the untouched
   production process, if any, remains. A sandboxed process view that cannot
   see sibling task/PTY sessions is not sufficient.
2. Set `POLYBOT_ENV` explicitly and run the offline plan. Planning loads local
   command metadata through an isolated model-free source loader; it does not
   connect to Discord or either database:

   ```bash
   POLYBOT_ENV=development \
   /home/nelluk/PolyBot39-dev/.venv/bin/python \
   scripts/manage_application_commands.py \
   --environment development \
   --mode plan \
   --guild-ids 478571892832206869
   ```

   Review every guild's desired roots and create/update/unchanged/remove
   lists. Unassigned allowed guilds intentionally plan an empty tree so stale
   roots can be pruned.
3. Obtain separate approval for remote inspection or apply. Remote modes
   require explicit `--guild-ids` within the runtime allowlist. Apply also
   requires all of the following to match exactly:

   ```bash
   --confirm-environment development \
   --confirm-guild-ids 478571892832206869 \
   --confirm-scope guild \
   --confirm-no-global-sync
   ```

   `--mode inspect` fetches current guild commands without mutation. `--mode
   apply` creates/replaces desired roots and prunes obsolete roots by syncing
   a fresh guild-local `CommandTree`. Repeating an unchanged plan performs no
   remote sync. There is no global mode or global fallback.
4. After the explicit guild operation is complete and separately approved,
   launch exactly one development beta from the reviewed checkpoint. Startup
   performs no command synchronization. Verify the authenticated application,
   environment, guild, process identity, and command tree during the approved
   smoke session.

## Safety boundaries

- Never omit `POLYBOT_ENV` or rely on its production default.
- Never pass a production guild to a development profile.
- Never use this runbook to connect a beta process to `polytopia2`.
- Never call `CommandTree.sync()` without an explicit guild. This tool does
  not expose a global deployment flag.
- Planning and application-command synchronization do not require a database;
  do not add a database fixture or weaken a database gate for them.
- A failed validation must be fixed in policy/configuration before any remote
  mutation. Unknown roots and capabilities are not silently ignored.

P8.0 evidence for this repository records offline planning only. No live
Discord inspection or apply is implied by the runbook or by a green offline
test suite.
