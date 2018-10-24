from playhouse.migrate import *
import settings

# http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#schema-migrations

db = PostgresqlDatabase('polytopia', user=settings.psql_user)
migrator = PostgresqlMigrator(db)

# position = SmallIntegerField(null=False, unique=False, default=1)
# is_started = BooleanField(default=False)
# elo_change_discordmember = SmallIntegerField(default=0)
# is_hidden = BooleanField(default=False)

migrate(
    # migrator.add_column('team', 'is_hidden', is_hidden),
    # migrator.add_index('matchside', ('match_id', 'position'), True),
)
