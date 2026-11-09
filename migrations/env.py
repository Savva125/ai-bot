from alembic import context

from creator_desk.models import Base


def run():
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("Client migrations require an explicit application connection")
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


run()
