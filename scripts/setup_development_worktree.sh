#!/usr/bin/env bash

set -euo pipefail

primary_checkout=/home/nelluk/PolyBot39-dev
production_checkout=/home/nelluk/PolyBot39
target_checkout=${1:-$PWD}
shared_python="$primary_checkout/.venv/bin/python"

fail() {
    echo "Worktree setup refused: $*" >&2
    exit 1
}

target_checkout=$(realpath -m -- "$target_checkout")

[[ -d "$target_checkout" ]] || fail "target is not a directory: $target_checkout"
[[ "$target_checkout" != "$production_checkout" ]] || fail "production checkout is never a valid target"
[[ "$target_checkout" != "$production_checkout/"* ]] || fail "production checkout descendants are never valid targets"
[[ -x "$shared_python" ]] || fail "shared development interpreter is unavailable: $shared_python"

for local_file in config.development.ini server_settings_dev.py; do
    source_path="$primary_checkout/$local_file"
    target_path="$target_checkout/$local_file"

    [[ -f "$source_path" && ! -L "$source_path" ]] || \
        fail "development source must be a regular file: $source_path"

    if [[ "$target_checkout" == "$primary_checkout" ]]; then
        continue
    fi

    if [[ -L "$target_path" ]]; then
        [[ $(readlink -f -- "$target_path") == "$source_path" ]] || \
            fail "existing symlink has an unexpected target: $target_path"
        continue
    fi

    [[ ! -e "$target_path" ]] || \
        fail "refusing to overwrite existing path: $target_path"
    ln -s -- "$source_path" "$target_path"
done

[[ -f "$target_checkout/scripts/check_runtime_config.py" ]] || \
    fail "target does not look like a PolyBot development checkout: $target_checkout"

profile_output=$(
    cd "$target_checkout"
    POLYBOT_ENV=development "$shared_python" scripts/check_runtime_config.py
)

grep -Fq 'environment: development' <<<"$profile_output" || \
    fail "runtime profile is not development"
grep -Fq 'database: polytopia_dev' <<<"$profile_output" || \
    fail "runtime database is not polytopia_dev"
grep -Eq '^[[:space:]]*psql_user[[:space:]]*=[[:space:]]*polybot_dev[[:space:]]*$' \
    "$primary_checkout/config.development.ini" || \
    fail "runtime database role is not polybot_dev"
grep -Fq 'background tasks enabled: False' <<<"$profile_output" || \
    fail "development background tasks are enabled"
grep -Fq 'HTTP API enabled: False' <<<"$profile_output" || \
    fail "development API is enabled"

echo "Development worktree ready: $target_checkout"
echo "Shared interpreter: $shared_python"
printf '%s\n' "$profile_output"
