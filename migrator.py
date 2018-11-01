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

expiration = DateTimeField(null=True)  # For pending/matchmaking status
notes = TextField(null=True)
is_pending = BooleanField(default=False)

sidename = TextField(null=True)
size = SmallIntegerField(null=False, default=1)
position = SmallIntegerField(null=False, unique=False, default=1)

migrate(
    migrator.add_column('game', 'expiration', expiration),
    migrator.add_column('game', 'notes', notes),
    migrator.add_column('game', 'is_pending', is_pending),

    migrator.rename_table('squadgame', 'gameside'),
    migrator.rename_column('lineup', 'squadgame_id', 'gameside_id'),

    migrator.add_column('gameside', 'sidename', sidename),
    migrator.add_column('gameside', 'size', size),
    migrator.add_column('gameside', 'position', position),

    migrator.drop_not_null('gameside', 'team_id'),

)

import modules.models as models
host = ForeignKeyField(models.Player, field=models.Player.id, null=True, backref='hosting', on_delete='SET NULL')

migrate(
    migrator.add_column('game', 'host_id', host),
    # migrator.add_index('gameside', ('game_id', 'position'), True)  # Will need to re-enable this once data is migrated, and change field to non-nullable
)

matches = models.Match.select().where(
    (models.Match.game.is_null(False)) & (models.Match.host.is_null(False))
)
with db:
    for m in matches:
        # print(m)
        m.game.host = m.host
        m.game.notes = m.notes
        m.game.expiration = m.expiration
        m.game.save()

    matches = models.Match.select().where(
        (models.Match.game.is_null(True)) & (models.Match.is_started == 0)
    )
    for m in matches:
        with db.atomic():
            opengame = models.Game.create(host=m.host, expiration=m.expiration, notes=m.notes, guild_id=m.guild_id, is_pending=True)
            count = 0
            for side in m.sides:
                newside = models.GameSide.create(game=opengame, size=side.size, position=count + 1)
                count += 1
                for sp in side.sideplayers:
                    models.Lineup.create(player=sp.player, game=opengame, gameside=newside)
            print(f'match {m.id} converted to game {opengame.id}')

    games = models.Game.select().where(models.Game.is_pending == 0)

    for g in games:
        count = 0
        for side in g.gamesides:
            side.position = count + 1
            side.save()
            print(f'Side {side.id} saved as position {side.position}')
            count += 1
        g.save()

migrate(
    migrator.add_not_null('gameside', 'position'),
    migrator.add_index('gameside', ('game_id', 'position'), True)  # Will need to re-enable this once data is migrated, and change field to non-nullable
)
