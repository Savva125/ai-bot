from alembic import context
from creator_desk_server.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata,
                  version_table="server_alembic_version")
with context.begin_transaction():
    context.run_migrations()
