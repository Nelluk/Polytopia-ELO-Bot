# Security Policy

## Supported version

Security fixes are made against the current production branch of PolyELO Bot.
Older deployments and forks may not receive fixes.

## Reporting a vulnerability

Do not disclose vulnerabilities, exposed credentials, or personal data in a
public GitHub issue or Discord channel.

Use GitHub's
[private vulnerability-reporting form](https://github.com/Nelluk/Polytopia-ELO-Bot/security/advisories/new)
for reports involving the bot, its API, production data, authentication,
authorization, or credentials. Include:

- a concise description of the issue and its potential impact;
- affected components or versions;
- reproduction steps or a proof of concept that does not access other users'
  data; and
- any suggested mitigation.

For wider-beta testing when GitHub's private form is unavailable, invoke
`/staffhelp` with no options. In the modal, enter:

```text
Short summary: Private security report
Detailed description: Please contact me privately.
Optional context: A safe indication of the affected area, if useful
```

Do not include vulnerability details in that initial Discord request. The
native JSONL store is development-only and the wider-beta `/staffhelp` flow is
not a production-ready security intake. Before P9, the project must separately
approve a production-safe authoritative intake/retention path (or another
production relay design); until then, use the currently deployed private
support/moderator route for production communities. Staff will arrange a
private follow-up.

For an ordinary privacy access, correction, or deletion request, follow
[PRIVACY.md](PRIVACY.md) instead of the security-reporting process.

## What to expect

We aim to acknowledge a security report within seven days and provide an
initial assessment or status update within 14 days. Remediation time depends on
severity and complexity. Please allow a reasonable period for investigation
and remediation before public disclosure.

If a report identifies unauthorized access to Discord API data, the maintainers
will contain the issue, rotate affected credentials, assess the data involved,
and notify Discord and affected users when required.

## Scope and safe harbor

Good-faith testing must avoid service disruption, social engineering,
credential theft, persistence, destructive actions, and access to data that
does not belong to the reporter. Stop testing and report immediately if personal
data or credentials are encountered.
