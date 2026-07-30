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

## P4.1 match-group beta procedure

Use this sequence for checkpoint `416ca30` or its later documentation
checkpoint. It tests the first T-A domain group without retesting every
previous command.

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

The synchronized top-level tree should contain the nine previously tested
top-level commands plus `match`. Expanding `/match` must show:

- `/match unstart game_id`
- `/match extend game_id`

The unsynchronized top-level `/extend` name should be absent. Prefix
`$unstart` and `$extend` remain registered.

### 3. Smoke `/match unstart`

Run these in a bot-command channel that is not associated with the ready
fixture:

1. If a non-staff account is available, attempt `/match unstart` against the
   ready game. It should deny ephemerally without changing the game.
2. As staff, run `/match unstart` against the ready game. Discord should show
   an immediate public defer and then a public success message mentioning the
   players.
3. Confirm the game is now an open matchmaking game and its deadline is at
   least 24 hours in the future.
4. Repeat `/match unstart` against the same game. It should report
   ephemerally that the game is already pending.
5. Run `$unstart READY_ID`; it should reach the preserved prefix path and
   report that the same game is already pending.

The harness fixture normally has no real announcement or game channels, so
this live case proves the native interaction and database transition but not
Discord channel deletion. Post-commit ordering, failed-effect retention, and
channel reconciliation remain covered by focused offline tests. A separate
interactive game with disposable beta channels may be used only if those
resources and its cleanup are recorded.

### 4. Smoke `/match extend`

1. Run `/match extend` against the now-pending ready game. It should defer
   publicly and report the old and new deadlines publicly.
2. Verify the new deadline is 24 hours later than the prior future deadline.
3. Run `$extend READY_ID` once to confirm the preserved prefix path; it should
   add another 24 hours.
4. If a non-staff account is available, verify `/match extend` denies before
   deferring or changing the deadline.
5. Run a harmless command such as `/elo-job-status` between mutations to
   confirm the bot remains responsive.

### 5. Stop and record state

Stop the beta cleanly. With the beta stopped, rerun fixture `status` and
record that the ready fixture is now pending plus its final deadline. Retain
games `149`-`151` unless there is an explicit cleanup decision; this smoke
test does not require harness cleanup.

Record:

- tested branch and commit;
- synchronized top-level command tree and both `/match` subcommands;
- public-success and ephemeral-denial behavior;
- ready fixture ID and final deadline;
- prefix results;
- beta shutdown result;
- any interactive channel/announcement test resources.
