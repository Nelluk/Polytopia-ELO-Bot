# Development beta feedback runbook

WB1.1 adds structured `/staffhelp` intake for the development wider beta. The
command is registered as a top-level command with no slash options. It opens a
requester-bound modal containing:

- explicit destination/category: **Contact server staff** (`help`), **Report a
  PolyELO bug** (`bug`), or **Suggest a PolyELO improvement** (`feature`);
- a 160-character short summary;
- a 4,000-character detailed description;
- optional 1,000-character command/game/context text; and
- optional PNG, JPEG, WebP, GIF, PDF, Markdown, or plain-text attachments.

The authoritative JSONL store is development-only. P9.20 later retained this
development record-first backend and added a distinct production backend to the
same `/staffhelp` form. P9.30 makes the production destination explicit: local
help uses the related game's or invoking guild's active configured staff-help
channel and first Helper role, while PolyELO bug/improvement feedback uses one
bot-level maintainer channel without a role ping. Production never writes
JSONL or `GameLog`. That reviewed source design does not itself deploy or
configure production; production rollout remains separately approved. The
approved retirement of the legacy prefix is not reversed by either backend.

The installed discord.py 2.7.1 Components v2 API supports `RadioGroup` and
`FileUpload`; the native form accepts at most 10 files. Each file is limited
to 5 MiB and all files in one report to 20 MiB. Discord attachment bytes are
read through the attachment object only; the intake never fetches a
user-supplied URL.

## Authoritative store

The store is available only when the selected runtime profile is exactly
`development`. It is derived from the profile, not from a repository-tracked
application path:

```text
<development profile log_root>/beta-feedback/reports.jsonl
<development profile log_root>/beta-feedback/attachments/<report-id>/
```

With the supplied development profile, `<development profile log_root>` is
`logs/development`. The feedback directory and attachment directories are
mode `0700`; the JSONL and attachment files are mode `0600`. The runtime
directory is ignored by Git.

Each successful append is one deterministic UTF-8 JSON object followed by a
newline. Schema version 1 includes `report_id`, `category`, `summary`,
`details`, `context`, `game_id`, `command_reference`, `requester_id`,
sanitized `requester_display_name`, `guild_id`, `channel_id`, `source`,
`timestamp_utc`, `git_checkpoint`, and attachment metadata. Attachment
metadata includes the sanitized original filename, fixed storage filename,
content type, byte count, Discord attachment ID when available, and SHA-256
digest. Report IDs are generated independently of user content.

The append worker is a bounded single-worker executor backed by a process
lock. Attachment files are staged under the controlled `.staging` directory,
fsynced, and atomically published under the generated report ID before the
JSONL line is appended, flushed, and fsynced. A failure before publication
cleans the stage. A failure after publication never acknowledges the report;
an uncertain append leaves a reconciliation warning rather than deleting
possibly committed evidence. Cancellation drains the worker before returning
cancellation to the interaction. If cancellation arrives after a worker has
committed, the worker drain logs only the committed report ID for
reconciliation; no acknowledgement is sent.

The checkpoint is best-effort diagnostic information. The worker uses an
optional automatically supplied build value or a bounded `git rev-parse HEAD`
lookup. A container image normally has neither Git metadata nor a manual build
value, so the record explicitly contains `"git_checkpoint": "unknown"`.
Unknown version information never blocks startup or feedback submission.

## Native submission and Discord relay

The legacy `$staffhelp` and `$helpstaff` prefix adapters were intentionally
retired before integration with user approval. Native `/staffhelp` is the
replacement for this wider-beta intake and is the only feedback intake path
implemented by WB1.1. It is available to ordinary testers in guilds assigned
`tools_support`; the capability remains default-deny. This does not claim
universal production availability.

Native reports are acknowledged ephemerally with their report ID only after
the local record and attachments are committed. The structured private
staff-channel mirror is attempted afterward in `admin-spam`
(`480078679930830849`). A relay failure leaves the report in JSONL and
returns a private recorded-with-relay-warning acknowledgement; the warning is
also logged with report/guild/channel identifiers only. Report details are
never sent to the originating public channel. If Discord rejects the final
native followup after commit, the failure is logged with the report ID and the
store remains authoritative; the handler never sends a false “no report ID”
message. The WB1.2 public release channel is separate: `todo-and-changelog`
(`481779940124000256`) never receives staffhelp report details or a fallback
relay.

Capability policy remains default-deny. WB1.1 itself changed no development
assignment or Discord state; the later approved wider-beta setup assigned
`tools_support` in the development guild. P9.20 changes no command shape, so it
requires no additional command synchronization.

## Read-only operator commands

Run the utility with the development profile explicitly selected:

```bash
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py list
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py show --report-id REPORT_ID
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py search "search text"
POLYBOT_ENV=development .venv/bin/python scripts/manage_beta_feedback.py --json list
```

The utility performs no create, edit, delete, resolve, upload, or attachment
payload reads. `list` and `search` are bounded to 1,000 records. `show` and
machine-readable output include attachment metadata but never open binary
files. An absent JSONL file is reported cleanly. Malformed or non-newline-
terminated final lines are reported as ignored malformed/truncated lines and
are never presented as valid records.

Reports can contain sensitive user-provided text. WB1.1 has no automated
retention or redaction operation; operators must handle the development log
root as sensitive data and use an approved filesystem-level retention process
if required. The reader intentionally cannot delete or rewrite evidence.

## Release attribution and reporter notification

When an accepted report directly causes a beta correction, use the
authoritative JSONL record to connect the release to its reporter:

1. read the exact `report_id` through the read-only utility;
2. verify the report's guild, command/workflow, and accepted fix match the
   reviewed release;
3. use only the stored integer `requester_id` as the release manifest's
   `notify_user_ids` value;
4. add a smoke-test item that asks the reporter to recheck the corrected
   behavior; and
5. after delivery, record the report ID and release/message IDs in roadmap
   evidence without copying private report prose or the requester ID.

This is the default for direct report-driven fixes and allows the reporter to
see that their feedback was acted on. It is not a general-purpose mention
mechanism. Do not notify someone for a merely related change, infer identity
from a display name, reveal report contents publicly, or mention a requester
from another guild. Deduplicate multiple reports from one requester. The
release system permits at most five exact user IDs; if more reporters are
linked, stop and record a deliberate notification disposition rather than
silently dropping or inventing mentions.

The public announcement identifies the reviewed users only through Discord
mentions and describes the released behavior through its bounded summary and
smoke checklist. The private `admin-spam` mirror and authoritative JSONL
remain the places for report details.

## Repository-first triage

The default response to a new report is code and evidence investigation, not
immediate reporter questioning. Read the authoritative report, trace the
registered command through its service/worker/renderer and permission policy,
compare native and retained-prefix behavior, inspect safe development logs or
read-only state when useful, and add a focused reproduction before deciding
that human clarification is required.

Ask the reporter only when the remaining ambiguity is genuinely subjective,
client-only, or materially changes the product decision. Details that can be
inferred from the repository, roadmap, tests, logs, or captured report context
should be inferred by the reviewing model. Accepted related findings may be
combined into one bounded correction unit, but their report IDs and requester
attribution remain distinct for release evidence and notification.

## WB1.4 accepted interaction-lifecycle evidence

The following accepted report IDs drove the interaction-lifecycle correction
unit. This record intentionally stores IDs and bounded dispositions, not
private report prose or requester identities:

- `46AfEE3ffDPQ2BytH9GILxDe`: preserve a `/game record` draft after a
  confirm-time validation or database failure so the requester can retry.
- `X-XZcpIiULdM26a-Jon1ndQr`: make a staff-selected `/player register member`
  target visible in the modal and prove the primitive target reaches the
  worker request after submission.
- `2k-rbXRD8CmxQuxjLy1UJuGk`: record the accepted no-channel-dependent-
  visibility decision. Drafts and failures remain private; committed
  competitive/profile outcomes remain public in every allowed channel.
- Repeated beta log evidence of Discord 404/error 10008 `Unknown Message`
  during player-registration private-placeholder cleanup: treat an already
  cleared placeholder as benign, preserve one public success, and keep other
  cleanup failures observable without changing commit semantics.

The `/staffhelp` disposition remains unchanged: it has no slash invocation
arguments and opens its structured modal. No options were invented for this
unit. Rename report `GgWxVs31FyFa72V2dHOleM0j` was explicitly out of scope.

## Validation and boundaries

Focused offline validation is:

```bash
POLYBOT_ENV=development .venv/bin/python -m unittest \
  tests.test_beta_feedback tests.test_slash_taxonomy \
  tests.test_application_command_policy
```

The development database suite remains behind its existing unchanged gate
(`POLYBOT_ENV=development`, database `polytopia_dev`, role `polybot_dev`).
WB1.1 adds no table, migration, API endpoint, GitHub integration, dependency,
external token, production action, or command registration action.
