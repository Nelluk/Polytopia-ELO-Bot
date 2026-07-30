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
explicit cleanup.
