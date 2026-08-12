# Development guild command-capability activation

This runbook covers P10.6c's owner-only development workflow. It does not
authorize production configuration, production Discord state, global command
synchronization, schema changes, or startup synchronization.

## Preconditions

- `POLYBOT_ENV=development` and database guild-configuration authority are
  selected before the bot starts.
- The operator invokes the command from a guild already active in the running
  database snapshot.
- The exact target is also active and visible to the authenticated development
  bot. It may be a P10.7-enrolled prefix-only guild.
- The remote global application-command tree must be empty. Any global root is
  a hard stop; this workflow cannot remove or synchronize global commands.

Only the configured bot owner can activate command capabilities or apply a
remote tree. An enrolled target does not gain this authority merely because a
Discord administrator is present there.

## Capability-changing flow

1. Run `/operator guild edit`, supplying `target_guild_id` when managing a
   different active guild.
   Cross-guild mode intentionally exposes only Command capabilities because
   Discord role/channel selectors are bound to the invoking guild. After the
   target receives `operator`, open its editor from inside that guild for
   ordinary role/channel settings.
2. Create or reset the complete inactive draft, select **Command
   capabilities**, and choose the desired repository-known capabilities.
3. Make any other intended complete-document edits and run **Validate**.
   Ordinary **Activate** deliberately refuses a capability-changing draft.
4. From an already trusted operator guild, run `/operator guild commands` with
   the same optional exact target ID.
5. Review the private plan. It binds the active revision/generation/document,
   draft version/full digest, current and desired capabilities, exact remote
   command fingerprints, create/update/remove sets, and an empty global tree
   into one SHA-256 plan digest.
6. Type the complete displayed
   `ACTIVATE COMMANDS <draft-digest> <plan-digest>` confirmation.

The bot revalidates the draft and Discord tree before writing. One database
transaction appends the complete immutable revision, advances generation,
records the command-plan digest in the protected activation audit, and consumes
the draft. It then publishes the new runtime policy before synchronizing only
the exact target guild. The runtime command-tree check is independently
default-deny, so a stale remotely registered root cannot execute after removal.

After the guild-only sync, the bot re-reads both the global and target trees.
Success requires the global tree to remain empty and the target tree to match
the confirmed desired fingerprints exactly.

If an already-active root's local source fingerprint differs from Discord,
planning stops and directs the operator to the external deployment runbook.
P10.6c may create a newly enabled root, update a stale registration for a root
being newly enabled, or remove a disabled root; it does not smuggle unrelated
source-shape updates into a settings activation.

## Reconciliation without a database write

Run `/operator guild commands target_guild_id:<exact-id>` when:

- activation reports that the database committed but Discord convergence is
  unverified;
- the bot restarted after such a committed activation; or
- an operator wants to verify the active database policy against Discord.

When no current draft changes capabilities, the command plans the already
active policy. If the tree is already exact it reports a no-op. Otherwise the
confirmation is `SYNC COMMANDS <plan-digest>` and only the target guild is
synchronized. No revision, generation, draft, or audit changes in this mode.

Never repeat the consumed activation to repair Discord state. Reconciliation
uses the authoritative active document.

## Failure truth

- Preview, stale evidence, validation, or transaction failure before commit
  changes neither database authority nor Discord state.
- A committed revision followed by runtime publication failure requires
  `/operator bot restart`; do not repeat activation.
- A committed revision followed by Discord apply or verification failure keeps
  the new fail-closed runtime policy. Run `/operator guild commands` again to
  reconcile the active policy without another database write.
- A reconciliation-only failure writes no database revision. Open a fresh plan
  before retrying because remote state may be uncertain.
- P10.6b3 rollback continues to reject a historical revision whose command
  capabilities differ. Restore those capabilities through a fresh draft and
  this coordinated workflow instead.

## Development deployment of P10.6c itself

The new nested `commands` command changes the existing development-guild
`operator` root. Before restarting the beta, use the established offline plan,
remote inspect, exact development-guild apply, and repeat inspect from
`APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md`. The first deployment of the code
must still use that out-of-process tool because the older running beta does not
yet contain `/operator guild commands`.

Do not ping testers for this owner-only control-plane capability. Live
acceptance should use a harmless development target and must not manufacture a
second enrollment merely to exercise the workflow.
