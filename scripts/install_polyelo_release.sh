#!/usr/bin/env bash

# One-time root installation for the constrained PolyElo release capability.

set -euo pipefail

PROJECT_ROOT=/srv/polyelo/PolyBot39
WRAPPER_SOURCE=$PROJECT_ROOT/deploy/polyelo-release
SUDOERS_SOURCE=$PROJECT_ROOT/deploy/sudoers/polyelo-release
WRAPPER_TARGET=/srv/polyelo/bin/polyelo-release
SUDOERS_TARGET=/etc/sudoers.d/polyelo-release

fail() {
  echo "install-polyelo-release: $*" >&2
  exit 2
}

if (( $# != 0 )); then
  fail 'this command accepts no arguments'
fi
if (( EUID != 0 )); then
  fail 'run this installer through sudo'
fi
[[ -f $WRAPPER_SOURCE && ! -L $WRAPPER_SOURCE ]] \
  || fail 'wrapper source must be a regular non-symlink file'
[[ -f $SUDOERS_SOURCE && ! -L $SUDOERS_SOURCE ]] \
  || fail 'sudoers source must be a regular non-symlink file'

/usr/sbin/visudo -cf "$SUDOERS_SOURCE" >/dev/null
/usr/bin/install -d -o root -g root -m 0755 /srv/polyelo/bin
/usr/bin/install -o root -g root -m 0755 \
  "$WRAPPER_SOURCE" "$WRAPPER_TARGET"

rollback_copy=$(/usr/bin/mktemp)
had_previous=false
cleanup() {
  /usr/bin/rm -f -- "$rollback_copy"
}
trap cleanup EXIT
if [[ -e $SUDOERS_TARGET ]]; then
  [[ -f $SUDOERS_TARGET && ! -L $SUDOERS_TARGET ]] \
    || fail 'existing sudoers target is not a regular file'
  /usr/bin/cp -a -- "$SUDOERS_TARGET" "$rollback_copy"
  had_previous=true
fi
/usr/bin/install -o root -g root -m 0440 \
  "$SUDOERS_SOURCE" "$SUDOERS_TARGET"
if ! /usr/sbin/visudo -cf /etc/sudoers >/dev/null; then
  if [[ $had_previous == true ]]; then
    /usr/bin/install -o root -g root -m 0440 \
      "$rollback_copy" "$SUDOERS_TARGET"
  else
    /usr/bin/rm -f -- "$SUDOERS_TARGET"
  fi
  /usr/sbin/visudo -cf /etc/sudoers >/dev/null \
    || fail 'sudoers validation failed and the prior state is also invalid'
  fail 'sudoers validation failed; restored the prior state'
fi

/usr/bin/cmp --silent "$WRAPPER_SOURCE" "$WRAPPER_TARGET" \
  || fail 'installed wrapper does not match tracked source'

echo 'Installed /srv/polyelo/bin/polyelo-release and its exact sudoers rule.'
echo 'Future approved releases may run: sudo -n /srv/polyelo/bin/polyelo-release'
