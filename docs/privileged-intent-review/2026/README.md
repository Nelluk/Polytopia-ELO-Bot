# PolyELO Privileged Intent Review Evidence

These screenshots were staged in a Discord server with a test account and
harmless test content. They demonstrate PolyELO's uses of the Server Members
and Message Content privileged intents.

## Server Members Intent

### Member rejoin restores private game-channel access

[View screenshot](members-1-rejoin-channel-restored.jpg)

PolyELO automatically restored a previously registered test player's access to
an unfinished game's private team channel after the player rejoined the server.
The player did not invoke a bot command.

### Member-role update synchronizes league roles

- [Roles before the update](members-2a-role-sync-before.jpg)
- [Roles after automatic synchronization](members-2b-role-sync-after.jpg)
- [PolyELO role-update log](members-2c-role-sync-log.jpg)

The test member initially had only the unrelated `YTfan` role. A staff member
then added the recognized team role `The Overcast`. PolyELO responded to the
member-role update by adding the associated `Tempest`, `Bronze Player`, and
`League Member` roles and recorded the team-role event in the bot log channel.

## Message Content Intent

### Cross-channel messages within the detection window

- [Repeated message in `#intent-test-two`](content-1a-repeated-message-channel-two.jpg)
- [Repeated message in `#intent-test-three`](content-1b-repeated-message-channel-three.jpg)

The same non-staff test account posted an identical harmless URL-bearing
message in different channels during the two-minute detection window. A third
copy was then posted in `#intent-test-one`, triggering enforcement.

### Automatic anti-scam enforcement

[View PolyELO enforcement notice](content-2-antiscam-enforcement-log.jpg)

After the third cross-channel copy, PolyELO deleted the tracked messages,
timed out the test account for 15 minutes, and posted this enforcement notice.

### Discord audit record and deletion result

- [Unique test-message search with no results](content-3a-message-search-no-results.jpg)
- [Discord audit-log timeout record](content-3b-discord-audit-timeout.jpg)

Discord's audit log identifies PolyELO as the actor, the test account as the
target, the reason `Sending scam messages`, and the 15-minute timeout. A search
for the unique test phrase returned no results after PolyELO deleted the three
messages.

No real phishing content was used. The URL was an `example.com` test URL, and
the screenshots do not contain production credentials or private conversation
content.
