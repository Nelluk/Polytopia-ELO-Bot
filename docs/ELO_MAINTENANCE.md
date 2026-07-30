# ELO Maintenance

This document describes the supported ELO repair paths and their execution
boundaries. It does not authorize a beta or production operation.

## Discord recalculation

The owner-only prefix `recalc_games_from` command and native
`/recalc-games-from` command recalculate from one completed game onward. Both
use the same bounded ELO executor and process-local coordinator. The slash
command requires explicit confirmation and defers before submitting work.

The coordinator serializes ELO mutations within one bot process and reports
the active operation through `/elo-job-status`. It cannot coordinate with a
separate CLI process or fixture-management process.

## Cancellation semantics

Python cannot safely stop a thread while synchronous Peewee work is running.
Cancelling the awaiting Discord task therefore does not abort the worker:

- the coordinator remains reserved until the worker transaction finishes;
- repeated cancellation does not release the reservation early;
- a successful transaction may commit even though its awaiting Discord task
  was cancelled;
- no forced-cancel or abort command is exposed.

Shutdown should allow the worker to finish. If hard process termination is
unavoidable, PostgreSQL—not application-level cancellation—determines whether
the open transaction rolls back.

## Full command-line recalculation

`bot.py --recalc_elo` is a standalone synchronous maintenance mode. It opens
and closes its own Peewee connection, claims the process-local ELO
coordinator, and runs the full recalculation in one synchronous transaction.
It does not launch Discord or use the executor because there is no Discord
event loop to protect.

Run this mode only while the bot process for the same database is stopped.
The coordinator cannot prevent a separately running bot from mutating ELO at
the same time. Development and production executions remain subject to their
respective runtime and approval gates.

## Retired duplicate-ELO reversal

The hidden `reverse_duplicated_elo` prefix command was retired during P3.2.
It always returned “command not finished”; its unreachable body bypassed the
coordinator and transaction boundary and contained an invalid game-side
reference.

Use the supported recalculation-from-game workflow for a bounded repair, or
the separately controlled full command-line recalculation when a complete
rebuild is actually required. No slash replacement is provided for the
retired command.
