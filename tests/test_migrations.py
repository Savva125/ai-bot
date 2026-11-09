import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import create_engine, inspect, text

from creator_desk.models import Base


def config(connection):
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def verify(engine):
    with engine.begin() as connection:
        cfg = config(connection)
        command.upgrade(cfg, "head")
        assert set(Base.metadata.tables).issubset(inspect(connection).get_table_names())
        assert not compare_metadata(MigrationContext.configure(connection), Base.metadata)
        command.downgrade(cfg, "base")
        assert not set(Base.metadata.tables).intersection(inspect(connection).get_table_names())


def test_migration_round_trip_sqlite(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    try:
        verify(engine)
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("CREATOR_DESK_TEST_DATABASE_URL"), reason="No PostgreSQL test database configured")
def test_migration_postgresql():
    # Opt-in disposable schema, never drop user tables or databases.
    schema = "creator_desk_test_" + uuid4().hex
    engine = create_engine(os.environ["CREATOR_DESK_TEST_DATABASE_URL"])
    assert engine.dialect.name == "postgresql"
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            cfg = config(connection)
            command.upgrade(cfg, "head")
            assert set(Base.metadata.tables).issubset(inspect(connection).get_table_names(schema=schema))
            assert not compare_metadata(MigrationContext.configure(connection), Base.metadata)
            command.downgrade(cfg, "base")
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
