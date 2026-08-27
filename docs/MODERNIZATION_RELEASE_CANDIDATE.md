# Modernization release-candidate evidence

Status: active M7/R-002 procedure; not production authorization

This procedure freezes one source checkpoint for final review and beta
testing. It does not access the production checkout, load ignored production
configuration, connect to `polytopia2`, deploy, synchronize commands, or send
an announcement.

## Candidate and evidence commits

The candidate commit contains all source, tests, runbooks, the validator, and
the tester-instruction draft. Complete validation runs against that clean
commit. A later evidence-only commit may contain its JSON record and roadmap
results; that does not redefine the candidate SHA.

Every gate inside the record repeats the same full candidate SHA. The
validator checks that the reviewed production rollback is its ancestor, every
adversarial resolution checkpoint is inside it, and cutover-critical files
match the recorded SHA-256 digests from the candidate tree. It also requires
the exact two production support routes and corrects the former runbook error
that confused development guild `478571892832206869` with production
PolyChampions guild `447883341463814144`.

Use only a manifest directly under `release-candidate-manifests/`:

```bash
.venv/bin/python scripts/manage_release_candidate.py \
  --manifest release-candidate-manifests/modernization-rc2.json inspect

.venv/bin/python scripts/manage_release_candidate.py \
  --manifest release-candidate-manifests/modernization-rc2.json validate

.venv/bin/python scripts/manage_release_candidate.py \
  --manifest release-candidate-manifests/modernization-rc2.json require-ready
```

`inspect` validates the bounded JSON schema without invoking Git. `validate`
also verifies commits, ancestry, evidence ancestry, and candidate-tree
digests. `require-ready` additionally returns nonzero unless all five gates
are `pass`:

- cutover-critical review;
- complete offline discovery;
- complete stopped-writer development PostgreSQL discovery; and
- the bounded human/live beta matrix; and
- separately approved redacted production-configuration verification.

Cutover review, the human beta matrix, and production configuration may not
pass with skipped required checks.

A known dependency limitation, pending human test, omitted gate, mismatched
SHA, stale digest, wrong production route, or unresolved review item blocks
readiness. Do not relabel it as a pass. The release record contains no token,
password, cookie, private key, database DSN, or production-config contents.

The current validator requires the original B1–B3, H1–H8, M1–M6, L1, and
N1–N2 findings plus the post-RC1 N3–N7 corrections. RC1 remains historical
evidence for its older candidate; it cannot certify a post-P11 source tree.

RC2 freezes source `8e79dc295c024340fd55f9678d507e6e214469b4`.
Cutover review, complete offline discovery, and stopped-writer development
PostgreSQL discovery pass. The bounded-beta matrix remains pending at four of
seven checks, and production configuration remains pending at zero of three;
therefore `require-ready` intentionally returns nonzero.

P9.27 subsequently corrected the container Beta Lab control boundary, so RC2
is historical for the current source even though its record remains valid for
`8e79dc2`. Do not carry RC2 gates forward to a successor candidate. P9.28 then
made all five protected Beta Lab packs ready at exact source `da7b204` without
command synchronization. Human command, retained-prefix, and public/private
visibility acceptance remain separate gates before a successor freeze.

The successor source also includes P12.1 player badges and P12.2 squad-profile
presentation. Its critical digest inventory therefore includes the separate
production player-badges module and CLI added at `0ccb002`. The schema tool is
no longer a source blocker, but its production verify/apply remains part of the
separately approved maintenance window. A successor manifest must be created
from the later exact candidate; RC2 cannot certify these additions.

RC3 freezes exact source `036bea1c8a2dffe52f7b73ac2f1760711257aae0`.
Cutover review, complete offline discovery, and the stopped-writer development
PostgreSQL gate pass. The exact beta is running that candidate and its approved
guild-only `league` update converged all 12 development roots while the global
tree remained empty. The bounded-beta gate remains pending at three of seven
until four human checks are recorded; production configuration remains pending
at zero of three. `require-ready` therefore correctly returns nonzero.

RC3 is now historical for the successor release. Post-RC3 checkpoint `eddc0cd`
fixes the one-badge negative overflow label and title-cases **Team & Squads**;
the exact development beta runs that checkpoint. Nelluk accepted badge and
squad presentation after bounded live use and provisionally accepted the
retained-prefix and public/private visibility checks without comprehensive
execution. The successor beta gate may record all seven acceptance items as
passed only if its evidence explicitly states this limited sampling and
residual production-discovery risk; it must not claim exhaustive testing.

The successor production plan preserves the complete live production guild
allowlist and all compatibility-ledger-retained prefix behavior. Native command
synchronization targets only Main `283436219780825088` and PolyChampions
`447883341463814144`: Main receives only `tools_support`; PolyChampions receives
`core_user`, `team`, `league`, `house`, `squad`, and `tools_support`. The
all-guild capability list is empty and no other guild is inspected or changed.
The live production PolyChampions staff-help channel
`1327320361200648213` is canonical. Product bug/improvement feedback routes to
the existing beta feedback destination, guild `478571892832206869`, channel
`480078679930830849`; the production embed already includes source server and
source channel metadata and disables mentions. The successor RC manifest must
bind these decisions to its later exact candidate SHA; RC3's record is not
rewritten to certify post-RC3 source or acceptance.

## Tester-message boundary

`release-candidate-manifests/tester-instructions-draft.md` was deliberately
short and unsent. It and the development-only beta release tooling were
retired after the modernization reached `master`; the candidate commits retain
their immutable historical copies.
