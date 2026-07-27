#!/usr/bin/env python3
"""Print a reproducible inventory of the active Python environment.

This intentionally uses importlib.metadata instead of pip so it remains useful
when the environment's pip command is unavailable or broken.
"""

import argparse
from importlib import metadata
import json
import platform
import sys


def installed_distributions():
    """Return installed distributions sorted by normalized project name."""
    distributions = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get('Name')
        if name:
            distributions[name] = distribution.version
    return dict(sorted(distributions.items(), key=lambda item: item[0].lower()))


def inventory():
    """Return runtime and installed-package details."""
    return {
        'python': platform.python_version(),
        'implementation': platform.python_implementation(),
        'executable': sys.executable,
        'platform': platform.platform(),
        'packages': installed_distributions(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--json', action='store_true', help='emit machine-readable JSON'
    )
    args = parser.parse_args()
    details = inventory()

    if args.json:
        print(json.dumps(details, indent=2))
        return

    print(f"Python=={details['python']}")
    print(f"Implementation=={details['implementation']}")
    print(f"Executable=={details['executable']}")
    print(f"Platform=={details['platform']}")
    for name, version in details['packages'].items():
        print(f'{name}=={version}')


if __name__ == '__main__':
    main()
