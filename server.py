"""Entry point to run the API server.

Too run this, use the following command:
$ python3 -m uvicorn main:server
(Replacing 'python3' with your Python installation).
You can specify the port/address to bind to with the `--host` and
`--port` options.
"""
from modules.api import server
