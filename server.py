"""Entry point to run the API server.

Too run this, use the following command:

$ python3 -m uvicorn server:server

(Replacing 'python3' with your Python installation).
You can specify the port/address to bind to with the `--host` and
`--port` options.
"""
from modules.api import server
from modules.utilities import connect

import logging
from logging.handlers import RotatingFileHandler

api_handler = RotatingFileHandler(filename='logs/api.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=5)
api_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
api_logger = logging.getLogger('polybot.api')
api_logger.setLevel(logging.DEBUG)
api_logger.addHandler(api_handler)

connect()
