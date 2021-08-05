from playhouse.migrate import *
import settings
from playhouse.postgres_ext import *
import logging
from logging.handlers import RotatingFileHandler
import modules.models as models

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

# db = PostgresqlExtDatabase(settings.psql_db, user=settings.psql_user, autoconnect=True)
db = models.db
migrator = PostgresqlMigrator(db)
db.connect(reuse_if_open=True)

# query = GameLog.select().where(GameLog.game_id > 0)
# for gl in query:
#     gl.message = f'__{str(gl.game_id)}__ - {gl.message}'
#     gl.save()

# is_ranked = BooleanField(default=True)
# elo_max = SmallIntegerField(default=1000)
# is_banned = BooleanField(default=False)
# required_role_id = BitField(default=None, null=True)
# timezone_offset = SmallIntegerField(default=None, null=True)
# win_claimed_ts = DateTimeField(null=True, default=None)
# win_confirmed = BooleanField(default=False)
# elo_after_game = SmallIntegerField(default=None, null=True)
# elo_change_team_alltime = SmallIntegerField(default=0)
# elo_alltime = SmallIntegerField(default=1000)
# game_chan = BitField(default=None, null=True)
# pro_league = BooleanField(default=True)
# date_polychamps_invite_sent = DateField(default=None, null=True)
# external_server = BitField(unique=False, null=True)
# team_chan_external_server = BitField(unique=False, null=True, default=None)
# tribe_direct = ForeignKeyField(Tribe, null=True, on_delete='SET NULL', field=Tribe.id)
# emoji = TextField(null=False, default='')
# elo_after_game_global = SmallIntegerField(default=None, null=True)
# team_elo_after_game = SmallIntegerField(default=None, null=True)
# team_elo_after_game_alltime = SmallIntegerField(default=None, null=True)
# size = ArrayField(SmallIntegerField, default=0)
# game_id = SmallIntegerField(null=True, default=12587)
# is_protected = BooleanField(default=False)
# name_steam = TextField(unique=False, null=True)
# is_mobile = BooleanField(default=True)
# name = TextField(null=False, default='')
# boost_level = SmallIntegerField(default=None, null=True)

# elo_alltime = SmallIntegerField(default=1000)  # x2
# elo_max_alltime = SmallIntegerField(default=1000)  # x2

# elo_change_player_alltime = SmallIntegerField(default=0)
# elo_change_discordmember_alltime = SmallIntegerField(default=0)

# elo_after_game_alltime = SmallIntegerField(default=None, null=True)  # snapshot of what local alltime elo was after game concluded
# elo_after_game_global_alltime = SmallIntegerField(default=None, null=True)

# elo_moonrise = SmallIntegerField(default=1000)
# elo_max_moonrise = SmallIntegerField(default=1000)

# elo_change_player_moonrise = SmallIntegerField(default=0)
# elo_change_discordmember_moonrise = SmallIntegerField(default=0)

# elo_after_game_moonrise = SmallIntegerField(default=None, null=True)
# elo_after_game_global_moonrise = SmallIntegerField(default=None, null=True)

# trophies = ArrayField(CharField, null=True)
trophies = BinaryJSONField(null=True, default=None)


migrate(
    # migrator.add_column('discordmember', 'elo_max', elo_max),
    # migrator.add_column('player', 'is_banned', is_banned),
    # migrator.add_column('discordmember', 'is_banned', is_banned),
    # migrator.add_column('gameside', 'required_role_id', required_role_id)
    # migrator.add_column('discordmember', 'timezone_offset', timezone_offset),
    # migrator.add_column('game', 'win_claimed_ts', win_claimed_ts),
    # migrator.add_column('gameside', 'win_confirmed', win_confirmed)
    # migrator.add_column('gameside', 'elo_change_team_alltime', elo_change_team_alltime),
    # migrator.add_column('team', 'elo_alltime', elo_alltime)
    # migrator.add_column('discordmember', 'date_polychamps_invite_sent', date_polychamps_invite_sent)
    # migrator.add_column('gameside', 'team_chan_external_server', external_server),
    # migrator.add_column('team', 'external_server', external_server)
    # migrator.add_column('lineup', 'tribe_direct_id', tribe_direct)
    # migrator.drop_column('tribe', 'emoji'),
    # migrator.add_column('lineup', 'elo_after_game_global', elo_after_game_global),
    # migrator.add_column('gameside', 'team_elo_after_game', team_elo_after_game),
    # migrator.add_column('gameside', 'team_elo_after_game_alltime', team_elo_after_game_alltime)
    # migrator.add_column('gamelog', 'is_protected', is_protected),
    # migrator.add_column('discordmember', 'name_steam', name_steam),
    # migrator.add_column('squad', 'name', name)
    # migrator.drop_column('gamelog', 'game_id'),
    # migrator.alter_column_type('gamelog', 'game_id', ForeignKeyField(Game))
    # migrator.drop_constraint('gamelog', 'gamelog_game_id_fkey')
    # migrator.add_column('discordmember', 'elo_moonrise', elo_moonrise),
    # migrator.add_column('player', 'elo_moonrise', elo_moonrise),

    # migrator.add_column('discordmember', 'elo_max_moonrise', elo_max_moonrise),
    # migrator.add_column('player', 'elo_max_moonrise', elo_max_moonrise),

    # migrator.add_column('lineup', 'elo_change_player_moonrise', elo_change_player_moonrise),
    # migrator.add_column('lineup', 'elo_change_discordmember_moonrise', elo_change_discordmember_moonrise),
    # migrator.add_column('lineup', 'elo_after_game_moonrise', elo_after_game_moonrise),
    # migrator.add_column('lineup', 'elo_after_game_global_moonrise', elo_after_game_global_moonrise),

    migrator.drop_column('discordmember', 'trophies'),
    migrator.drop_column('player', 'trophies'),
    migrator.add_column('discordmember', 'trophies', trophies),
    migrator.add_column('player', 'trophies', trophies),

)

print('done')
