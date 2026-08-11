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
  --manifest release-candidate-manifests/modernization-rc1.json inspect

.venv/bin/python scripts/manage_release_candidate.py \
  --manifest release-candidate-manifests/modernization-rc1.json validate

.venv/bin/python scripts/manage_release_candidate.py \
  --manifest release-candidate-manifests/modernization-rc1.json require-ready
```

`inspect` validates the bounded JSON schema without invoking Git. `validate`
also verifies commits, ancestry, evidence ancestry, and candidate-tree
digests. `require-ready` additionally returns nonzero unless all four gates
are `pass`:

- cutover-critical review;
- complete offline discovery;
- complete stopped-writer development PostgreSQL discovery; and
- the bounded human/live beta matrix.

A known dependency limitation, pending human test, omitted gate, mismatched
SHA, stale digest, wrong production route, or unresolved review item blocks
readiness. Do not relabel it as a pass. The release record contains no token,
password, cookie, private key, database DSN, or production-config contents.

## Tester-message boundary

`release-candidate-manifests/tester-instructions-draft.md` is deliberately
short and unsent. After Nelluk approves its final wording, delivery uses the
existing development-only beta release tooling. Finish all planned downtime
and health checks first; successful delivery remains the terminal action.
