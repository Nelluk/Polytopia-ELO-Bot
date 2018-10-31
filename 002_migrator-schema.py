from playhouse.migrate import *
import settings
import modules.models as models

# http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#schema-migrations

db = PostgresqlDatabase('polytopia_dev2', user=settings.psql_user)
migrator = PostgresqlMigrator(db)

host = ForeignKeyField(models.Player, field=models.Player.id, null=True, backref='hosting', on_delete='SET NULL')

migrate(
    migrator.add_column('game', 'host_id', host),
    # migrator.add_index('gameside', ('game_id', 'position'), True)  # Will need to re-enable this once data is migrated, and change field to non-nullable
)
