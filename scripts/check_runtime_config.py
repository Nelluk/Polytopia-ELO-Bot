#!/usr/bin/env python3
"""Print the selected runtime profile without importing bot or database code."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime_config import (
    RuntimeConfigurationError,
    format_runtime_profile,
    get_runtime_profile,
)


def main(profile=None) -> int:
    try:
        selected_profile = profile or get_runtime_profile()
    except RuntimeConfigurationError as exc:
        print(f'Runtime configuration error: {exc}', file=sys.stderr)
        return 2
    print(format_runtime_profile(selected_profile))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
