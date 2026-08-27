# Development beta feedback

`/staffhelp` is the native structured intake surface. It is a top-level command
with no slash options and opens a requester-bound modal for local staff help,
bug reports, or improvement suggestions. The legacy `$staffhelp` and
`$helpstaff` adapters are retired.

The development and production backends are deliberately different:

- Development commits a local JSONL report and attachments before attempting
  its private Discord mirror.
- Production relays local help to the configured guild staff route and bot
  feedback to the configured maintainer route. It writes neither JSONL nor
  `GameLog`.

The `tools_support` capability is default-deny. Changing its guild assignment
requires the explicit application-command workflow; startup never synchronizes
commands.

## Development store

The store is available only under the exact development runtime profile:

```text
<development log_root>/beta-feedback/reports.jsonl
<development log_root>/beta-feedback/attachments/<report-id>/
```

With the supplied development profile, the root is `logs/development`. The
feedback and attachment directories use mode `0700`; files use mode `0600`.
The complete runtime directory is ignored by Git.

Each successful append is one deterministic UTF-8 JSON object followed by a
newline. It includes the report ID, category, summary, details, context, game or
command reference, requester and Discord routing identifiers, timestamp,
best-effort Git checkpoint, and attachment metadata. Stored attachment metadata
includes a sanitized name, content type, byte count, Discord attachment ID, and
SHA-256 digest. A container without Git metadata records the checkpoint as
`unknown`; that never blocks intake.

The native form accepts at most ten PNG, JPEG, WebP, GIF, PDF, Markdown, or
plain-text attachments. Each file is limited to 5 MiB and the report total to
20 MiB. The intake reads Discord attachment objects only and never fetches a
user-supplied URL.

The append worker serializes writes with a process lock. It stages and fsyncs
attachments before atomically publishing them, then appends and fsyncs the
JSONL record. Failures before publication clean the stage. An uncertain or
post-commit failure preserves possible evidence and emits a reconciliation
warning rather than falsely acknowledging or deleting it. Cancellation drains
the worker before returning.

## Acknowledgement and relay

Development reports are acknowledged ephemerally with their report ID only
after local commit. The private staff-channel mirror is attempted afterward.
A relay failure leaves the JSONL record authoritative and returns a private
recorded-with-relay-warning acknowledgement. Report details are never sent to
the originating public channel or used as a fallback public release message.

If Discord rejects the final follow-up after commit, the failure is logged with
the report ID and the stored report remains authoritative. Operators must not
tell the requester that no report exists merely because a post-commit Discord
effect failed.

## Read-only operator utility

Select the development profile explicitly:

```bash
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py list
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py show --report-id REPORT_ID
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py search "search text"
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py --json list
```

The utility cannot create, edit, delete, resolve, upload, or read attachment
payloads. `list` and `search` are bounded to 1,000 records. `show` and JSON
output expose attachment metadata only. Missing stores are reported cleanly;
malformed or unterminated lines are never presented as valid reports.

Reports can contain sensitive user-provided text. There is no automated
retention or redaction operation. Treat the development log root as sensitive,
use an approved filesystem retention procedure when needed, and never copy
private report prose or requester identities into source, public notes, or
general operational records.

## Triage and validation

Investigate the repository and available evidence before asking a reporter for
more information. Trace the registered command through its service, worker,
renderer, permission policy, focused tests, and safe development logs or
read-only state. Ask only when the remaining ambiguity is subjective,
client-only, or materially changes the product decision.

Focused offline validation is:

```bash
POLYBOT_ENV=development .venv/bin/python -m unittest \
  tests.test_beta_feedback tests.test_slash_taxonomy \
  tests.test_application_command_policy
```

The development database suite remains behind its exact development database
and role gate. The feedback subsystem uses no database table, migration, API
endpoint, GitHub integration, external token, or startup command-registration
effect.

Completed rollout and accepted-report evidence is preserved at Git checkpoint
`a226ade9`; it is not part of the current operator runbook.
