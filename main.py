"""Entry point to run the bot and API.

Too run this, use the following command:
$ python3 -m uvicorn main:server
(Replacing 'python3' with your Python installation).
You can specify the port/address to bind to with the `--host` and
`--port` options.
"""
import asyncio

import bot
from modules.api import server

import sys


@server.on_event('startup')
async def startup():
    """Initialise and connect the Discord client."""
    loop = asyncio.get_running_loop()
    bot.init_bot(loop, sys.argv[2:])
