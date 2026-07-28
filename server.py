"""Entry point to run the API server.

Too run this, use the following command:

$ python3 -m uvicorn server:server

(Replacing 'python3' with your Python installation).
You can specify the port/address to bind to with the `--host` and
`--port` options.
"""
from runtime_config import RuntimeConfigurationError, get_runtime_profile

runtime_profile = get_runtime_profile()
if not runtime_profile.api_enabled:
    raise RuntimeConfigurationError(
        f'The HTTP API is disabled for the {runtime_profile.environment} '
        'runtime profile.'
    )

import logging_config  # noqa: E402,F401
from modules.api import server
from modules.utilities import connect

connect()
