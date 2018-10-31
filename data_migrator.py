from peewee import *
from playhouse.postgres_ext import *
import settings
import modules.models as models
import logging
from logging.handlers import RotatingFileHandler

handler = RotatingFileHandler(filename='discord.log', encoding='utf-8', maxBytes=500 * 1024, backupCount=1)
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))

my_logger = logging.getLogger('polybot')
my_logger.setLevel(logging.DEBUG)
my_logger.addHandler(handler)  # root handler for app. module-specific loggers will inherit this

logger_peewee = logging.getLogger('peewee')
logger_peewee.setLevel(logging.DEBUG)

if (logger_peewee.hasHandlers()):
    logger_peewee.handlers.clear()

logger_peewee.addHandler(handler)

logger = logging.getLogger('polybot.' + __name__)


# db = PostgresqlDatabase(settings.psql_db, user=settings.psql_user)
db = PostgresqlDatabase('polytopia_dev2', user=settings.psql_user)

# matches with games:
# match.host > game.host
# match.notes > game.notes

# matchside position - will need to go back and set for even old non-match games

matches = models.Match.select().where(
    (models.Match.game.is_null(False)) & (models.Match.host.is_null(False))
)
for m in matches:
    # print(m)
    m.game.host = m.host
    m.game.notes = m.notes
    m.game.expiration = m.expiration
    m.game.save()
