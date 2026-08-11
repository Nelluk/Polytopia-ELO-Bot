# Final database/slash modernization adversarial-review prompt

Use the following prompt for the final pre-candidate adversarial review. The
reviewer is expected to use the GitHub connector as its evidence source.

---

Act as a senior adversarial reviewer for the PolyBot39 database-access and
Discord slash-command modernization.

Repository and target:

- GitHub repository: `Nelluk/Polytopia-ELO-Bot`
- Review branch: `codex/database-slash-modernization`
- Production baseline for comparison: `master`
- Evidence source: the GitHub connector only

Start by resolving and reporting the exact 40-character GitHub HEAD of the
review branch, the exact `master` HEAD, and their merge base. Bind every review
claim to that observed review-branch checkpoint. Do not trust a checkpoint
copied into this prompt or infer current state from an older progress entry.
If the branch moves during review, stop and report that the evidence is no
longer bound to one immutable candidate.

This is a read-only review. Do not edit files, push commits, open or merge a
pull request, synchronize Discord commands, operate a service, connect to a
database, or recommend executing production changes as part of the review.
Do not inspect or make assumptions from Nelluk's production checkout. Review
only repository evidence available through GitHub.

## Required authority

Read the current sections of these files before drawing conclusions:

- `AGENTS.md`
- `docs/DATABASE_AND_SLASH_MODERNIZATION.md`
- `docs/MODERNIZATION_COLLABORATION_WORKFLOW.md`
- `docs/MODERNIZATION_PRE_PRODUCTION_REVIEW.md`
- `docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md`
- `docs/MODERNIZATION_PRODUCTION_CUTOVER.md`
- `docs/APPLICATION_COMMAND_DEPLOYMENT_RUNBOOK.md`
- `docs/DEVELOPMENT_BETA_OPERATIONS.md`
- `docs/DEVELOPMENT_BETA_FEEDBACK.md`
- `docs/PRIVACY_READINESS_CHECKLIST.md`
- `docs/PRIVACY_REQUEST_RUNBOOK.md`
- `PRIVACY.md`
- `SECURITY.md`

In the main roadmap, read the opening safety/architecture sections, current
execution pointer, current decisions, P9 production-readiness sections, P9.15
through P9.20, and the recent progress entries. Use targeted GitHub searches
for older history instead of treating the entire chronological log as current
authority. Historical statements are evidence of their checkpoint, not proof
of present behavior.

## Review objective

Determine whether all valid findings from the previous adversarial review are
actually resolved in the current code, tests, configuration templates, and
operations documentation, and independently look for new release-blocking or
high-risk defects introduced by the accumulated cross-unit design.

The current durable record says these previous findings are resolved:

- blockers B1–B3;
- high findings H1–H8;
- medium findings M1–M6; and
- low/documentation finding L1.

M7/R-002 is intentionally open: it is the later exact-HEAD release-candidate
evidence gate. Do not mark a prior finding resolved merely because the roadmap
says so. Trace each finding to implementation and regression evidence. If a
finding is only partially resolved, reopen it with a concrete failure mode.

## Required technical scrutiny

Inspect the accumulated branch versus `master` and focus on boundaries where
individually reasonable units can fail when combined:

1. Database and concurrency boundaries
   - blocking ORM or rendering work on the Discord event loop;
   - worker-local Peewee connection ownership and cleanup;
   - atomicity, rollback, lock/coordinator scope, races, and stale revalidation;
   - cancellation after a worker or external effect has started;
   - model instances or lazy ORM graphs escaping workers;
   - truthful pre-commit versus post-commit publication and reconciliation.

2. Discord behavior and command parity
   - slash/prefix permissions, guild/channel rules, messages, cards,
     announcements, roles, and ELO coordination;
   - public/private response routing after interaction deferral;
   - confirmation expiry, duplicate execution, retry guidance, and uncertain
     external-effect outcomes;
   - command taxonomy, capability visibility, default-deny behavior, and
     retained/retired prefix decisions.

3. Environment and operational separation
   - explicit development/production profile selection;
   - application, guild, database, API, background-task, and startup identity
     gates;
   - absence of startup or global Discord synchronization;
   - global-tree inspection refusal and guild-only apply confirmations;
   - development-beta single-writer, restart, checkpoint, and announcement
     ordering;
   - production backup, migration, canary, rollback, manual purge, and
     operator-restart safety boundaries.

4. Privacy and support routing
   - one public `/staffhelp` form with exact environment-selected behavior;
   - development JSONL record-first authority and fixed beta mirror;
   - production direct per-guild relay, first configured Helper-role mention,
     no production JSONL/database archive, and truthful delivery failures;
   - attachment bounds, mention control, sensitive logging, cancellation, and
     incomplete configuration behavior;
   - consistency among privacy policy, security policy, runbooks, capability
     policy, and production cutover gates.

5. Production readiness and evidence integrity
   - additive timezone schema compatibility and production migration tooling;
   - exact release/rollback checkpoint handling;
   - configuration examples versus real runtime expectations;
   - whether documentation tests validate meaningful operational invariants
     rather than merely matching stale prose;
   - whether any final-candidate claim depends on evidence from a different
     HEAD or on unperformed production work.

Inspect implementation and tests directly. Do not accept broad documentation
claims as substitutes for code evidence, and do not infer correctness solely
from test counts. Search for bypasses and alternate call paths, including
retained prefix handlers, scheduled tasks, error fallbacks, and cancellation
handlers.

## Known facts to classify accurately

- Production has not been deployed, migrated, restarted, or command-synced by
  this modernization thread. That is a separate approval boundary, not by
  itself a source defect.
- The normal development environment currently lacks the locked DuckDB
  package. The most recent complete offline discovery recorded 1,651 tests:
  1,577 passed, 71 intentionally skipped, and only three known environment
  failures involving DuckDB runtime import, dependency inventory, and
  reporting-export import. Verify that these are still accurately described;
  do not silently call unrelated failures the same limitation.
- Broad wider-beta acceptance is ongoing. Lack of exhaustive tester signoff is
  distinct from a reproducible implementation defect.
- Dynamic guild configuration/onboarding is a documented post-modernization
  backlog interest, not a release blocker unless current static configuration
  creates a concrete safety or correctness failure.
- Production `/staffhelp` activation still requires the release record to
  verify every allowlisted guild's configured staff-help channel and first
  Helper role. Distinguish that unperformed operational gate from defects in
  the source policy or dispatcher.

## Finding standard

Report only evidence-backed findings. For each finding provide:

- stable ID and severity: Blocker, High, Medium, or Low;
- exact file and line or symbol locations on the reviewed branch;
- the concrete triggering sequence and observable failure;
- why existing tests or gates do not prevent it;
- the smallest bounded correction;
- focused regression evidence that should be required; and
- whether it blocks M7/R-002, only blocks production execution, or is a
  non-blocking backlog improvement.

Distinguish clearly among:

- an implementation defect;
- missing or misleading test evidence;
- stale or contradictory current documentation;
- an intentionally unperformed production/configuration gate;
- a known development-environment limitation; and
- optional redesign or product preference.

Do not inflate severity based on hypothetical scale. This is a small Discord
bot, but database corruption, false mutation success, duplicated irreversible
effects, cross-environment access, unsafe production operations, privacy leaks,
and global command mutation remain serious regardless of user count.

When no issue exists in a reviewed area, say what code/test evidence supports
that conclusion. If GitHub connector coverage cannot establish a claim, mark
it unverified rather than guessing or asking for local production access.

## Required report structure

1. Exact reviewed branch HEAD, `master` HEAD, merge base, and whether the
   branch stayed stable during review.
2. Executive recommendation:
   - ready to begin M7/R-002;
   - ready after listed bounded corrections; or
   - not ready, with precise blockers.
3. Previous-finding matrix for B1–B3, H1–H8, M1–M6, and L1, each marked
   resolved, partially resolved, reopened, or unverified, with evidence.
4. New findings ordered by severity using the finding standard above.
5. Cross-cutting test/evidence gaps that do not duplicate a finding.
6. Known limitations and separately gated production operations, explicitly
   separated from defects.
7. Recommended next bounded units in priority order.
8. A direct answer to: "May this exact HEAD proceed to the M7/R-002 final
   release-candidate evidence gate?"

If there are no valid new findings, say so directly. Do not invent work merely
to avoid a clean review result.

---
