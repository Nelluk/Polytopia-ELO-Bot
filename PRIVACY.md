# PolyELO Bot Privacy Policy

Effective: July 15, 2026

PolyELO Bot (also referred to as "PolyELO," "the bot," "we," or "us") provides
matchmaking, ELO ratings and leaderboards, match history, league and tournament
administration, game-channel automation, and community safety features for
Polytopia Discord communities.

This policy explains what data PolyELO processes, why it is processed, how long
it is kept, and how Discord users can request access, correction, or deletion.

## Data we process

Depending on the features a user or server uses, PolyELO may process:

- Discord identifiers and profile data, including user IDs, usernames,
  server-specific nicknames, server IDs, role membership, and moderation or
  inactivity status.
- Polytopia and gaming profile data voluntarily supplied to the bot, including
  Polytopia names and IDs, Steam names, and time-zone preferences.
- Game, matchmaking, ranking, league, and tournament data, including teams,
  houses, squads, signups, opponents, results, ratings, game names and notes,
  timestamps, and league preferences.
- Content and attachments intentionally submitted through bot commands, such as
  game notes, result reports, screenshots, and staff-help requests.
- Messages and image attachments visible to the bot when needed for automatic
  cross-channel scam detection. For this purpose, the bot temporarily compares
  recent message text and image fingerprints. It does not use this content for
  advertising, user profiling, or AI or machine-learning training.
- Operational and security information, including command names, Discord and
  channel IDs, timestamps, error details, and actions taken by the bot.

PolyELO does not use or store users' online/offline presence, activities, or
client-platform status.

## How we use data

We use this data only to:

- register players and maintain ELO ratings, rankings, and match history;
- create and administer games, leagues, tournaments, teams, and private game
  channels;
- synchronize bot records with relevant Discord membership and role changes;
- respond to support requests and resolve game or moderation disputes;
- detect and respond to cross-channel scams or spam;
- secure, operate, troubleshoot, and improve the bot; and
- comply with applicable law and Discord's platform requirements.

We do not sell personal data, use it for advertising, disclose it to data
brokers, or use message content to train AI or machine-learning models.

## Storage and service providers

The primary bot database and operational logs are maintained on restricted
production infrastructure. Production data and backups are encrypted at rest,
and data is encrypted in transit where it crosses a network. Access is limited
to maintainers and community staff who need it for bot operations, support,
league administration, or moderation.

PolyELO uses the following services where necessary:

- Discord supplies the messages, interactions, member events, and other API
  data needed to operate the bot.
- Google Sheets and Google Drive are used for certain PolyChampions bullet
  tournament signups and brackets. Tournament usernames, house affiliations,
  and results may be stored there for tournament administration.
- GitHub hosts the bot's public source code, policies, issue tracker, and private
  vulnerability-reporting workflow. Information a user voluntarily posts in a
  public GitHub issue is public, so personal data requests should not be filed
  as public issues.

We do not share Discord API data with unrelated third parties. Service
providers receive only the data needed to perform the applicable function.

## Message-content handling

Ordinary messages inspected by the anti-scam feature are held only in process
memory for the two-minute detection window. The corresponding message text,
attachments, and image fingerprints are then discarded and are not written to
the PolyELO database. If the bot detects a scam pattern, it may retain limited
enforcement metadata, such as the user, server, timestamp, and action taken.

Content a user intentionally submits as part of a command or support request
may be retained as part of the resulting game, league, tournament, moderation,
or support record.

## Retention

We retain data only while it is needed for the purposes described above:

- Anti-scam message text and image fingerprints are retained in memory for no
  more than two minutes.
- Operational logs use bounded automatic rotation; older files are overwritten
  instead of being maintained as a permanent archive.
- Support and dispute records are retained while the matter is active and as
  needed for resolution or an associated game or moderation record.
- Persistent game, rating, and league records are retained while PolyELO
  operates because later ratings and historical league results depend on them.
  Direct identifiers can be deleted or irreversibly anonymized on a verified
  request while preserving de-identified competitive history.
- Tournament records in Google Sheets are retained while needed for tournament
  administration and history and are deleted or anonymized when no longer
  needed or following an applicable verified request.
- Encrypted rolling backups are retained only for disaster recovery and expire
  according to the configured backup schedule. A deletion may remain in an
  inaccessible backup until that backup expires. If a backup is restored,
  completed deletion requests will be reapplied before normal service resumes.

The detailed operational schedule is available in
[docs/DATA_RETENTION.md](docs/DATA_RETENTION.md).

## Access, correction, and deletion requests

Requests are handled manually; users do not need a special privacy command or
external email account.

Invoke `/staffhelp` with no options. In the modal, enter the suggested request
across the appropriate fields:

```text
Short summary: Privacy request
Detailed description: Please contact me about my PolyELO data.
Optional context: Any relevant account or request context
```

In production, the bot relays the request directly to that server's configured
staff-only channel and pings its configured Helper role; it does not keep a
local JSONL copy. The development beta additionally writes its restricted
JSONL feedback record before mirroring it to beta staff. Staff may move the
discussion to another private Discord conversation. Do not include credentials
or unrelated sensitive information in the request.

We will verify a request using the affected Discord account. We will not ask
for a password, token, government identification, or payment. We aim to
acknowledge requests within seven days and complete applicable requests within
30 days. We may retain de-identified competitive records that can no longer be
linked to the requester.

General questions about this policy may be posted to the
[GitHub issue tracker](https://github.com/Nelluk/Polytopia-ELO-Bot/issues), but
users must not include Discord IDs or other personal information in a public
issue. Security vulnerabilities should be reported according to
[SECURITY.md](SECURITY.md).

## Children

PolyELO is not directed to children under 13 or anyone below the minimum age
required to use Discord in their jurisdiction. If we learn that prohibited data
was collected, we will delete it.

## Changes to this policy

We may update this policy as the bot's features or legal obligations change.
Material changes will be published in this repository with a new effective
date. Continued operation of the bot remains subject to Discord's Developer
Terms and Developer Policy.
