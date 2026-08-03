# Development beta feedback runbook

WB1.1 adds structured `/staffhelp` intake for the development beta. The
command is registered as a top-level command with no slash options. It opens a
requester-bound modal containing:

- category: `help`, `bug`, or `feature`;
- a 160-character short summary;
- a 4,000-character detailed description;
- optional 1,000-character command/game/context text; and
- optional PNG, JPEG, WebP, GIF, PDF, Markdown, or plain-text attachments.

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
cancellation to the interaction.

The checkpoint uses a validated runtime/build value when supplied, then a
bounded `git rev-parse HEAD` lookup in the worker. If neither is available,
the record explicitly contains `"git_checkpoint": "unknown"`.

## Prefix compatibility and Discord relay

`$staffhelp` and `$helpstaff` retain their existing grammar, user cooldown,
public acknowledgement, staff-channel message, optional game card, and
legacy `GameLog` write. A valid prefix request now commits to the JSONL store
before the existing relay. Prefix attachment URLs remain in the legacy staff
message; the authoritative store contains the captured attachment bytes and
immutable metadata. A prefix storage failure does not send the success
acknowledgement or write the legacy `GameLog` entry.

Native reports are acknowledged ephemerally with their report ID only after
the local record and attachments are committed. The structured staff-channel
mirror is attempted afterward. A relay failure leaves the report in JSONL and
returns a private recorded-with-relay-warning acknowledgement; the warning is
also logged with report/guild/channel identifiers only. Report details are
never sent to the originating public channel.

The existing `tools_support` capability remains default-deny. No development
capability assignment was changed, and no command synchronization or bot
launch is part of WB1.1.

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
