#!/usr/bin/env bash

set -euo pipefail

fail() {
    echo "Worktree setup refused: $*" >&2
    exit 1
}

canonical_directory() {
    local directory=$1

    [[ -d "$directory" ]] || return 1
    (
        cd -P -- "$directory"
        pwd -P
    )
}

resolve_existing_path() {
    local path=$1
    local link_parent
    local link_target
    local hop

    # A bounded loop keeps a malformed symlink cycle fail-closed.
    for ((hop = 0; hop < 40; hop++)); do
        [[ "$path" == /* ]] || return 1

        if [[ ! -L "$path" ]]; then
            [[ -f "$path" ]] || return 1
            link_parent=${path%/*}
            [[ -n "$link_parent" ]] || link_parent=/
            link_parent=$(canonical_directory "$link_parent") || return 1
            printf '%s/%s\n' "$link_parent" "${path##*/}"
            return 0
        fi

        # All paths reaching this point are absolute. BSD and GNU readlink
        # therefore receive a path operand that cannot be parsed as an option;
        # no GNU-only -- or -f flag is needed.
        link_target=$(readlink "$path") || return 1
        if [[ "$link_target" == /* ]]; then
            path=$link_target
        else
            link_parent=${path%/*}
            [[ -n "$link_parent" ]] || link_parent=/
            path="$link_parent/$link_target"
        fi
    done

    return 1
}

invoked_script=${BASH_SOURCE[0]}
[[ "$invoked_script" == /* ]] || \
    fail "helper must be invoked by an absolute path: $invoked_script"
[[ -f "$invoked_script" && ! -L "$invoked_script" ]] || \
    fail "invoked helper is not a regular file: $invoked_script"

script_parent=${invoked_script%/*}
[[ -n "$script_parent" ]] || script_parent=/
script_parent=$(canonical_directory "$script_parent") || \
    fail "invoked script parent is not an existing directory: $script_parent"
primary_checkout=$(canonical_directory "$script_parent/..") || \
    fail "primary checkout is not an existing directory: $script_parent/.."
primary_parent=$(canonical_directory "$primary_checkout/..") || \
    fail "primary checkout parent is not an existing directory: $primary_checkout/.."

production_checkout="$primary_parent/PolyBot39"
if [[ -d "$production_checkout" ]]; then
    production_checkout=$(canonical_directory "$production_checkout") || \
        fail "production checkout cannot be physically canonicalized: $production_checkout"
fi

target_input=${1:-$PWD}
target_checkout=$(canonical_directory "$target_input") || \
    fail "target is not a directory: $target_input"
shared_python="$primary_checkout/.venv/bin/python"

if [[ "$target_checkout" == "$production_checkout" || \
      "$target_checkout" == "$production_checkout/"* ]]; then
    fail "production checkout and descendants are never valid targets"
fi
[[ -f "$shared_python" && -x "$shared_python" ]] || \
    fail "shared development interpreter is unavailable: $shared_python; bootstrap the primary checkout with 'uv sync --locked --python 3.12.13' under separate dependency-installation approval"
[[ -f "$target_checkout/scripts/check_runtime_config.py" ]] || \
    fail "target does not look like a PolyBot development checkout: $target_checkout"

for local_file in config.development.ini server_settings_dev.py; do
    source_path="$primary_checkout/$local_file"
    target_path="$target_checkout/$local_file"

    [[ -f "$source_path" && ! -L "$source_path" ]] || \
        fail "development source must be a regular file: $source_path"

    if [[ "$target_checkout" == "$primary_checkout" ]]; then
        continue
    fi

    if [[ -L "$target_path" ]]; then
        [[ $(resolve_existing_path "$target_path") == "$source_path" ]] || \
            fail "existing symlink has an unexpected target: $target_path"
        continue
    fi

    [[ ! -e "$target_path" ]] || \
        fail "refusing to overwrite existing path: $target_path"
    [[ "$source_path" == /* && "$target_path" == /* ]] || \
        fail "canonical path validation failed"
    # canonical_directory emits absolute paths, so these BSD/GNU ln operands
    # cannot be parsed as options without relying on GNU-only --.
    ln -s "$source_path" "$target_path"
done

profile_output=$(
    cd -P -- "$target_checkout"
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
