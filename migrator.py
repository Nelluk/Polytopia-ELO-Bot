from playhouse.migrate import *
import settings

# http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#schema-migrations

db = PostgresqlDatabase('polytopia', user=settings.psql_user)
migrator = PostgresqlMigrator(db)

# position = SmallIntegerField(null=False, unique=False, default=1)
# is_started = BooleanField(default=False)

migrate(
    # migrator.add_column('match', 'is_started', is_started),
    # migrator.add_index('matchside', ('match_id', 'position'), True),
)
