# 🧪 WHAT TO TEST

This is the running wider-beta checklist—not just the newest release. Please use the native slash commands and interactive controls below on desktop or mobile. Report confusing behavior, missing information, permission problems, stale cards, and unexpected private/public responses with `/staffhelp`.

## Games

- `/game record`, `/game open`, `/game join`, `/game leave`, `/game start`, `/game show`, `/game search`, and `/game players`
- After confirming `/game open`, its public warning/completion messages should
  appear as standalone channel messages—not as replies to a private or deleted
  draft interaction—and the join reaction should still be added.
- Interactive open-game card: join, leave, refresh, start, and delete controls
- Interactive game card actions appropriate to the game state
- `/game win`: use owned ready game 149 for an ordinary claim, then confirm it
  through the opponent flow. Check the pending/confirmed wording, game card,
  channel notices, announcements, and any ELO/Nova/experience/champion effects;
  a publication warning after a committed result must say not to retry.
- `/game result confirm`: use owned unconfirmed game 150 as staff and verify the
  same committed result/card/effects appear without duplicated audit or role
  work. Compare retained `$confirm` behavior where applicable.
- `/game result undo`: use owned completed game 151, verify its result and ELO
  reset once, and check the public reset notice plus experience/champion role
  reconciliation. Compare retained `$unwin`; an after-commit warning must not
  invite a duplicate undo.
- Compare `/game win` with retained `$win` for the same permission, pending vs.
  confirmed result, channel-routing, card, announcement, and role behavior.
- Staff corrections other than result confirm/undo remain on the general
  checklist; rank and unstart publication snapshots are not part of this
  release.
- `/game map`, `/game name`, `/game notes`, `/game side`, `/game ranked`, and bulk `/game tribe`
- `/game manage kick`, `/game manage extend`, `/game manage unstart`, and `/game manage delete`
- The former beta-only direct paths `/game unwin`, `/game confirm`,
  `/game delete`, `/game unconfirmed`, `/game set-ranked`, `/game extend`, and
  `/game unstart` should no longer appear. Use `/game search` with
  `view:Unconfirmed results` for the staff result queue. After this command-
  tree update, fully restart Discord if a client still shows stale paths or
  leaves a newly nested command at **Sending command...**.
- `/game ping`: try inferred and explicit games, Compose/Edit/Cancel, reopening
  after dismissing a modal, long multi-section text, multiple attachments, and
  Confirm. Verify recipients see the actor (and any on-behalf-of target), role
  or `@everyone` text does not ping, delivery is not duplicated, and any
  partial fanout failure gives a public terminal reconciliation warning.
- `/game logs`: as a participant, open a game you played; as staff, try the
  no-ID server view; use Search, Clear search, scope changes when offered,
  Previous/Next, and page jump. Successful results should be public without
  pinging names from old log text; permission/lookup/control failures should
  be private, and another user must not be able to control your workspace.
- Retained `$ping` and `$pingall` should still use the shared notification
  behavior; the retired platform-only `pingmobile`/`pingsteam` aliases are not
  part of beta testing.
- Check that successful changes are public and identify who made them; failures should remain private

## Players and leaderboards

- `/player show`, including profile sections and navigation. Open Analytics
  for yourself and another player, switch between current-reset and all-time
  ELO history, confirm the graph works on desktop/mobile, and compare the
  requester-versus-player local ranked 1v1 record. Revisit both eras to check
  cached controls remain responsive without replacing the public workspace.
- `/player register` and `/player timezone` read/edit/clear behavior
- `/leaderboard players`: pagination, common filters, advanced filters, active/all toggle, jump-to-page, desktop/mobile layout
- `/leaderboard teams`: active/all-tier default, tier and archived-team filters, pagination/page jump, current-page ELO graph, desktop/mobile layout, and sensible behavior for teams with missing or empty Discord roles
- `/leaderboard roles`: Free Agents default for ordinary users; staff/House
  Leader multi-role selection; All/Any matching; global/local ELO and W-L;
  total/recent-game sorting; inactive-member handling; pagination/page jump;
  desktop/mobile layout
- `/leaderboard activity` and `/leaderboard squads`
- Check local/global, current/peak, current-era/all-time, and active/all combinations for plausible ELO and W-L records

## Teams

- `/team create`
- `/team emoji`, `/team image`, `/team name`, `/team server`, `/team tier`, and `/team house`: read, edit, clear where offered, requester-team inference, and public actor attribution
- Team permission checks using `@testers` (staff) and `@Mod` roles
- `/team show`: explicit and inferred-team lookup, dense roster/ELO card, graph and image display, recent/all-completed activity toggle, missing-role warning, and desktop/mobile layout

## Houses

- `/house list`: browse pages, select a House, return to the list, and verify
  the controls work on desktop and mobile without reloading or replacing the
  public workspace.
- `/house show`: try an explicit House and omit the option when you have one
  unambiguous House role. Check leadership, active/archived Teams, tiers,
  current Team ELO, rosters, player ELO, and the House image.
- `/house name`: omit `name` for a public read; as a Mod, supply a replacement
  and verify the public result identifies the actor. Confirm the bot warns
  that the exact Discord House role must be renamed separately.
- `/house image`: omit image/clear for a public read, replace the image with a
  PNG/JPEG/WebP attachment, and explicitly clear it. Check the image on both
  desktop and mobile and verify committed changes publicly identify the actor.
- `/house create`: as a Mod, create only a development House that should be
  retained. Confirm the public result identifies the actor, stored name, and
  House ID and explains that its exact Discord role is a separate staff step.
  Duplicate, invalid, non-Mod, and wrong-channel attempts should fail privately
  without creating a House or audit record.
- The retired `$house_rename` and `$house_image` aliases should no longer be
  available. The retired `$house_add` command should no longer be available.
- Another user must not be able to operate your list/detail controls. Missing
  exact House or Team roles should produce useful warnings, while lookup,
  inference, permission, and expired-control failures should stay private.
- Compare retained `$house HOUSE`, `$houses`, and `$balance` output for the
  same configured Houses. Report meaningful information missing from either
  the native cards or legacy text.

## League

- `/league guide`: verify the quick start is public and points to the current
  player-registration, Novas, game-search/open/start/show workflows.
- `/league mark-active`: remove your own Inactive role. House Leaders,
  Co-Leaders, and Mods should also be able to target another member; ordinary
  users must be denied privately when targeting someone else. Successful role
  changes should be public and identify actor/target.
- `/league join-novas`: try an eligible registered user with no Team role.
  Success should add The Novas, remove Newbie when present, and publish an
  attributed confirmation. Unregistered users and existing Team members
  should fail privately without changing roles.
- `/league season`: omit `season` for all records, select a normal season, and
  try Season 1 or 2 for the historical result. Verify tier headings,
  regular/postseason W-L-incomplete counts, public output, and paging/page
  jump when enough teams are present.
- `/league free-agents post`: as a Mod, try the default channel and an explicit
  disposable test channel. Verify the private modal/preview/Edit/Cancel flow,
  then Confirm and check the public actor attribution plus all three reactions:
  `🔆`, `⏯`, and `❎`. A second post must refuse privately and link the live
  announcement. An eligible Nova Grad should receive/remove Free Agent when
  adding/removing `🔆`; ineligible or closed signups should be rejected. As a
  Mod, close and reopen with `⏯`, then conclude the disposable post with `❎`
  and its confirmation. A non-Mod control attempt must change nothing.
- `/league roster promote`: as a Helper or Mod, choose a player and destination
  Team. Verify the player's profile image and stored Team image appear, custom
  headline/footer text works, and the public card identifies who generated it.
  Retry with one direct HTTP(S) image URL override. A non-Helper must be denied
  privately, and an invalid/non-image URL must not publish a card.
- `/league roster trade`: choose two members, verify both profile images and
  the trade arrows, then try optional text and one raw URL override. Compare a
  retained `$promote` or `$trade` using a Team/member/raw-URL mix; both prefix
  commands should now require Helper access.
- `/league roster draft`: as a Drafter, Helper, or Mod, choose a registered
  member and an exact Team that has both a matching Discord role and stored
  image. Verify the legacy dense draft-card design, player avatar, Team image,
  Team-role color, local/global ELO and W-L, and House “selects” heading when
  the exact House role exists. Success should be public and identify the
  actor/player/Team; missing registration/image/role and unauthorized use
  should fail privately. The retired `$draft` command should not appear.
- `/league roster price`: choose a registered player with recent league games,
  first omitting `season` and then selecting an explicit ending season. Verify
  the public no-ping result shows the final price, chosen/inferred ending
  season, three season tier/W-L/game inputs, and whether the House Leader or
  House Co-Leader adjustment applied. A player with no qualifying three-season
  history should fail privately. The retired `$tradeprice` and `$playerprice`
  commands should not appear.
- `/league maintenance export`: as Helper/staff or above, run once with
  `include_logs:false` and once with `include_logs:true`. Each invocation
  should remain private and attach a readable gzip CSV covering the same
  confirmed ranked 2v2/3v3 league games; the logged version should add a
  `logs` column. A non-staff attempt must fail privately. Retained
  `$league_export` and `$league_export logs` should use the same data and
  attachment format, although their prefix completion remains public.
- `/league maintenance mark-inactive`: as a Mod, open the private preview and
  inspect its candidate/exclusion counts, missing protected-role warnings,
  paging, and Cancel first. A Helper must be denied privately, and the retired
  `$deactivate_players`/`$deactivate` names should not run. Use **Confirm only
  after verifying every displayed candidate is safe to mark in the beta
  guild**. Confirmation must refresh the plan before applying at most 100
  roles, continue through individual failures, and post one public no-ping
  actor-attributed aggregate; member-level failure details stay private.
  `$kick_inactive` is retired and should no longer run.
- `/league maintenance kick-inactive`: as a Mod, open the private preview and
  inspect every eligible/protected reason, paging, the 25-member run/deferred
  counts, and **Cancel**. Unknown, managed, Helper/Mod, leadership, bot, and
  owner roles/accounts must be protected; current Team and starter roles may
  remain eligible only when the 7/30/60-day and pending/incomplete-game rules
  also pass. Helpers must be denied privately and `$kick_inactive` must no
  longer run. **Do not submit the typed `KICK <count>` confirmation unless
  Nelluk has designated every listed account as disposable for this test.**
- Retained `$tutorial`, `$imalive`, `$novas`, `$joinnovas`, `$season`,
  `$jrseason`, `$ps`, `$js`, `$seasonjr`, and `$newfreeagent` should continue
  to reach the same underlying behavior.

- `/league tokens` with no options: verify the public workspace lists all
  configured House balances, Recent changes opens the audit history, paging
  works, and selecting a House opens only that House's balance/history.
- `/league tokens house:HOUSE` should open the same House-specific view
  directly. Another user must not be able to operate your controls; expired,
  invalid, and ambiguous selections should fail privately.
- As a Helper/staff-level tester, use `amount` with an optional `note` on a
  development House and then restore its original balance. Both committed
  updates should be public, identify the actor, appear newest-first in history,
  and never duplicate after a publication error. Non-staff may read but must
  not update.
- The retired `$tokens` command should no longer be available. `/league
  tokens` deliberately remains usable outside designated bot channels in the
  configured league/test guild to preserve its prior access behavior.

## Squads

- `/squad show` with no ID should return **No eligible squads** promptly on
  accounts that have no eligible squads rather than leaving the interaction
  pending.
- On a public `/squad show` workspace with eligible results, use the member
  selector with one to three registered members. Repeat from a later result
  page when available: matching results should replace the same public card,
  reset to page one, and must not delete the workspace or report that it could
  not be refreshed.
- Exact squad cards and `/squad name` read/edit/clear behavior.
- If stale desktop/mobile command caches leave a newly changed command on
  **Sending command...**, fully restart Discord and retry. Reopening the picker
  or invoking a rendered command may help but did not refresh every tester's
  client cache.

## Maintenance and feedback

- `/elo status` and owner-only `/elo recalculate`
- `/staffhelp` bug, feature, and help forms, including attachments and the staff mirror
- `/whattotest` should always return this current running checklist

Items remain listed until they have received sufficiently broad testing; a single successful invocation does not automatically remove them.
