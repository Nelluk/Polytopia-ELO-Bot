from playhouse.migrate import *
import settings
import modules.models as models

# http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#schema-migrations

db = PostgresqlDatabase(settings.psql_db, user=settings.psql_user)
migrator = PostgresqlMigrator(db)

host = ForeignKeyField(models.Player, field=models.Player.id, null=True, backref='hosting', on_delete='SET NULL')
expiration = DateTimeField(null=True)  # For pending/matchmaking status
notes = TextField(null=True)
is_pending = BooleanField(default=False)

sidename = TextField(null=True)
size = SmallIntegerField(null=False, default=1)
position = SmallIntegerField(null=False, unique=False, default=1)

migrate(
    migrator.add_column('game', 'host_id', host),
    migrator.add_column('game', 'expiration', expiration),
    migrator.add_column('game', 'notes', notes),
    migrator.add_column('game', 'is_pending', is_pending),

    migrator.rename_table('squadgame', 'gameside'),
    migrator.rename_column('lineup', 'squadgame_id', 'gameside_id'),

    migrator.add_column('gameside', 'sidename', sidename),
    migrator.add_column('gameside', 'size', size),
    migrator.add_column('gameside', 'position', position),
    # migrator.add_index('gameside', ('game_id', 'position'), True),  # Will need to re-enable this once data is migrated, and change field to non-nullable

    migrator.drop_not_null('gameside', 'team_id'),

)
