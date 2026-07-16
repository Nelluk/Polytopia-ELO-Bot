# PolyELO Privacy Readiness Checklist

Complete this checklist before linking the privacy policy in Discord's
Developer Portal or making claims in a privileged-intent review.

## Code and data handling

- [ ] Deploy the explicit Discord intent configuration that requests Guild
  Members and Message Content but not Guild Presences.
- [ ] Deploy strict two-minute eviction for anti-scam message text and image
  fingerprints.
- [ ] Confirm operational logs record command names and metadata, not complete
  command text and arguments.
- [ ] Confirm `staffhelp` works in at least one documented support path for every
  production community, or ensure local moderators know how to relay a request.
- [ ] Test the manual privacy-request runbook against a non-production or test
  account without destroying shared competitive history.

## Infrastructure

- [ ] Confirm the live database, logs, and backups are encrypted at rest.
- [ ] Confirm remote administration uses restricted accounts and strong
  authentication.
- [ ] Confirm the bot's database account has only the privileges it needs.
- [ ] Confirm rolling backups expire no later than 30 days.
- [ ] Confirm restored backups have a process for reapplying completed privacy
  requests before normal service resumes.
- [ ] Identify the maintainers and tournament administrators allowed to access
  production data and remove access that is no longer needed.

## GitHub and Discord setup

- [ ] Commit `PRIVACY.md`, `SECURITY.md`, and the `docs/` policies to the public
  default branch.
- [ ] In GitHub repository **Settings > Advanced Security**, enable **Private
  vulnerability reporting**.
- [ ] Test that the repository's **Report a vulnerability** button opens a
  private report rather than a public issue.
- [ ] Add this URL as the application's Privacy Policy URL in Discord's
  Developer Portal:

  `https://github.com/Nelluk/Polytopia-ELO-Bot/blob/master/PRIVACY.md`

- [ ] Ensure the bot's user-facing profile or help material points users to the
  public repository or policy.
- [ ] Ensure staff know that public GitHub issues are for general questions only
  and must not contain personal data or vulnerability details.

## Ongoing review

- [ ] Assign a maintainer to check privacy and security requests regularly.
- [ ] Record the date and outcome of each request in a restricted minimal log.
- [ ] Review support/dispute records and Google Sheets at least annually and
  remove or anonymize data that is no longer needed.
- [ ] Review these documents whenever PolyELO adds a new data source, external
  service, privileged intent, or materially different feature.
