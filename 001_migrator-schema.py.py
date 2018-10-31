from playhouse.migrate import *
import settings

# http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#schema-migrations

db = PostgresqlDatabase('polytopia_dev2', user=settings.psql_user)
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
