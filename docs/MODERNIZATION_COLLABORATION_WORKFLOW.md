# Modernization collaboration workflow

Last updated: 2026-07-31

Status: Active

This document defines how planning/oversight and implementation tasks share
the database/slash modernization program. It supplements
`DATABASE_AND_SLASH_MODERNIZATION.md`; that roadmap remains authoritative for
scope, phase status, decisions, evidence, and the next unit.

## Roles

### Sol planning and oversight task

- reconcile the accumulation branch, roadmap, taxonomy, and unresolved user
  decisions;
- select one bounded unit and write its executable prompt, exclusions, risks,
  validation, and acceptance criteria;
- remain read-only while Luna owns an implementation unit;
- review the completed unit at its integration gate using the risk tiers
  below;
- approve integration or return focused findings;
- keep cross-unit policy and compatibility decisions in the roadmap.

Sol does not need to review each intermediate commit. The normal review unit
is one bounded branch at its integration gate.

### Luna execution task

- verify the exact worktree, branch, base commit, and clean status named in
  the prompt before editing;
- implement only the selected unit;
- preserve unrelated changes and stop on an unexpected or dirty base;
- run the required focused, offline, gated-database, and beta validation;
- update the roadmap evidence for that unit;
- self-review the full branch diff;
- provide the handoff packet below without merging into the accumulation
  branch unless explicitly authorized.

## Worktree layout and ownership

The primary planning/integration checkout remains:

```text
/home/nelluk/PolyBot39-dev
```

The reusable Luna execution checkout is:

```text
/home/nelluk/PolyBot39-dev/.worktrees/luna
```

It is created detached at a clean accumulation checkpoint. At the start of a
unit, Luna creates a dedicated `codex/<unit-name>` branch in that worktree.
After integration, the worktree returns to a clean detached accumulation
checkpoint before being assigned another unit.

Rules:

- Never use or modify `/home/nelluk/PolyBot39`, the production checkout.
- Sol must not switch branches, edit files, stage, or commit in Luna's
  worktree.
- Luna must not switch branches, edit files, stage, or commit in the primary
  checkout.
- Git objects and refs are shared across worktrees. Branches are not; Git
  prevents one branch from being checked out in both worktrees.
- Runtime processes are host-wide, not worktree-local. Before launching or
  restarting beta, inspect all development `bot.py --skip_tasks` processes,
  compare start times and command paths, and leave exactly one intended beta.
- A worktree is isolation for files/index/HEAD, not authorization for
  production, database, Discord, dependency, push, merge, or service actions.

## Unit lifecycle

1. Sol verifies that the accumulation branch and its GitHub tracking branch
   are reconciled and clean.
2. Sol selects a bounded roadmap unit and records its risk tier.
3. Sol supplies Luna an exact base commit, worktree path, branch name, scope,
   exclusions, tests, beta requirements, and expected handoff.
4. Luna creates the unit branch in its worktree and implements it.
5. Luna validates, self-reviews, commits, updates roadmap evidence, and sends
   a handoff packet.
6. Sol reviews the whole branch at the appropriate depth.
7. Luna addresses focused findings and refreshes validation when needed.
8. Sol verifies the final commits/evidence, then integrates the unit into
   `codex/database-slash-modernization` when authorized.
9. Sol pushes the accumulation checkpoint when authorized and returns the
   Luna worktree to a clean detached copy of that checkpoint.

Do not stack a new unit on an unreviewed branch merely to keep Luna busy.
Adjacent low-risk units may share one beta session, but their commits,
evidence, and integration decisions remain separate.

## Risk-tiered review

### Tier 1 — lightweight integration review

Examples: documentation, tests, copy, isolated renderer polish with no
permission or database behavior change.

Review the branch summary, diff, relevant focused tests, roadmap evidence,
and `git diff --check` before integration.

### Tier 2 — full branch review

Examples: bounded read workers, Components v2 workspaces, command
registration, permission-sensitive visibility, prefix routing, or Discord
effects that do not mutate game/ELO state.

Review the complete diff, worker ownership, event-loop behavior, permissions,
component authorization/expiry, compatibility ledger, full offline suite,
gated database evidence when applicable, and beta evidence.

### Tier 3 — plan review and full integration review

Examples: ELO/game mutations, destructive actions, coordinator changes,
schema/data migrations, security/privacy features, multi-process
coordination, or production deployment.

Sol reviews the design before implementation and the complete final branch
before integration. Require fault injection, rollback, concurrency,
permission, post-commit ordering, operational rollback, and the exact
production or beta approval gates relevant to the unit.

## Luna handoff packet

Every execution handoff must include:

```text
Unit and risk tier:
Worktree:
Branch and exact base commit:
Final commits:
Files changed:
Behavior/interface changed:
Prefix compatibility:
Compatibility-ledger changes:
Database/transaction/concurrency boundaries:
Discord visibility and post-commit effects:
Focused tests and results:
Complete offline tests and results:
Gated development-database tests and safety identity:
Beta sync/smoke result and process status:
Known limitations:
Roadmap sections updated:
Current worktree status:
Recommended integration action:
```

## Prompt header for Luna

Every implementation prompt should begin with:

```text
Work only in /home/nelluk/PolyBot39-dev/.worktrees/luna.
Do not edit or operate on /home/nelluk/PolyBot39 or the primary planning
checkout /home/nelluk/PolyBot39-dev.

Read AGENTS.md, docs/DATABASE_AND_SLASH_MODERNIZATION.md, and any unit-specific
runbook in full. Verify that the worktree is clean and detached at EXPECTED_SHA.
Create and switch to BRANCH_NAME before editing. Stop if the base, branch,
worktree, or runtime state differs from the prompt.
```

The remainder of the prompt supplies the selected unit's objective, scope,
exclusions, tests, approvals, and handoff requirements.
