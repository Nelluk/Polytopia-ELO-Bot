# 🧪 WHAT TO TEST

Status: retired slash-command acceptance checklist retained as historical
reference pending the cleanup described in
[`BETA_ONLY_CLEANUP.md`](BETA_ONLY_CLEANUP.md).

This was the full running wider-beta checklist—not just the newest release.
`/whattotest` presents these sections through a compact private Beta Lab
dashboard instead of posting the whole file into a channel. Use the native
slash commands and interactive controls below on desktop or mobile. Report
confusing behavior, missing information, permission problems, stale cards,
and unexpected private/public responses with the dashboard's **Report
problem** button or `/staffhelp`.

For a low-friction pass, run `/whattotest` and choose **Give me a 5-minute
test**. Click it again for another short assignment. These read tests use the
current development data, including historical mirrored players, Teams, and
games; missing synthetic fixture packs do not block them. When the dashboard
reports guided mutable sessions ready, eligible testers can choose **Start
guided session** for an owned Team/House persona plus fresh Ready, Unconfirmed,
and Completed games. Choose any one task; its page supplies exact slash fields
and the expected result. Use **Refresh results**, then **Finish and clean up**
whenever done. If the guided button is unavailable, stick to read tests. The
full sections below are a reference, not a request to test everything in one
sitting. Use **Close panel** to dismiss an ordinary read-testing workspace;
**Finish and clean up** appears only for an active guided session.

## Games

- `/game record`, `/game open`, `/game join`, `/game leave`, `/game start`, `/game show`, `/game search`, and `/game players`
- After confirming `/game open`, its public warning/completion messages should
  appear as standalone channel messages—not as replies to a private or deleted
  draft interaction—and the join reaction should still be added.
- Interactive open-game card: join, leave, refresh, start, and delete controls
- Interactive game card actions appropriate to the game state
- `/game win`: start a guided session, choose **Win claim**, and use the exact
  fields shown for its Ready game. Check the pending wording, game card,
  channel notices, announcements, and any ELO/Nova/experience/champion effects;
  a publication warning after a committed result must say not to retry.
- `/game result undo`: choose **Undo result**, use the exact Completed game,
  and verify its result and ELO
  reset once, and check the public reset notice plus experience/champion role
  reconciliation. Compare retained `$unwin`; an after-commit warning must not
  invite a duplicate undo.
- Compare `/game win` with retained `$win` for the same permission, pending vs.
  confirmed result, channel-routing, card, announcement, and role behavior.
- `/game map`, `/game name`, `/game notes`, `/game side`, and bulk `/game tribe`
- `/game manage kick` and `/game manage delete` on games where the ordinary
  host/participant permission permits them. Staff correction variants are in
  **Helper commands to test**.
- The former beta-only direct paths `/game unwin`, `/game confirm`,
  `/game delete`, `/game unconfirmed`, `/game set-ranked`, `/game extend`, and
  `/game unstart` should no longer appear. After this command-tree update,
  fully restart Discord if a client still shows stale paths or
  leaves a newly nested command at **Sending command...**.
- `/game ping`: try inferred and explicit games, Compose/Edit/Cancel, reopening
  after dismissing a modal, long multi-section text, multiple attachments, and
  Confirm. Verify recipients see the actor (and any on-behalf-of target), role
  or `@everyone` text does not ping, delivery is not duplicated, and any
  partial fanout failure gives a public terminal reconciliation warning.
- `/game logs`: as a participant, open a game you played; use Search, Clear
  search, scope changes when offered,
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
- Retained `$getname`/`$getcode` should return the account-wide canonical name;
  `$getnames`/`$codes` should preserve draft order, account names, and timezone
  hints without stalling other bot activity.
- `/leaderboard players`: pagination, common filters, advanced filters, active/all toggle, jump-to-page, desktop/mobile layout
- `/leaderboard teams`: active/all-tier default, tier and archived-team filters, pagination/page jump, current-page ELO graph, desktop/mobile layout, and sensible behavior for teams with missing or empty Discord roles
- `/leaderboard roles`: Free Agents default for ordinary users; All/Any
  matching; global/local ELO and W-L; total/recent-game sorting;
  inactive-member handling; pagination/page jump; desktop/mobile layout.
  Privileged multi-role selection is in **Helper commands to test**.
- `/leaderboard activity` and `/leaderboard squads`
- Check local/global, current/peak, current-era/all-time, and active/all combinations for plausible ELO and W-L records

## Teams

- `/team show`: explicit and inferred-team lookup, dense roster/ELO card, graph and image display, recent/all-completed activity toggle, missing-role warning, and desktop/mobile layout
- `/team house`: omit `house` and `clear` to inspect the current affiliation
  and requester-Team inference. All other Team attribute commands require Mod
  access and are in **Mod commands to test**.

## Houses

- `/house list`: browse pages, select a House, return to the list, and verify
  the controls work on desktop and mobile without reloading or replacing the
  public workspace.
- `/house show`: try an explicit House and omit the option when you have one
  unambiguous House role. Check leadership, active/archived Teams, tiers,
  current Team ELO, rosters, player ELO, and the House image.
- `/house name`: omit `name` for a public read and confirm the stored name is
  clear. Mod rename coverage is in **Mod commands to test**.
- `/house image`: omit image/clear for a public read. Check the current image on
  desktop and mobile; Mod replace/clear coverage is in **Mod commands to
  test**.
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
- `/league mark-active`: remove your own Inactive role. Targeting another
  member is covered in **Mod commands to test**.
- `/league join-novas`: try an eligible registered user with no Team role.
  Success should add The Novas, remove Newbie when present, and publish an
  attributed confirmation. Unregistered users and existing Team members
  should fail privately without changing roles.
- `/league season`: omit `season` for all records, select a normal season, and
  try Season 1 or 2 for the historical result. Verify tier headings,
  regular/postseason W-L-incomplete counts, public output, and paging/page
  jump when enough teams are present.
- `/league roster price`: choose a registered player with recent league games,
  first omitting `season` and then selecting an explicit ending season. Verify
  the public no-ping result shows the final price, chosen/inferred ending
  season, three season tier/W-L/game inputs, and whether the House Leader or
  House Co-Leader adjustment applied. A player with no qualifying three-season
  history should fail privately. The retired `$tradeprice` and `$playerprice`
  commands should not appear.
- Retained `$tutorial`, `$imalive`, `$novas`, `$joinnovas`, `$season`,
  `$jrseason`, `$ps`, `$js`, and `$seasonjr` should continue
  to reach the same underlying behavior.

- `/league tokens` with no options: verify the public workspace lists all
  configured House balances, Recent changes opens the audit history, paging
  works, and selecting a House opens only that House's balance/history.
- `/league tokens house:HOUSE` should open the same House-specific view
  directly. Another user must not be able to operate your controls; expired,
  invalid, and ambiguous selections should fail privately.
- The retired `$tokens` command should no longer be available. `/league
  tokens` deliberately remains usable outside designated bot channels in the
  configured league/test guild to preserve its prior access behavior.

## Helper commands to test

- This page is for members with the configured Helper/staff classification.
  The pinned `testers` role does not grant Helper access. Use only designated
  development fixtures, and do not attempt a mutable scenario when the
  dashboard says guided mutable sessions are not prepared.
- `/game result confirm`: when a safe Unconfirmed test game is available,
  confirm it and verify the result/card/effects publish once without duplicate
  audit or role work. Compare retained `$confirm`; a committed publication
  warning must say not to retry.
- `/game ranked`, `/game manage extend`, and `/game manage unstart`: use safe
  development games and verify validation failures stay private while
  successful corrections are public and actor-attributed. Retained `$rankset`,
  `$rankunset`, `$extend`, and `$unstart` should accept the same numeric IDs.
- `/game logs`: omit the game ID for the staff server view, then exercise
  Search, Clear search, scope changes, paging, and page jump. Old log text must
  not ping anyone, and another user must not control the workspace.
- `/game search` with `view:Unconfirmed results`: verify the staff result queue
  is useful and private failures remain private.
- `/leaderboard roles`: verify Helper/House Leader multi-role selection,
  All/Any matching, and the same sorting/paging behavior as the ordinary view.
- `/league roster promote` and `/league roster trade`: verify player/profile
  and stored Team images, optional text, and one direct HTTP(S) image override.
  Success should identify the actor; invalid images and non-Helper attempts
  must not publish. Retained `$promote` and `$trade` require Helper access.
- `/league roster draft`: as a Drafter, Helper, or Mod, choose a registered
  member and exact Team with a matching Discord role and stored image. Verify
  player/Team images, Team-role color, ELO/W-L, House heading, public actor
  attribution, and private validation failures. `$draft` remains retired.
- `/league maintenance export`: run `include_logs:false` and
  `include_logs:true`. Each response stays private and attaches a readable gzip
  CSV for confirmed ranked 2v2/3v3 league games; the latter adds a `logs`
  column. Retained `$league_export` uses the same data and format.
- `/league tokens`: after recording the current value, update one development
  House with `amount` and optional `note`, then restore the original value.
  Both updates should be public, actor-attributed, newest-first in history, and
  never duplicated after publication failure. Non-staff may read but not edit.
- `/elo status`: verify the private response accurately reports whether an ELO
  mutation job is active. Owner-only recalculation is on **Owner/operator
  commands to test**.

## Mod commands to test

- This page is for members with a configured Mod role. Use only disposable or
  explicitly retained development objects. A Helper-only persona must be
  denied where Mod authority is required.
- `/team create`, plus mutation modes of `/team emoji`, `/team image`, `/team
  name`, `/team server`, `/team tier`, and `/team house`: verify edit/clear
  behavior where offered, requester-Team inference, public actor attribution,
  and private denial for non-Mods.
- Mutation modes of `/house name` and `/house image`: verify rename,
  replace/clear with PNG/JPEG/WebP, public actor attribution, and the warning
  that Discord role renaming is separate. `/house create` should create only a
  reviewed development House intended to be retained; duplicate, invalid,
  non-Mod, and wrong-channel attempts must create nothing.
- `/game manage delete`: use the Mod-only completed-game path only on an agreed
  disposable development game and verify any ELO reversal occurs once.
- `/league mark-active member:...`: target another member and verify House
  Leaders, Co-Leaders, and Mods are allowed while ordinary users are denied
  privately. Successful role changes should identify actor and target.
- `/league free-agents post`: use the default or a disposable channel and test
  modal Preview/Edit/Cancel before Confirm. Verify actor attribution, `🔆`, `⏯`,
  and `❎` behavior, duplicate-post refusal, eligibility checks, and non-Mod
  denial. Retained `$newfreeagent` should reach the same behavior.
- `/league maintenance mark-inactive`: inspect the private candidate/exclusion
  preview, warnings, paging, and **Cancel** first. Confirm only after every
  candidate is safe; apply is capped at 100 and must publish one no-ping
  aggregate while individual failures stay private. Retired
  `$deactivate_players`, `$deactivate`, and `$kick_inactive` must not run.
- `/league maintenance kick-inactive`: inspect every eligible/protected reason,
  paging, and 25-member run/deferred counts, then **Cancel**. Helpers, Mods,
  leadership, bots, owner, managed/unknown roles, and unsafe accounts must be
  protected. Do not submit `KICK <count>` unless Nelluk has designated every
  listed account as disposable.

## Owner/operator commands to test

- `/elo recalculate` is owner-only. Use its plan/confirmation flow only under a
  separately agreed development ELO test; ordinary Helpers and Mods must be
  denied.
- `/operator beta prepare` and `/operator beta reset` are owner-only fixture
  controls. Preview/Cancel and expiry must make no changes; any later apply is
  separately approval-gated and must touch only the exactly owned scenario
  bundle. These commands are not part of ordinary wider-beta testing.

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

## Feedback and dashboard

- `/staffhelp` bug, feature, and help forms, including attachments and the staff mirror
- `/whattotest` should clearly separate ready read tests from unavailable
  guided mutable sessions, keep staff-only fixture diagnostics out of the
  tester panel, show privileged tests only in their labeled sections, and
  offer **Close panel** when no guided session exists.

Items remain listed until they have received sufficiently broad testing; a single successful invocation does not automatically remove them.
