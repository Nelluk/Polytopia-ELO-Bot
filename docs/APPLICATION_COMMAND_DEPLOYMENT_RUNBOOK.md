# Guild Application-Command Deployment Runbook

P8.0 makes application-command registration an explicit, guild-scoped
operation. The bot does not synchronize commands from `on_ready`; launching a
beta cannot change the Discord command tree. Under database authority, P10.6c
adds a separate owner-confirmed one-guild capability workflow described in
`DEVELOPMENT_GUILD_COMMAND_CAPABILITIES.md`; it is not a startup side effect or
a replacement for this source-deployment procedure.

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
default-deny. Current families are `core_user`, `elo_maintenance`, `operator`,
and reserved `team`, `league`, `house`, `squad`, and `tools_support`. The
current `tools_support` family exposes `/staffhelp` only. The taxonomy names
`/about`, `/guide`, `/help`, `/support`, and `/tools` remain unloaded and
reserved; an assignment must not silently invent them. See
`modules/application_command_policy.py` for the authoritative membership.

An explicitly configured capability may apply to every allowed guild without
copying the same assignment into each entry. The reviewed production use is
`tools_support`, after every guild's `staff_help_channel` and first
`helper_roles` entry has been verified; development may use `operator` under
its separate policy:

```python
application_command_all_guild_capabilities = ('tools_support',)
```

This setting is also default-deny when missing or empty. Production
`tools_support` exposes only the public `/staffhelp` form: it directly relays
to the invoking guild's configured channel with only its first helper role
mentionable and writes no development feedback record. The development backend
retains the durable JSONL record and fixed beta mirror. Do not assign any
capability until its loaded root and environment-specific operational policy
have been reviewed; planning rejects an assigned but unloaded root.

Discord can filter only top-level roots. A capability cannot hide an
individual staff subcommand inside an otherwise-enabled public root. Use a
runtime permission check or a separately approved root for that case.
`/operator` is Administrator-visible by default, but its exact configured
owner/superuser checks remain the authoritative authorization boundary.

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

   Both remote modes first fetch and display the remote global command tree as
   a read-only `global` snapshot alongside the selected `guilds` plans.
   `--mode inspect` never mutates either scope. `--mode apply` refuses before
   any guild synchronization when the global snapshot is nonempty; the error
   names the observed global roots. An empty global snapshot permits the tool
   to create/replace desired roots and prune obsolete roots by replacing only
   the selected guild's local definitions on the client's existing
   `CommandTree`, then syncing with an explicit guild. Every other guild scope
   remains untouched. Repeating an unchanged plan performs no remote sync.
   There is no global apply, removal, synchronization, or fallback path.
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
- A nonempty global snapshot is evidence for a separately reviewed cleanup,
  not authority to remove it. Do not weaken or bypass the guild-apply guard.
- Planning and application-command synchronization do not require a database;
  do not add a database fixture or weaken a database gate for them.
- A failed validation must be fixed in policy/configuration before any remote
  mutation. Unknown capabilities and assigned roots absent from the loaded
  command source are not silently ignored. Reserved roots are harmless while
  their capabilities remain unassigned.

## Database-authority development exception

The profile-backed tool above remains authoritative for deploying changed
command source and for initial deployment of the P10.6c command itself. Once a
database-authority beta is already running P10.6c, capability assignments come
from its immutable active guild documents rather than ignored static settings.
The owner may then use `/operator guild commands` to activate a reviewed
capability-changing draft or reconcile the active policy. That path repeats
the empty-global guard, binds exact command fingerprints into its confirmation,
and contains only an explicit target-guild `sync` call.

Do not use the profile-backed offline plan as evidence for a later database
capability revision: it intentionally does not connect to PostgreSQL. Use the
P10.6c private plan for that revision. Production remains on this separately
approved out-of-process procedure until production database authority is
explicitly authorized.

## Static-production operator bootstrap

Production may use the hidden owner-only `$syncpcoperator` bootstrap when the
out-of-process manager cannot be run as the protected service account. This is
a narrow exception for the statically configured PolyChampions `operator`
capability, not a general startup-sync or arbitrary-guild path.

The command is available only in production, only to the configured bot owner,
and only when invoked from PolyChampions. The ignored production settings must
already assign `operator` to PolyChampions. Its first invocation performs the
same empty-global and exact-guild inspection as the guarded in-bot capability
service, then reports the create/update/remove plan and a digest-bound
confirmation. The owner applies it by rerunning the exact displayed command:

```text
$syncpcoperator SYNC PC OPERATOR <plan-digest>
```

The confirmed invocation rebuilds the plan from fresh remote evidence,
requires the digest to match, synchronizes only PolyChampions, and verifies
both the empty global tree and exact target convergence. It writes no database
state and does not restart the bot. A source-fingerprint mismatch in any
already-active root still fails closed and requires the external deployment
path. Once synchronized, normal future lifecycle control uses
`/operator bot restart`.

P8.0 evidence for this repository records offline planning only. No live
Discord inspection or apply is implied by the runbook or by a green offline
test suite.
