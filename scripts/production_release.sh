#!/usr/bin/env bash

# Fixed production steps run as the unprivileged polyelo service account.
# Add future reviewed, idempotent schema migrations here; the root wrapper and
# sudoers rule should not need to change for normal releases.

set -euo pipefail

PROJECT_ROOT=/srv/polyelo/PolyBot39
PYTHON=$PROJECT_ROOT/.venv/bin/python

fail() {
  echo "production-release: $*" >&2
  exit 2
}

if (( $# != 0 )); then
  fail 'this command accepts no arguments'
fi
[[ $(/usr/bin/id -un) == polyelo ]] \
  || fail 'release steps must run as the polyelo service account'
[[ ${POLYBOT_ENV:-} == production ]] \
  || fail 'POLYBOT_ENV must be production'

cd "$PROJECT_ROOT"

"$PYTHON" scripts/migrate_player_timezone_production.py \
  --apply \
  --confirm P9-B1-PRODUCTION-TIMEZONE-APPLY
"$PYTHON" scripts/migrate_player_badges_production.py \
  --apply \
  --confirm P12.1-PRODUCTION-PLAYER-BADGES-APPLY
"$PYTHON" scripts/migrate_game_keep_active_production.py \
  --apply \
  --confirm P5.17-PRODUCTION-GAME-KEEP-ACTIVE-APPLY

"$PYTHON" scripts/manage_application_commands.py \
  --environment production \
  --mode apply \
  --guild-ids 283436219780825088,447883341463814144 \
  --confirm-environment production \
  --confirm-guild-ids 283436219780825088,447883341463814144 \
  --confirm-scope guild \
  --confirm-no-global-sync
