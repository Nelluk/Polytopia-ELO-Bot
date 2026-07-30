# Development Beta Fixtures

`scripts/manage_dev_fixtures.py` creates a small, repeatable game set for
testing beta Discord commands. It is separate from `--add_default_data`,
which initializes permanent reference data.

## Safety

The command refuses to operate unless both the configured profile and the
live PostgreSQL session identify:

- `POLYBOT_ENV=development`
- database `polytopia_dev`
- role `polybot_dev`
- background tasks disabled
- API disabled
- a guild allowed by the development profile

Run seed and cleanup only while the beta bot is stopped. The in-process ELO
coordinator cannot coordinate with a separate fixture-management process.

The selected user IDs must already have `DiscordMember` and `Player` records
in the development guild. The harness does not create or modify users.

## Commands

Seed an even group of 2, 4, 6, or 8 existing development-guild users:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py seed \
  --user DISCORD_ID \
  --user DISCORD_ID
```

Inspect the fixture set:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py status
```

After stopping the beta bot, remove all owned fixture games:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py cleanup --confirm
```

Seed is idempotent for the same user set. To change users, clean the existing
set first.

## Created scenarios

- `Beta Fixture Ready`: ranked, started, and without a winner.
- `Beta Fixture Unconfirmed`: ranked with a claimed but unconfirmed winner.
- `Beta Fixture Completed`: ranked with a confirmed winner and ELO history.

The database ownership marker is stored in `Game.notes`. Cleanup requires the
exact marker, development guild, and fixture name prefix. It will not delete
ordinary games.

The ignored local manifest is written to
`data/development/beta_fixture_manifest.json`. It is an operator aid, not
cleanup authority. If it is lost, database markers still identify fixtures;
if it is stale, it cannot authorize an unrelated deletion.

Games created interactively with `/newgame` are not automatically adopted.
Delete those through the beta command after testing, or record their IDs for
explicit cleanup. A recorded interactive game may instead be intentionally
retained for later development testing, but it is not reset or protected by
the harness and its current state must be checked before each reuse.

## Combined P2.2/P3.1 beta procedure

This sequence was used for the accepted `/newgame`,
`/recalc-games-from`, and `/elo-job-status` session and remains the reusable
procedure for a later regression run.

### 1. Verify fixtures while the beta is stopped

Confirm no development beta process is running, then inspect the owned set:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py status
```

The accepted session used:

- users `272510639124250625` (Nelluk) and `481525222072254484`
  (`testaccount12174`);
- game `115`: ready/incomplete ranked;
- game `116`: claimed but unconfirmed ranked;
- game `117`: confirmed ranked and the recalculation target.

The owned set was cleaned successfully after acceptance, so current `status`
reports no owned games. `status` is read-only. Do not reseed when fixtures are
present. To recreate this set for a later session, seed it only while the beta
remains stopped:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py seed \
  --user 272510639124250625 \
  --user 481525222072254484
```

Record the returned game IDs because a newly seeded set may not reuse
`115`-`117`.

### 2. Verify the profile, then launch and synchronize

```bash
POLYBOT_ENV=development .venv/bin/python scripts/check_runtime_config.py
POLYBOT_ENV=development .venv/bin/python bot.py --skip_tasks
```

The preflight must select the beta application, `polytopia_dev`, the
development guild, and disabled background tasks/API. Startup should
synchronize these eight development-guild commands:

- `win`
- `unwin`
- `delete`
- `newgame`
- `confirm`
- `unconfirmed`
- `recalc-games-from`
- `elo-job-status`

Do not proceed if the authenticated bot, database, guild, runtime policy, or
sync target differs.

### 3. Test P3.1 using the owned confirmed fixture

1. As staff, run `/elo-job-status`; it should report no active job.
2. As the owner, run `/recalc-games-from` with the confirmed fixture game ID
   and `confirm:false`; it must refuse without starting work.
3. Run the same command with `confirm:true`; it should defer promptly and
   eventually report the recalculation timestamp.
4. While it is running, use `/elo-job-status` from another client/session if
   practical. It should show operation, game, requester, start time, and
   elapsed time. A fast recalculation may finish before this can be observed.
5. To exercise conflict handling without changing another fixture, submit the
   same confirmed recalculation again only while status still reports the
   first job active. It should reject promptly. Skip this timing-dependent
   case if the first job has already completed.
6. After completion, `/elo-job-status` should return to idle.
7. If a non-owner account is available, verify that
   `/recalc-games-from ... confirm:true` is denied before work starts.

Game `117` is the current target. Use the ID reported by `status` instead if
the set has been reseeded.

### 4. Test P2.2 `/newgame`

Create at least:

- a ranked Mobile 1v1; and
- an unranked Steam 1v1.

Use the native Discord member selectors and include the requester unless
testing a staff override. If enough distinct development-guild members are
available, also exercise the optional slots with a 2v2; lack of four suitable
accounts does not block acceptance because the option shape is covered
offline.

Record every created game ID immediately. These games are ordinary,
unowned rows and are deliberately outside fixture cleanup. Delete them
through `/delete` during the beta session when practical, and verify the
preserved prefix interface with one low-impact prefix case if desired. Any
interactive game not successfully deleted must remain on an explicit cleanup
list.

### 5. Optional fixture-backed regression checks

If broader pilot coverage is useful:

- game `115` can exercise a win claim followed by `unwin`;
- game `116` can exercise `confirm`;
- game `117` can exercise confirmed-game reads or owner recalculation.

These checks are optional for P2.2/P3.1. Record mutations and final states;
the owned cleanup is designed to remove the marked fixture set afterward.

### 6. Stop, inspect, and clean up

Stop the beta process cleanly before using the harness again. Then:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py status

POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py cleanup --confirm

POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py status
```

The final status should show no owned fixture games. This cleanup does not
authorize or remove interactive `/newgame` rows. Confirm each recorded
interactive ID was deleted through Discord, place it on an explicitly scoped
cleanup list, or document that it is intentionally retained for a later
development unit.

Record the tested branch/commit, synchronized command list, command results,
fixture mutations, interactive game IDs, final cleanup result, and beta
shutdown result in the modernization roadmap.

## Unified `/game` and `/elo` beta procedure

Use this sequence for checkpoint `63af179` or its later documentation
checkpoint. It validates the clean taxonomy migration of every native command
implemented through P4.1d.

### 1. Preflight while the beta is stopped

Confirm no development beta process is running, then run:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py status

POLYBOT_ENV=development .venv/bin/python \
  scripts/check_runtime_config.py
```

At preparation time the owned set is:

- game `149`: ready/incomplete and unranked;
- game `150`: claimed but unconfirmed and ranked;
- game `151`: completed/confirmed and ranked.

Use the current ready game reported by `status` rather than assuming ID 149
if the set is later reseeded. Do not seed while this owned set exists.

### 2. Launch and verify development-guild synchronization

Launch remains separately approved:

```bash
POLYBOT_ENV=development .venv/bin/python bot.py --skip_tasks
```

The preflight and startup must select the beta application, `polytopia_dev`,
role `polybot_dev`, development guild `478571892832206869`, and disabled
background tasks/API.

The synchronized top-level application-command tree should contain exactly:

- `/game`
- `/elo`

Expanding `/game` must show:

- `/game record`
- `/game win`
- `/game unwin`
- `/game delete`
- `/game confirm`
- `/game unconfirmed`
- `/game set-ranked`
- `/game unstart`
- `/game extend`

Expanding `/elo` must show:

- `/elo recalculate`
- `/elo status`

The old top-level names (`/newgame`, `/win`, `/unwin`, `/delete`, `/confirm`,
`/unconfirmed`, `/set-ranked`, `/recalc-games-from`, and `/elo-job-status`)
and the never-synchronized `/match` root should be absent. Their corresponding
prefix commands and aliases remain registered.

### 3. Smoke the existing pilot commands

Use the three owned fixtures and record every mutation:

1. Run `/elo status`; it should report idle ephemerally.
2. Run `/game unconfirmed`; it should include the current unconfirmed fixture.
3. Run `/game set-ranked` on the ready fixture, then reverse the value with
   the preserved `$rankset` or `$rankunset` prefix path. Successful native
   output should be public.
4. Run `/game win` against the ready fixture using one fixture participant as
   winner, then `/game unwin` to restore it. Confirm both remain responsive
   and that the win reversal returns the fixture to incomplete state.
5. Run `/game confirm` against the unconfirmed fixture. This intentionally
   changes that fixture to confirmed; record the result.
6. As owner, run `/elo recalculate` against the confirmed fixture with
   `confirm:false`, then with `confirm:true`. The false case should refuse
   ephemerally; the true case should defer ephemerally and complete. Check
   `/elo status` during the job if timing permits.

Permission-denial checks are useful when a non-staff/non-owner beta account is
available. They should fail ephemerally before worker submission.

### 4. Smoke record and delete

1. Use `/game record` to create one disposable ranked game with a roster such
   as `@PlayerOne vs @PlayerTwo`.
2. Confirm that the parsed requester-only preview is correct, edit the roster
   once if useful, then select Confirm record.
3. Record the returned game ID immediately; it is an ordinary unowned game.
4. Delete it with `/game delete` and confirm the public result.
5. Verify one low-impact preserved prefix creation/deletion or `$help`
   registration case if practical. Do not leave the interactive game
   unrecorded if deletion fails.

If suitable registered test members are available, also preview an uneven or
three-sided roster. Existing guild rules may reject an uneven game at final
validation even when the parser correctly infers its sides.

### 5. Smoke `/game unstart`

Run these in a bot-command channel that is not associated with the ready
fixture:

1. If a non-staff account is available, attempt `/game unstart` against the
   ready game. It should deny ephemerally without changing the game.
2. As staff, run `/game unstart` against the ready game. Discord should show
   an immediate public defer and then a public success message mentioning the
   players.
3. Confirm the game is now an open matchmaking game and its deadline is at
   least 24 hours in the future.
4. Repeat `/game unstart` against the same game. It should report
   ephemerally that the game is already pending.
5. Run `$unstart READY_ID`; it should reach the preserved prefix path and
   report that the same game is already pending.

The harness fixture normally has no real announcement or game channels, so
this live case proves the native interaction and database transition but not
Discord channel deletion. Post-commit ordering, failed-effect retention, and
channel reconciliation remain covered by focused offline tests. A separate
interactive game with disposable beta channels may be used only if those
resources and its cleanup are recorded.

### 6. Smoke `/game extend`

1. Run `/game extend` against the now-pending ready game. It should defer
   publicly and report the old and new deadlines publicly.
2. Verify the new deadline is 24 hours later than the prior future deadline.
3. Run `$extend READY_ID` once to confirm the preserved prefix path; it should
   add another 24 hours.
4. If a non-staff account is available, verify `/game extend` denies before
   deferring or changing the deadline.
5. Run a harmless command such as `/elo status` between mutations to
   confirm the bot remains responsive.

### 7. Stop and record state

Stop the beta cleanly. With the beta stopped, rerun fixture `status` and
record that the ready fixture is now pending plus its final deadline. Retain
games `149`-`151` unless there is an explicit cleanup decision; this smoke
test does not require harness cleanup.

Record:

- tested branch and commit;
- synchronized `/game` and `/elo` trees and absence of old top-level names;
- results for all eleven migrated native commands;
- public-success and ephemeral-denial behavior;
- ready fixture ID and final deadline;
- prefix results;
- beta shutdown result;
- any interactive channel/announcement test resources.

## Components v2 leaderboard showcase

The player-leaderboard showcase is a separate owned fixture family. It does
not change or take ownership of the three reusable game-command fixtures.
Run these operations only while the development beta is stopped:

```bash
cd /home/nelluk/PolyBot39-dev

POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py leaderboard-status

POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py leaderboard-seed
```

`leaderboard-seed` is idempotent. It creates 24 exact
`LB2 Showcase 01`-through-`24` profiles and 48 recent ranked 1v1 games, enough
to exercise several pages, varied records, current/peak/all-time ratings,
local/global views, and active/all toggles.

The data is independently owned by all of:

- exact `development` / `polytopia_dev` / `polybot_dev` gates;
- the configured development guild;
- a reserved Discord-ID range;
- exact generated player/member names;
- a dedicated exact game notes marker and generated game-name set.

Cleanup is confirmed and applies only to rows satisfying every ownership
check:

```bash
POLYBOT_ENV=development .venv/bin/python \
  scripts/manage_dev_fixtures.py leaderboard-cleanup --confirm
```

For P7.5 beta testing:

1. Run `/lb2`; it should have no slash options.
2. Compare its default local/current/active result with
   `/leaderboard players`.
3. Change the preset to global current, local peak, local all-time, and global
   all-time.
4. Toggle **Show all players** and back to **Show active only**.
5. Use Previous/Next and the page-number modal.
6. Use **My rank**; it should jump when the requester is present or explain
   ephemerally when absent.
7. Have another user try a control; the public result should not change and
   the denial should be ephemeral.
8. Confirm ordinary commands remain responsive while an uncached preset is
   loading.
