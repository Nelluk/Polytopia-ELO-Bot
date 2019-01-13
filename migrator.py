from playhouse.migrate import *
import settings
from playhouse.postgres_ext import *
import logging
from logging.handlers import RotatingFileHandler

# http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#schema-migrations
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

db = PostgresqlDatabase(settings.psql_db, user=settings.psql_user)
migrator = PostgresqlMigrator(db)

# is_ranked = BooleanField(default=True)
# elo_max = SmallIntegerField(default=1000)
# is_banned = BooleanField(default=False)
required_role_id = BitField(default=None, null=True)

migrate(
    # migrator.add_column('discordmember', 'elo_max', elo_max),
    # migrator.add_column('player', 'is_banned', is_banned),
    # migrator.add_column('discordmember', 'is_banned', is_banned),
    migrator.add_column('gameside', 'required_role_id', required_role_id)


)
