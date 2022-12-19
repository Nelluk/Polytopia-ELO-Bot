import logging
import sys
from logging.handlers import RotatingFileHandler

# Logger config is a bit of a mess and probably could be simplified a lot, but works. debug and above sent to file / error above sent to stderr
handler = RotatingFileHandler(filename='logs/full_bot.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=10)
partial_handler = RotatingFileHandler(filename='logs/discord.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=10)  # without peewee logging
elo_handler = RotatingFileHandler(filename='logs/elo.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=5)
api_handler = RotatingFileHandler(filename='logs/api.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=5)

handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
partial_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
elo_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
api_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))

my_logger = logging.getLogger('polybot')
my_logger.setLevel(logging.DEBUG)
my_logger.addHandler(handler)  # root handler for app. module-specific loggers will inherit this
my_logger.addHandler(partial_handler)

elo_logger = logging.getLogger('polybot.elo')
elo_logger.setLevel(logging.DEBUG)
elo_logger.addHandler(elo_handler)

api_logger = logging.getLogger('polybot.api')
api_logger.setLevel(logging.DEBUG)
api_logger.addHandler(api_handler)

err = logging.StreamHandler(sys.stderr)
err.setLevel(logging.ERROR)
err.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
my_logger.addHandler(err)


discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.INFO)

if (discord_logger.hasHandlers()):
    discord_logger.handlers.clear()

discord_logger.addHandler(handler)
discord_logger.addHandler(partial_handler)

logger_peewee = logging.getLogger('peewee')
logger_peewee.setLevel(logging.DEBUG)

if (logger_peewee.hasHandlers()):
    logger_peewee.handlers.clear()

logger_peewee.addHandler(handler)

