# PolyELO Data Retention Schedule

Effective: July 15, 2026

This schedule supports the public [Privacy Policy](../PRIVACY.md). It describes
the operating criteria used to retain and dispose of PolyELO data. When a
verified deletion request requires earlier disposal, the request takes priority
unless retention is legally required.

| Data category | Examples | Retention rule | Disposal method |
| --- | --- | --- | --- |
| Anti-scam working set | Recent message text, message and channel IDs, image fingerprints | In process memory for no more than two minutes | Automatic eviction; all remaining data disappears when the process exits |
| Game and rating records | Players, lineups, opponents, results, ELO changes, game dates | While PolyELO operates and the records remain necessary for ratings and competitive history | Delete direct identifiers or irreversibly anonymize them after a verified request; retain only de-identified history |
| League and team records | Teams, houses, squads, roles, bids, preferences, trophies | While the league feature operates or the record is needed for current or historical administration | Delete or anonymize when no longer needed or following an applicable verified request |
| Matchmaking records | Open games, notes, hosts, participants, channel and announcement IDs | Until the game expires, is deleted, or becomes part of persistent game history | Application cleanup, staff action, or anonymization with the associated historical game |
| Command-created content | Game names and notes, submitted identifiers, result information | As long as the resulting game, profile, or league record is needed | Delete or anonymize with the associated record |
| Staff-help and dispute records | Request text, attachment links, staff actions, relevant game information | While active and as needed for resolution, moderation accountability, or an associated game record; review at least annually | Delete unnecessary request content; anonymize retained game or enforcement history when applicable |
| Operational logs | Command name, user/channel/server metadata, warnings, exceptions | Bounded automatic file rotation: up to 10 backup files for general streams and up to 5 for API/ELO streams; no permanent log archive | Oldest files are overwritten automatically; remove individual data earlier when required and operationally feasible |
| Bullet tournament sheets | Discord usernames, bracket, house, opponent and result data in Google Sheets | While needed for tournament administration and history | Tournament administrator deletes or anonymizes rows after a verified request or when no longer needed |
| API credentials | Tokens for approved PolyELO API applications | Until revoked, replaced, the owner is deleted, or the integration ends | Revoke and securely remove the token and associated application record |
| Encrypted backups | Database and necessary operational data | Only for disaster recovery, until automatic expiry under the configured backup schedule | Automatic expiry; deletion requests are reapplied if restoration occurs before normal service resumes |

## Review responsibilities

- Maintainers review this schedule at least annually and when material features
  or service providers change.
- Community staff should avoid copying support or dispute content into new
  systems unless necessary.
- Google Sheets tournament administrators are responsible for applying relevant
  deletions or anonymization to the sheets they manage.
- Maintainers should verify that backup expiry, log rotation, and anti-scam
  eviction match this schedule before making related statements in an external
  review or questionnaire.

The manual procedure for verified requests is documented in
[PRIVACY_REQUEST_RUNBOOK.md](PRIVACY_REQUEST_RUNBOOK.md).
