# Development guild-configuration delegation

P10.9 adds an owner-controlled, development-only policy for delegating
ordinary same-guild configuration work to explicit Discord roles. It does not
delegate bot-wide operations, security settings, lifecycle, enrollment,
command deployment, global visibility, or production authority.

## Command flow

The configured owner runs `/operator guild delegation` inside the exact active
guild. The private workspace shows the current policy version and manager role
IDs. Its role selector replaces the complete role set; `@everyone`, deleted
roles, and Discord-managed roles are rejected. The owner separately chooses
whether managers may activate ordinary changes or may only prepare and
validate a draft.

No selection changes the database until **Review and apply** is confirmed with
the complete displayed value:

```text
DELEGATE <guild-id> <full-plan-digest>
```

The plan digest binds the guild, prior policy version, complete canonical role
set, and activation flag. Apply reloads the policy under the active guild's
registry lock, rechecks the exact current Discord role snapshot, replaces one
versioned policy row, and appends one protected `delegation_policy` audit event
without changing the active configuration revision or registry generation.
Revocation is the same replacement with no manager roles and activation off.

After a policy exists, an authorized role member runs `/guild edit` in that
same guild. This separate top-level root does not expose the administrator-
default `/operator` commands. The `guild` and `operator` roots are deployed
together by the existing default-deny `operator` command capability.

## Delegated boundary

The delegated editor exposes only:

- display name and command prefix;
- require/allow teams, uneven teams, and maximum team size;
- ordinary bot/newbie/challenge channels and game categories; and
- ranked, unranked, Steam, game-announcement destinations.

It never exposes or accepts helper/mod/user/inactive roles, private bot
channels, log/staff-help routes, global-leaderboard inclusion, command
capabilities, enrollment/lifecycle, delegation, or another guild. A draft
containing any owner-only change is invisible to managers until the owner
finishes or resets it. Complete-document comparison in the database worker
enforces this boundary even if a component payload is forged.

Every create, refresh, edit, discard, validate, and activation request freezes
the requester's current Discord role IDs and invocation guild. The worker then
reloads the current database policy in its own transaction. Removed roles,
revoked policies, cross-guild requests, stale drafts, and protected changes
fail closed. Activation additionally reloads the policy's separate activation
flag immediately before the existing live-reference validation and atomic
revision/audit commit.

## Additive development schema

The connection-free plan is:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_delegation.py plan
```

During an approved stopped-writer window, apply and verify only the exact
printed plan:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_delegation.py apply \
  --confirm 'P10.9 APPLY <exact-plan-digest>'

POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_guild_configuration_delegation.py verify
```

The migration creates only `guild_configuration_delegation`, validates the
existing P10 storage identity/schema, and has no destructive rollback. A
partial or drifted table is refused.

The permanent real-schema proof runs only while the guarded beta writer is
stopped:

```bash
POLYBOT_ENV=development \
POLYBOT_P10_9_DELEGATION_INTEGRATION=1 \
  .venv/bin/python -m unittest -v \
  tests.test_guild_configuration_delegation_database
```

It replaces one policy, verifies its protected audit, and rolls the outer
transaction back. No manager policy or audit fixture is retained.

## Deployment and acceptance

P10.9 changes the development command tree by adding `/guild edit` and
`/operator guild delegation`. Stop only the guarded beta, run the complete
database gate, inspect the connection-free command plan and empty global tree,
apply only configured development guild `478571892832206869`, then start the
clean pushed checkpoint with startup synchronization disabled.

Initial acceptance is owner-first: verify the policy panel opens and `/guild
edit` denies an unconfigured ordinary user. Do not grant a real role merely to
manufacture acceptance. A later intended policy can be exercised by one role
member, including the edit-only versus activation-enabled distinction. This
operator/security surface does not warrant a tester ping by itself.
