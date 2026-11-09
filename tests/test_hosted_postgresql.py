"""Opt-in PostgreSQL row-lock tests, using a disposable isolated schema."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from creator_desk_server.licensing import Licensing
from creator_desk_server.manage import migrate
from creator_desk_server.models import Base
from creator_desk_server.schemas import Activation, CreateLicense


@pytest.mark.skipif(not os.getenv("CREATOR_DESK_TEST_DATABASE_URL"), reason="No PostgreSQL test database configured")
def test_postgresql_migration_and_concurrent_device_and_generation_limits():
    base_url = make_url(os.environ["CREATOR_DESK_TEST_DATABASE_URL"])
    schema = "hosted_test_" + uuid4().hex
    owner = create_engine(base_url)
    assert owner.dialect.name == "postgresql"
    with owner.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(base_url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        migrate(engine)
        with engine.connect() as c:
            assert not compare_metadata(MigrationContext.configure(c, opts={"version_table": "server_alembic_version"}), Base.metadata)
        service = Licensing(sessionmaker(engine, expire_on_commit=False))
        item = service.create(CreateLicense(max_devices=1), "test")
        barrier = Barrier(2)

        def activate(index):
            barrier.wait(timeout=10)
            try:
                return service.activate(Activation(key=item["key"], device_id=str(index) * 32))
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(activate, [1, 2]))
        assert sum(isinstance(r, dict) for r in results) == 1 and 403 in results
        token = next(r["token"] for r in results if isinstance(r, dict))
        barrier = Barrier(2)

        def reserve(index):
            barrier.wait(timeout=10)
            try:
                return service.reserve(token, str(uuid4()), "hash", 180)
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, [1, 2]))
        assert sum(isinstance(r, tuple) for r in results) == 1 and 429 in results
    finally:
        engine.dispose()
        with owner.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        owner.dispose()
