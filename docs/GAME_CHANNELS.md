# Game-channel reuse

Starting a game creates missing team channels and, for eligible larger games,
a central channel. A side with an existing channel reference reuses that
channel, including after unstart, a lineup change, and restart. Season-game
channels and recent Nova-game channels remain protected during unstart.

On restart, retained channels keep their IDs, history, names, categories, and
existing role/member permissions. The bot checks the current stored roster and
channel reference before refreshing the channel:

- Empty topics and recognizable bot-written topics for that game receive the
  current game name, roster, host, and notes. Custom topics are preserved.
- Season-game topics explain that the channel is retained after completion.
- Current participants already able to view, post, and read history need no
  permission change. Otherwise the bot adds only those missing permissions to
  that participant's existing member overwrite, preserving unrelated settings.
- Explicit member/non-default-role denials of those permissions and Discord
  timeouts are preserved and reported for staff review. Players absent from
  the channel's server are also reported.
- Former participants' permissions are not revoked. Team/category access may
  legitimately give them continued access; staff control those permissions.

Reusing a channel does not send another creation greeting. The original
greeting remains historical; the current bot-written topic and game card show
the new lineup. Custom channel names may still contain the old game name.

A missing/inaccessible channel, changed game/reference, or Discord edit
failure produces a reconciliation warning. The bot retains the reference and
does not create a duplicate as a fallback. Staff should inspect the reported
channel and access settings; the game start has already committed and should
not be repeated to repair a channel.

These updates run on subsequent starts. Deploying the code does not refresh
already-running games or alter their channels retroactively.
