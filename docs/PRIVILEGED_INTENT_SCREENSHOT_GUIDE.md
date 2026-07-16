# Help PolyELO Gather Discord Intent Review Screenshots

Discord is reviewing PolyELO Bot's continued access to two features: receiving
server-member events and reading message content. We need a few screenshots
showing the related PolyELO features working inside a Discord server.

This guide is for a Discord server moderator or bot staff member helping gather
the raw screenshots. You do **not** need to edit, annotate, publish, or submit
anything to Discord. Send the original screenshots to the PolyELO owner, who
will package them for the application.

## Safety and privacy rules

- Use a test account and test channels whenever possible.
- Do not expose private conversations, credentials, tokens, email addresses, IP
  addresses, or unrelated member information.
- Crop out or cover unrelated messages and member lists if they appear.
- Keep the server name, channel name, PolyELO bot name, relevant test-account
  name, complete bot response, and Discord timestamp visible.
- Do not use a real phishing or malware link. Use the harmless test URL provided
  below.
- The anti-scam test deletes messages and applies a 15-minute timeout. Make sure
  the test account owner understands this. A moderator may remove the timeout
  after the screenshots are complete.
- The anti-scam test account must not have a staff, helper, moderator, or other
  PolyELO-exempt role.

If any test would disrupt a real game or member, stop and coordinate with the
PolyELO owner instead.

## What to send

Please gather the five screenshot sets below. A “set” may contain more than one
raw screenshot when two Discord views cannot fit legibly in one image.

Suggested filenames are included, but exact filenames are not important.

---

## 1. Member rejoin restores a private game channel

Suggested filename:

```text
members-1-rejoin-channel-restored.jpg
```

This demonstrates that PolyELO must receive a member-join event even when the
member does not run a bot command.

### Preparation

1. Use a registered test account that is assigned to an unfinished PolyELO game
   with a private game or team channel.
2. Confirm the test account can see the channel before leaving.
3. Have the test account leave the server and then rejoin using the same Discord
   account.
4. Wait for PolyELO to restore access to the unfinished-game channel.

### Screenshot to capture

Open the private game channel and capture PolyELO's automatic message similar
to:

> `@TestUser has been added back to this channel after rejoining the server.`

The screenshot should clearly show:

- the private channel name;
- the PolyELO bot name/avatar;
- the complete automatic message;
- the test account mention; and
- the Discord timestamp.

Do not include unrelated game discussion.

---

## 2. A team-role change synchronizes league roles

Suggested filenames:

```text
members-2a-role-sync-before.jpg
members-2b-role-sync-after.jpg
members-2c-role-sync-log.jpg
```

This demonstrates that PolyELO must receive member-role update events without
waiting for the affected member to run a command.

### Preparation

1. Use a test account in a server where PolyELO's league role synchronization
   is configured.
2. Open the test member's profile or server role editor and capture their
   relevant roles **before** the change.
3. Add one recognized PolyELO team role to the test member.
4. Wait for PolyELO to add or update the associated league, house, and tier
   roles.
5. Capture the test member's relevant roles **after** synchronization.
6. Open the configured PolyELO/bot log channel and locate the bot message
   reporting that the team role was added.

### Screenshots to capture

Please send:

- a clear **before** view of the relevant roles;
- a clear **after** view showing the team role and derived league/house/tier
  roles; and
- PolyELO's log-channel message reporting the team-role addition.

Keep Discord's role names and timestamps readable. The PolyELO owner will make
the final before/after composite.

---

## 3. The same harmless message appears in different channels

Suggested filenames:

```text
content-1a-channel-one.jpg
content-1b-channel-two.jpg
```

This begins the anti-scam demonstration. PolyELO detects a repeated URL-bearing
message from one user across three different channels within two minutes.

### Preparation

Create or select three temporary channels that PolyELO can view and moderate,
for example:

```text
#intent-test-one
#intent-test-two
#intent-test-three
```

From the same non-staff test account, use this exact harmless message:

```text
PolyELO privileged-intent test https://example.com/polyelo-intent-review-test
```

### Screenshots to capture

1. Post the message in `#intent-test-one` and capture it.
2. Post the identical message in `#intent-test-two` and capture it.
3. Confirm both channel names, the same test-account name, the complete message,
   and timestamps are visible.

Take these screenshots **before** posting the third copy. The third copy will
trigger PolyELO and the first two messages should be deleted.

---

## 4. PolyELO detects and reports the anti-scam pattern

Suggested filename:

```text
content-2-enforcement-log.jpg
```

### Trigger the test

Within two minutes of the first message, post the identical test message from
the same test account in `#intent-test-three`.

PolyELO should:

- delete the three tracked messages;
- apply a 15-minute timeout to the test account; and
- post an enforcement notice in the configured bot/moderation log channel.

### Screenshot to capture

Open the bot/moderation log channel and capture the complete PolyELO message
similar to:

> `Anti-scam: timed out @TestUser (...) for 15 minutes for cross-posting scam messages in #intent-test-three.`

The screenshot should show:

- the log-channel name;
- PolyELO's bot name/avatar;
- the complete enforcement notice;
- the test account and third test-channel reference; and
- the timestamp.

If the feature does not trigger, do not keep repeating the test in public
channels. Check that the account is not staff-exempt and ask the PolyELO owner
to help troubleshoot.

---

## 5. Discord records the timeout and the messages are gone

Suggested filenames:

```text
content-3a-discord-audit-log.jpg
content-3b-messages-removed.jpg
```

This independently shows that PolyELO performed the moderation action and
removed the test content.

### Screenshots to capture

First, open **Server Settings → Audit Log** and find the timeout/moderation
action. Expand it if Discord provides more detail. Capture:

- PolyELO as the actor;
- the test account as the target;
- the timeout/moderation action;
- the reason `Sending scam messages`; and
- the corresponding timestamp.

Second, show that the unique test messages were removed. The preferred option
is to search the server for:

```text
polyelo-intent-review-test
```

and capture the no-results view. If Discord search has not updated, capture the
three test channels after the messages have been deleted.

The PolyELO owner will combine the audit-log and deletion screenshots for the
submission.

---

## Cleanup

After all screenshots are saved:

1. Remove the test account's timeout if appropriate.
2. Restore or remove test league roles as appropriate.
3. Close or delete temporary test channels according to server policy.
4. Confirm no real user content was accidentally included in the screenshots.
5. Send the original-resolution images privately to the PolyELO owner.

Do not post the evidence publicly yourself unless the owner specifically asks
you to. The owner will redact anything necessary, combine related images, host
the final evidence, and place the links in Discord's review form.

## Quick delivery checklist

- [ ] Rejoin/channel-restoration bot message
- [ ] League roles before the team-role change
- [ ] League roles after automatic synchronization
- [ ] PolyELO team-role change log message
- [ ] First repeated test message in channel one
- [ ] Second repeated test message in channel two
- [ ] PolyELO anti-scam enforcement notice
- [ ] Discord audit-log timeout entry
- [ ] Search result or channel views showing the test messages were removed
- [ ] All images checked for unrelated or sensitive information
