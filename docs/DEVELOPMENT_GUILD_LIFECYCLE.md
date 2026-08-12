# Development guild suspension and resumption

This runbook covers P10.8's owner-only lifecycle controls for the database-
authority development bot. It does not authorize production state, global
application-command synchronization, schema changes, retirement, or deletion.

## Contract and preconditions

- Invoke the command from an active trusted development guild different from
  the exact target. The last active guild cannot be suspended.
- The configured owner is the only caller. Discord Administrator alone grants
  no lifecycle authority.
- The target must remain visible to the authenticated development bot and must
  have an existing valid active revision.
- Resume additionally requires every saved Discord role/channel reference to
  pass current live validation.
- The remote global application-command tree must be empty. Any global root is
  a hard refusal.

Suspension leaves the bot in the guild, but removes that guild from the
immutable runtime allowlist. Prefix commands, application interactions, and
guild listener events then fail closed. The active revision pointer, document,
inactive draft, revision history, and prior audits are preserved. Only the
registry state and generation change, with one new protected audit event.

## Operator flow

From a different active control guild, run one of:

```text
/operator guild suspend target_guild_id:<exact active guild ID>
/operator guild resume target_guild_id:<exact suspended guild ID>
```

Review the target name/ID, lifecycle state and generation change, unchanged
revision/document digest, exact target-guild command diff, command-plan digest,
and empty-global evidence. Confirm by typing the complete displayed value:

```text
SUSPEND GUILD <guild-id> <document-digest> <command-plan-digest>
RESUME GUILD <guild-id> <document-digest> <command-plan-digest>
```

The database transition commits first under the shared configuration lock.
The bot reloads and publishes the complete immutable active graph, then applies
and verifies only the exact target guild's command tree. Suspension removes all
target roots; resume restores the command capabilities saved in the unchanged
active revision. No startup, reconnect, ordinary edit, or lifecycle operation
performs global synchronization.

Use `/operator guild list` from an active guild to inspect every registry row.
The list labels active, suspended, pending, and retired states and shows the
latest suspension/resumption actor and timestamp.

## Failure truth and reconciliation

- A preview, evidence, validation, or transaction failure before commit changes
  neither database state nor Discord commands.
- If the state commits but runtime publication is unverified, do not repeat the
  database transition. Restart the development bot so startup reloads the
  authoritative active graph.
- If state and runtime publication succeed but Discord apply/verification is
  uncertain, rerun the same suspend or resume command. It produces a
  `SUSPEND SYNC ...` or `RESUME SYNC ...` plan and reconciles only the target
  tree without another registry generation or audit event.
- A reconciliation-only failure performs no database write; inspect and open a
  fresh plan before retrying because remote state may have changed.
- A suspended guild cannot run its own resume command. Always retain a separate
  active control guild.

The current development database has only one active guild. Do not manufacture
or retain a second enrollment merely to exercise lifecycle mutation. The real
database gate proves a transition inside an outer rollback; live target
suspend/resume acceptance waits for a separately intended enrolled guild.

## Deployment of P10.8 itself

Adding the two nested lifecycle commands changes the existing development-
guild `operator` root. Stop only the guarded development beta for the
stopped-writer database gate, review the offline desired command tree, inspect
the empty global tree, and use the established application-command runbook to
apply only configured development guild `478571892832206869`. Restart the
durable beta with startup synchronization disabled and verify identity,
database authority, checkpoint, health, sole-writer status, and private
`/operator guild list` availability.

This is an owner-only control-plane release. It does not change the human
tester workflow and does not warrant a tester ping or `WHAT TO TEST` expansion.
