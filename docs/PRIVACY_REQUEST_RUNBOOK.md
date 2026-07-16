# Privacy Request Runbook

This runbook is for PolyELO maintainers and authorized community staff handling
access, correction, deletion, or anonymization requests. The process is manual;
low request volume does not require a dedicated command or deletion script.

## 1. Receive and acknowledge

Requests normally arrive through `$staffhelp` (or the server's configured
prefix) with a short message such as:

```text
Privacy request - please contact me about my PolyELO data
```

Move any detailed discussion to an appropriate private Discord conversation.
Do not ask the user to post personal information in a public GitHub issue or
Discord channel. Acknowledge the request within seven days and record the date,
request type, responsible maintainer, and target completion date in a restricted
maintainer record.

## 2. Verify the requester

- Verify control of the affected Discord account using the account that invoked
  `staffhelp` or a follow-up action from that account.
- Record the Discord user ID internally so the correct records can be located.
- Do not request a Discord password, bot token, government identification,
  payment, or unrelated personal information.
- If an authorized server administrator requests server-wide data action,
  verify their Discord permissions and define the exact server scope.

## 3. Identify the data

Check each applicable location:

- PostgreSQL `DiscordMember` and per-server `Player` records, including Discord,
  Polytopia, Steam, nickname, rating, team, trophy, time-zone, ban, and invite
  fields.
- Games, lineups, matchmaking entries, teams, squads, houses, auction bids, and
  house preferences linked to those player records.
- `GameLog` entries containing the user's Discord ID, current or prior names,
  support requests, or staff actions.
- API application records and tokens owned by the user.
- PolyChampions bullet tournament Google Sheets.
- Active operational logs. Rotated logs and encrypted backups are handled under
  the retention schedule.

For an access request, provide a concise, understandable summary instead of a
raw database dump containing internal data or information about other users.

## 4. Choose deletion or anonymization

If the user has no associated games, the owner-only `delete_player` command may
remove the player record after the maintainer confirms that it covers the full
request scope.

If competitive history depends on the record, irreversibly anonymize it rather
than deleting shared game history:

- replace Discord IDs with a unique random internal surrogate that cannot be
  used to recover or mention the former Discord account;
- replace usernames, nicknames, Polytopia names and IDs, Steam names, and other
  profile fields with a neutral deleted-user label or null value;
- clear time-zone, invitation, preference, token, and other user-specific data
  that is not necessary for de-identified historical results;
- revoke and remove API credentials owned by the user;
- remove or redact direct identifiers from `GameLog` and support records;
- remove or anonymize the user's rows in tournament Google Sheets; and
- preserve only the minimum de-identified rating and result relationships needed
  to keep other players' shared match history accurate.

Do not run ad-hoc destructive database commands without an encrypted backup and
a reviewed plan. Perform related database changes atomically where possible.

## 5. Verify completion

- Search the primary database for the old Discord ID and known stored names.
- Check relevant support records, Google Sheets, and active logs.
- Confirm that the anonymized record cannot generate a Discord mention or be
  mapped back to the requester using retained PolyELO data.
- Record what was deleted, corrected, anonymized, or retained and why.
- Have a second maintainer review requests involving shared game history when
  practical.

## 6. Respond and handle backups

Tell the requester when the action is complete and describe any de-identified
competitive history that was retained. Aim to complete applicable requests
within 30 days.

Deleted data may remain in inaccessible encrypted rolling backups until those
backups expire under the configured backup schedule. Do not restore deleted
data for ordinary use. If disaster recovery requires restoration, reapply
completed privacy requests before returning the service to normal operation.

## 7. Escalate incidents

If the request reveals unauthorized access, accidental disclosure, or receipt
of data the bot should not have collected, preserve only necessary evidence,
contain the issue, rotate affected credentials, and follow [SECURITY.md](../SECURITY.md).
