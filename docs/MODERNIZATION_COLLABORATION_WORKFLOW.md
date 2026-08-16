# Modernization collaboration workflow

Last updated: 2026-08-15

Status: Active

This document defines how planning/oversight and implementation tasks share
the database/slash modernization program. It supplements
`DATABASE_AND_SLASH_MODERNIZATION.md`; that roadmap remains authoritative for
scope, phase status, decisions, evidence, and the next unit.

## Roles

### Temporary single-thread omni workflow

When Nelluk explicitly selects a Sol omni thread to conserve quota, that
thread may own planning, implementation, review, integration, and development
deployment for one bounded unit end to end. It still uses an isolated Git
worktree and unit branch so the running beta's accumulation checkout remains
stable. The thread must self-review the complete diff at the normal risk tier,
keep implementation and evidence checkpoints distinct where useful, and pass
the same focused/offline/database/beta gates; the exception removes the Luna
handoff, not any safety or validation boundary.

Do not dispatch or actively monitor a Luna worker while this explicit mode is in
effect. Return to the ordinary Sol/Luna roles when Nelluk asks. This section is
process authority only for an explicit current-thread instruction; it is not
a standing license for future tasks to silently collapse review roles.

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

### Specialized subagent roles

Use subagents as a phased team rather than treating one agent as a replacement
remote implementation task:

- parallel read-only explorers independently map code/data behavior,
  operations, tests, or adversarial risks and return concise evidence;
- Sol resolves conflicts between those reports and writes one authoritative
  implementation contract;
- exactly one write-enabled Luna worker owns the isolated execution worktree,
  implementation, validation, self-review, commit, roadmap evidence, and
  handoff;
- after that worker is idle, parallel read-only reviewers inspect the committed
  branch for correctness and test gaps without changing tracked files; and
- Sol synthesizes review findings, performs the risk-tier integration review,
  and either accepts the branch or sends bounded corrections to the sole
  writer.

Do not create parallel writers for one branch. Do not delegate merely to keep
agents busy, repeat work already established by the repository, or push
coordination overhead onto a small sequential change. A single narrow worker
is sufficient when there are no useful independent discovery or review
streams.

### Model and effort gate

“Luna worker” means a Codex internal subagent explicitly pinned to
`gpt-5.6-luna`. The orchestration call and returned agent identity are the
audit evidence; prompt text that merely calls an inherited agent “Luna” is
not. Internal subagents are preferred because Sol can coordinate parallel
specialists, collect distilled results, and keep noisy exploration and test
output out of the planning context without creating user-owned sidebar tasks.

Before dispatch, Sol must verify that the current orchestration capability
offers the exact Luna model and an explicit supported reasoning effort. Choose
effort by subtask: medium for straightforward inventory, high for complex
tracing or adversarial review, and Max only when a demanding bounded
implementation demonstrably warrants it. If a task remains too ambiguous for
a narrow Luna assignment, decompose it further or keep the decision with Sol
instead of compensating only with higher effort.

Spawn with no inherited model selection; when the tool requires it, use
`fork_turns="none"` or a bounded turn fork and supply the relevant repository,
scope, safety, evidence, and return-contract context explicitly. Give a writer
the exact worktree/base/branch and full implementation contract; give an
explorer or reviewer only the bounded question and read-only constraints it
needs. Record returned agent IDs or canonical task names in unit evidence. Do
not let subagents recursively delegate unless Sol explicitly authorizes a
concrete second-level split.

A separate user-visible Codex task configured as Luna remains an allowed
fallback when internal model pinning is unavailable or when Nelluk explicitly
wants independently visible execution. Do not fork the Sol planning task for
that fallback because a fork may inherit the planning model. Create or reuse a
task whose Luna model and intended effort are selected at creation and visible
in its header. The inability to create that fallback does not block work when
the preferred internal model-pinned route is available.

If this gate fails after work begins, interrupt the task and inspect its Git
state. Uncommitted interrupted work is non-authoritative and should normally
be discarded rather than incorporated without a fresh compliant review.

### Planning-task model and context continuity

The primary planning/integration task remains the designated Sol oversight
task. If the app visibly changes it to Luna or another unapproved model or
effort, stop integration, deployment, and worker-dispatch actions until
Nelluk restores the intended setting.

After context compaction or a visible model correction, reread `AGENTS.md`,
this workflow, and the current-unit, decision, and progress sections of
`DATABASE_AND_SLASH_MODERNIZATION.md`; then verify the primary branch and
working tree before acting. Chat promises are not process authority. These
tracked files and their tests are the durable authority.

## Handoff-driven worker supervision

Sol verifies each subagent once at dispatch: explicit model/effort, bounded
role, read/write authority, and expected result. For the sole writer this also
includes the exact worktree/base/branch, clean starting state, and successful
worktree setup. The writer prompt must require the structured handoff below;
explorer and reviewer prompts require concise findings with file/symbol or test
evidence and an explicit no-finding result when green.

After that setup is confirmed, Sol may perform independent read-only oversight
work and then use the orchestration wait mechanism with a long bounded wait.
Do not repeatedly poll the worker, read incremental commentary, watch its
terminal, or narrate unchanged progress. Normal implementation duration and
silence are not blockers. Sol resumes review when:

- Luna sends the requested completion/blocker handoff;
- the user explicitly asks for a status check;
- the task reports a stopped/failed/approval-needed state; or
- an external operational deadline makes a one-time check necessary.

The implementation review input is the writer's final handoff, committed
branch, and independent reviewer summaries—not any agent's intermediate
reasoning. A correction prompt follows the same rule: send the sole writer the
bounded accepted findings, require a fresh handoff, then wait. This keeps Sol
focused on decomposition, decisions, synthesis, and integration rather than
duplicating delegated work.

## Repository-first beta feedback triage

Treat a beta report as evidence to investigate, not as a tier-one support
ticket whose reporter must diagnose the implementation. Before asking the
reporter for clarification, the oversight task should normally:

1. read the authoritative feedback record and identify the exact command,
   guild, channel, game, checkpoint, and requester context already captured;
2. trace the reported output or behavior through the registered adapter,
   shared service, worker, renderer, permission policy, and nearby tests;
3. compare native and retained-prefix behavior, roadmap/taxonomy decisions,
   and analogous commands to infer the intended parity boundary;
4. inspect available development logs and safe read-only state when they can
   distinguish a runtime, data, permission, or presentation cause; and
5. reproduce or encode the inferred behavior in a focused offline test where
   practical.

Ask the reporter only when a material ambiguity remains after that
investigation, the answer depends on a subjective product preference, or the
missing evidence exists only in their client. Do not ask for screenshots,
command spellings, permission facts, or expected behavior that the repository,
logs, stored report, or safe development-state inspection can establish.

Once accepted, group only genuinely related findings into a bounded correction
unit, preserve each authoritative report ID for release attribution, and use
the stored requester ID for the reviewed beta notification workflow. The
worker prompt receives the inferred behavior and acceptance criteria, not an
instruction to rediscover product policy from the reporter.

## Worktree layout and ownership

The primary planning/integration checkout remains:

```text
/home/nelluk/PolyBot39-dev
```

Internal subagents share the parent task's filesystem and do not imply a new
checkout. Read-only explorers inspect the primary checkout without switching
branches or editing files. The sole write-enabled worker receives the manually
prepared isolated Luna checkout:

```text
/home/nelluk/PolyBot39-dev/.worktrees/luna
```

A user-visible write-enabled fallback task may instead receive an app-managed
isolated worktree. Its supplied path is authoritative and must be named in the
prompt. Do not substitute the primary checkout or another worktree.

Post-implementation reviewers inspect the committed branch read-only through
Git or the idle execution checkout. They must not edit tracked files, switch
its branch, run concurrent write-producing validation, or overlap a correction
turn by the writer.

Whether manually prepared or app-managed, the execution checkout starts
detached at a clean accumulation checkpoint. At the start of a unit, Luna
creates a dedicated `codex/<unit-name>` branch in that checkout. After
integration, the manual Luna checkout returns to a clean detached accumulation
checkpoint before another assignment; an app-managed checkout is disposed of
by Codex according to its task lifecycle.

The worktree reuses only development-local resources:

- ignored `config.development.ini` and `server_settings_dev.py` symlinks point
  to the same files in the primary development clone;
- Python commands use the exact shared interpreter at
  `<primary-checkout>/.venv/bin/python` rather than creating, synchronizing,
  or symlinking another environment;
- production configuration and credentials are never linked into the
  worktree.

Before a worker runs tests or imports the runtime profile in a new worktree,
run the helper by an absolute path from the primary checkout, passing the
existing target worktree:

```bash
/absolute/path/to/primary-checkout/scripts/setup_development_worktree.sh "$PWD"
```

The command works on macOS and Linux. The helper derives the authoritative
primary checkout from the physical parent of its invoked script; `$PWD` is only
the target argument. It uses the same primary-relative `.venv/bin/python` on
either host and does not accept an environment override for that location.
The script is idempotent and fail-closed. It creates only the two documented
development-configuration symlinks, refuses to overwrite an existing path or
target the production checkout, verifies the shared interpreter, and runs the
read-only development-profile check. It does not install dependencies,
connect to PostgreSQL, launch the bot, or broaden any database/Discord gate.

Codex local environments may run this command automatically as their Linux or
macOS setup script. The repository workflow still includes the explicit
absolute-path command in worker prompts so it remains auditable when no app
environment is selected.
Do not use `.worktreeinclude` for these two files: that mechanism copies
ignored files into disposable managed worktrees and snapshots, while the
symlink setup keeps one authoritative development-only credential copy.

Rules:

- Never use or modify `/home/nelluk/PolyBot39`, the production checkout.
- Sol must not switch branches, edit files, stage, or commit in Luna's
  worktree.
- Luna must not switch branches, edit files, stage, or commit in the primary
  checkout.
- Git objects and refs are shared across worktrees. Branches are not; Git
  prevents one branch from being checked out in both worktrees.
- Runtime processes are host-wide, not worktree-local. Before launching or
  restarting beta, use a host-wide process view capable of seeing sibling
  Codex task/PTY sessions; an ordinary sandboxed `ps` result is not sufficient
  evidence that no beta is running. Inspect all development
  `bot.py --skip_tasks` processes, compare command paths, working directories,
  start times, and ancestry, identify production processes separately, and
  leave exactly one intended development beta. Never stop a production
  process while cleaning up a development duplicate. Command registration is
  separate: stop beta, run the offline desired-state plan, obtain explicit
  guild-scoped apply approval, then launch with startup synchronization
  disabled.
- A worktree is isolation for files/index/HEAD, not authorization for
  production, database, Discord, dependency, push, merge, or service actions.

### Presentation-transition policy

Components v2 is opt-in, not the default presentation for every slash read.
Require a concrete current usability benefit before adding interaction:
pagination/filtering, genuine multi-view consolidation, drafts/previews,
attachment authoring, or review/confirmation workflows are strong examples.
The initial output must preserve or improve the common legacy view; a future
hypothetical feature is not a reason to hide useful information behind extra
taps now. Prefer progressive enhancement: provide the complete proven embed
first, then add optional controls when a demonstrated need exists.

When a native experiment does not provide that benefit, remove the dormant
game-specific UI and keep both interfaces on the proven renderer over the same
immutable DTO/read service. A temporary classic split renderer is not the
default transition pattern. If a genuinely justified high-use/high-risk
transition ever needs one, it must share the DTO/service, never add a second
database query, mutation implementation, or permission path, and record its
user impact plus an explicit removal condition in the roadmap compatibility
ledger and handoff. A visual rejection is a correction signal, not beta
acceptance, and no integration claim should be made until the required review
passes.

## Unit lifecycle

1. Sol verifies that the accumulation branch and its GitHub tracking branch
   are reconciled and clean, selects one bounded unit, and records its risk
   tier.
2. When independent uncertainty exists, Sol dispatches two or more bounded
   read-only explorers in parallel and waits for all requested summaries.
3. Sol reconciles their evidence into one implementation contract with exact
   scope, exclusions, worktree/base/branch, tests, beta requirements, and
   handoff expectations.
4. Sol dispatches exactly one write-enabled Luna worker. The worker creates
   the unit branch in its isolated worktree and implements only the contract.
5. The writer validates, self-reviews, commits, updates roadmap evidence, and
   sends the structured handoff.
6. After the writer is idle, Sol dispatches independent read-only reviewers in
   parallel when the risk tier or diff complexity benefits from specialization.
7. Sol synthesizes the reviewer results and performs the complete applicable
   integration review.
8. The sole writer addresses accepted bounded findings and refreshes evidence
   when needed; reviewers never patch the branch.
9. Sol verifies final commits/evidence, then integrates into
   `codex/database-slash-modernization` when authorized.
10. Sol pushes the accumulation checkpoint when authorized and returns the
    Luna worktree to a clean detached copy of that checkpoint.

Do not stack a new unit on an unreviewed branch merely to keep Luna busy.
Adjacent low-risk units may share one beta session, but their commits,
evidence, and integration decisions remain separate.

## Post-beta-push handoff

After every beta code push/restart, command synchronization, or combined beta
release, Sol must proactively end the operator handoff with this section:

```text
### What can we do next?

Recommended: <one bounded unit and why it is ready>
Also ready: <one useful alternative, or "none">
Waiting on: <feedback, approval, validation, or "nothing">
```

Do not wait for Nelluk to ask what comes next. The recommendation must be
derived from the current roadmap, integrated checkpoint, open beta feedback,
and active worker state. Do not recommend stacking on an unreviewed unit or
imply that a separately gated database, Discord, production, dependency, or
deployment action is already authorized. If no safe unit is selectable, say
so explicitly and name the exact condition that will make one selectable.

This section belongs in the Sol/operator handoff, not automatically in the
public Discord release announcement. Public announcements retain the bounded
release summary and prominently labelled **WHAT TO TEST** checklist defined by
the beta-operations runbook.

For a tester-pinged release, Sol must verify that every announced workflow has
usable development data and permissions. Successful announcement delivery is
the terminal deployment action: no planned command sync, database operation,
or service restart follows it. Later documentation-only evidence is committed
without restarting an unchanged bot.

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

### Development-database validation cadence

Do not take the durable beta offline for a real-database suite after every
ordinary unit. When a unit extends an already validated worker/transaction
pattern without changing schema, ELO coordination, or database-writer
ownership, its focused rollback/fault-injection coverage and complete offline
suite are sufficient for integration. Record the deferred real-database case
and batch it into the next planned stopped-writer validation window.

A stopped-beta development-database run remains mandatory before integration
when a unit introduces a new transactional graph, changes ELO/coordinator
semantics, changes schema or fixtures, adds another database-writing process,
or exposes uncertainty that only PostgreSQL can resolve. Run the accumulated
gated suite before wider-beta release checkpoints and production cutover.
This cadence reduces tester downtime without weakening the single-writer
boundary.

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
Beta sync/testing result and process status:
Known limitations:
Roadmap sections updated:
Current worktree status:
Recommended integration action:
Post-beta `What can we do next?` recommendation (when applicable):
```

## Prompt header for the sole Luna writer

Every implementation prompt should begin with:

```text
Work only in /home/nelluk/PolyBot39-dev/.worktrees/luna.
Do not edit or operate on /home/nelluk/PolyBot39 or the primary planning
checkout /home/nelluk/PolyBot39-dev.

Read AGENTS.md, docs/DATABASE_AND_SLASH_MODERNIZATION.md, and any unit-specific
runbook in full. Verify that the worktree is clean and detached at EXPECTED_SHA.
Create and switch to BRANCH_NAME before editing. Stop if the base, branch,
worktree, or runtime state differs from the prompt.

Run `/absolute/path/to/primary-checkout/scripts/setup_development_worktree.sh "$PWD"`
before profile-dependent tests or imports. The helper path must be absolute and
must be in the primary checkout; stop if it refuses the worktree.

Run Python through `/absolute/path/to/primary-checkout/.venv/bin/python`; do not
install or synchronize dependencies unless separately approved.
```

The remainder of the prompt supplies the selected unit's objective, scope,
exclusions, tests, approvals, and handoff requirements. End every worker
prompt with an instruction to send the completed or blocked handoff directly
to the originating Sol task; do not rely on Sol polling for completion.
