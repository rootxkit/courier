"""Database fixtures for the API: the relational database, and the telemetry one.

The relational fixtures follow the telemetry ones in `gateway/tests/conftest.py`
exactly, including the guard: the database name must end in `_test`, the
fixture creates and drops it, and it refuses rather than skips anything else.
The telemetry fixtures are imported from there, not copied, so the two cannot
drift apart.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gateway.tests.conftest import (  # noqa: F401 - fixtures, used by name
    MAINTENANCE_DATABASE,
    database_name,
    engine,
    prepared_database,
    require_test_database_url,
    test_database_url,
    with_database,
)

RELATIONAL_TEST_DATABASE_ENV = "RELATIONAL_TEST_DATABASE_URL"
RELATIONAL_ALEMBIC_INI = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "migrations"
    / "relational"
    / "alembic.ini"
)


@pytest.fixture(scope="session")
def relational_test_database_url() -> str:
    url = os.environ.get(RELATIONAL_TEST_DATABASE_ENV)
    if not url:
        pytest.skip(
            f"{RELATIONAL_TEST_DATABASE_ENV} is not set; `make test-db` sets it "
            f"and `make up` starts the server"
        )
    return require_test_database_url(url)


@pytest.fixture(scope="session")
async def prepared_relational_database(
    relational_test_database_url: str,
) -> AsyncIterator[str]:
    url = relational_test_database_url
    name = database_name(url)
    admin_url = with_database(url, MAINTENANCE_DATABASE)

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
            await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    await asyncio.to_thread(migrate_relational, url, "head")
    try:
        yield url
    finally:
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as connection:
                await connection.execute(
                    sa.text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": name},
                )
                await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            await admin.dispose()


@pytest.fixture
async def relational_engine(
    prepared_relational_database: str,
) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(prepared_relational_database)
    try:
        yield created
    finally:
        await created.dispose()


def migrate_relational(url: str, target: str, *, down: bool = False) -> None:
    """Run the relational migrations, the ones production runs."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(RELATIONAL_ALEMBIC_INI))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        if down:
            command.downgrade(config, target)
        else:
            command.upgrade(config, target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
