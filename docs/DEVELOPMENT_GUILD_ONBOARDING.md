# Development guild onboarding

This runbook covers P10.7 owner enrollment of a guild that is already visible
to the development bot. It applies only to the database-authority development
profile. It does not authorize production onboarding, schema work, or global
application-command synchronization.

## Quarantine behavior

An invitation creates no database row and grants no defaults. The bot retains
the Discord connection so the owner can inspect the target, but drops its
prefix commands and guild listener events. Application interactions receive a
private quarantine response. The join and ready lifecycle remain observable.

The configured development bootstrap guild IDs remain mandatory at startup.
The database may additionally contain owner-enrolled active guilds; startup
loads the complete active inventory and fails closed if any active guild or
configured Discord reference is unavailable.

## Enrollment flow

Run the command in an already active development guild, not in the quarantined
target:

```text
/operator guild enroll target_guild_id:<exact ID> template:Basic prefix server
```

The caller must be the configured owner. The target must be visible to the
same development bot, absent from every registry state, outside the known
production-guild denylist, and give the bot `View Channels`, `Send Messages`,
and `Read Message History` server permissions.

The private preview identifies the exact guild, observed bot permissions,
template, and full document digest. Confirm only by typing the displayed value
exactly:

```text
ENROLL <guild-id> <full-64-character-document-digest>
```

Commit rechecks owner authority, target identity, the complete current runtime
inventory, every current revision/generation/document digest, the target's
absence, and current Discord role/channel evidence under the shared
configuration advisory lock. One transaction then creates registry state,
immutable revision 1, generation 1, and audit event 1. Audit or revision
failure rolls back the entire graph.

After commit, a separate read-only connection reloads every active guild and
the event loop publishes the new immutable runtime snapshot only if all prior
guild evidence is unchanged and the new guild is exactly revision/generation
1. A committed-but-unpublished result is terminal: do not enroll again; use
`/operator bot restart` to reconcile the committed active graph.

## Basic prefix server template

The initial template uses prefix `$`, makes `@everyone` an ordinary full user
through the existing levels 1–3 role mapping, and deliberately grants no:

- helper, moderator, or advanced-matchmaking role;
- team requirement or team behavior;
- global-leaderboard participation;
- restricted/private bot-channel list or announcement/staff destination;
- application-command capability.

Enrollment never synchronizes Discord commands. Prefix behavior becomes
available from the published runtime snapshot. Configure ordinary settings
later through `/operator guild edit`. Application-command capability changes
and the exact guild-scoped plan/apply remain the separate P10.6c workflow.

## Verification and recovery

From an active guild, use `/operator guild list` to verify revision 1 and
generation 1. In the newly enrolled guild, verify a harmless `$guide` or
equivalent read before configuring destinations or roles. Do not expect slash
commands until a separately reviewed capability activation and guild-only
command apply have completed.

There is intentionally no unenroll, delete, or hard-reset command in P10.7.
Suspension, retirement, delegated administration, and production enrollment
remain separate work.
