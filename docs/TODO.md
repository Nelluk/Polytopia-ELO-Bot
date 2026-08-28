# Project TODO

This is the maintainer-owned backlog for concrete work that is worth retaining
but is not yet approved for implementation. A listed item records intent and
context; it does not authorize source changes, deployment, database writes,
Discord command synchronization, or other operational action.

Keep entries concise and evidence-based. Remove completed items after their
durable behavior and operating instructions are documented in the appropriate
current guide; Git history retains the completed planning record.

## Reliability

### Add bounded Discord Gateway disconnect recovery

Status: proposed; not implemented.

On 2026-08-28, the production process remained alive while Discord's regional
Gateway returned repeated HTTP 503 responses. `discord.py` continued trying to
resume the same session and regional endpoint while its exponential backoff
grew to waits of more than 14 minutes. Docker therefore saw a running process
and did not invoke the existing `restart: unless-stopped` policy. The in-band
`/operator restart` command could not be delivered through the disconnected
Gateway. A manual Compose restart discarded the stale connection state and
established a fresh Gateway session within four seconds.

Investigate a conservative application-level watchdog that:

- preserves normal `discord.py` reconnect and resume behavior during short
  interruptions;
- tracks continuous disconnection until `on_ready` or `on_resumed` confirms
  recovery;
- after a reviewed threshold, revalidates protected in-process activity,
  closes cleanly, and exits through the existing supervised-restart path so
  Compose starts the same image;
- uses a cooldown or circuit breaker to avoid restart loops during a prolonged
  Discord-wide outage;
- logs the disconnect duration, recovery reason, and outcome without tokens,
  session identifiers, or other sensitive data; and
- has focused tests for recovery, active-work deferral, cooldown behavior, and
  the existing one-writer boundary.

Decide the disconnect threshold, restart cooldown, active-work deferral policy,
and failed-recovery alerting before implementation. A Docker healthcheck alone
is insufficient because ordinary Compose does not restart an unhealthy process
that remains running.
