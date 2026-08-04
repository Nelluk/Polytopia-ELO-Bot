# 🧪 WHAT TO TEST

This is the running wider-beta checklist—not just the newest release. Please use the native slash commands and interactive controls below on desktop or mobile. Report confusing behavior, missing information, permission problems, stale cards, and unexpected private/public responses with `/staffhelp`.

## Games

- `/game record`, `/game open`, `/game join`, `/game leave`, `/game start`, `/game show`, `/game search`, and `/game players`
- Interactive open-game card: join, leave, refresh, start, and delete controls
- Interactive game card actions appropriate to the game state
- `/game win`, `/game result confirm`, `/game result undo`, and staff corrections
- `/game map`, `/game name`, `/game notes`, `/game side`, `/game ranked`, and bulk `/game tribe`
- `/game manage kick`, `/game manage extend`, `/game manage unstart`, and `/game manage delete`
- Check that successful changes are public and identify who made them; failures should remain private

## Players and leaderboards

- `/player show`, including profile sections and navigation
- `/player register` and `/player timezone` read/edit/clear behavior
- `/leaderboard players`: pagination, common filters, advanced filters, active/all toggle, jump-to-page, desktop/mobile layout
- `/leaderboard activity` and `/leaderboard squads`
- Check local/global, current/peak, current-era/all-time, and active/all combinations for plausible ELO and W-L records

## Teams

- `/team create`
- `/team emoji`, `/team image`, `/team name`, `/team server`, `/team tier`, and `/team house`: read, edit, clear where offered, requester-team inference, and public actor attribution
- Team permission checks using `@testers` (staff) and `@Mod` roles
- `/team show`: explicit and inferred-team lookup, dense roster/ELO card, graph and image display, recent/all-completed activity toggle, missing-role warning, and desktop/mobile layout

## Maintenance and feedback

- `/elo status` and owner-only `/elo recalculate`
- `/staffhelp` bug, feature, and help forms, including attachments and the staff mirror
- `/whattotest` should always return this current running checklist

Items remain listed until they have received sufficiently broad testing; a single successful invocation does not automatically remove them.
