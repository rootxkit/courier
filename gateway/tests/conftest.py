"""Isolation for the database-backed tests.

**A test must not be able to reach a database or an archive it did not
create.** This is not a tidiness rule; it is here because of what happened on
2026-09-24.

The retention tests ran against the shared development database and applied a
12 KiB size ceiling - sized for a handful of synthetic segments - to a live
station holding 3.3 MB from a hardware run. 14,288 `archive_segments` rows were
marked deleted across successive runs. Nothing failed, because deleting a
segment whose file is already gone is deliberately tolerated so the sweep can
be re-run after a crash.

**The index rows were real. Only the file paths were wrong.** Had the tests'
archive root matched the real one, they would have deleted recorded flight
data, and still nothing would have failed.

So:

- the database name must end in `_test`, and the fixture **fails hard** rather
  than skipping if it does not - a skip is how a guard gets quietly disabled;
- the database is created and dropped by the fixture, so it cannot be one
  somebody is using;
- archive roots must live under a temporary directory, checked by resolved
  path rather than by trusting the caller.

`tests/test_isolation_guards.py` points the guards at bad values and asserts
they refuse. A guard nobody has seen reject anything is a guard nobody knows
works.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# The suffix a database must carry before any test is allowed to touch it.
TEST_DATABASE_SUFFIX = "_test"

# Read instead of TELEMETRY_DATABASE_URL, so pointing a shell at the
# development database cannot start a test run against it by accident.
TEST_DATABASE_ENV = "TELEMETRY_TEST_DATABASE_URL"

# Connected to in order to CREATE and DROP the test database, since a session
# cannot drop the database it is connected to.
MAINTENANCE_DATABASE = "postgres"


class UnsafeTestTargetError(RuntimeError):
    """A test was about to touch something it did not create."""


def require_test_database_url(url: str) -> str:
    """Return `url`, or refuse if it does not name a test database.

    Refuses rather than skips. A guard that skips when it is unhappy is
    indistinguishable from a guard that is switched off.
    """
    name = database_name(url)
    if not name.endswith(TEST_DATABASE_SUFFIX):
        raise UnsafeTestTargetError(
            f"refusing to run tests against database {name!r}: the name must "
            f"end in {TEST_DATABASE_SUFFIX!r}. Tests create and drop their own "
            f"database; pointing them at a real one has already marked 14,288 "
            f"archive rows deleted once."
        )
    return url


def require_temporary_path(path: Path) -> Path:
    """Return `path`, or refuse if it is not inside a temporary directory.

    Compared on the resolved path, so a symlink or a `..` cannot smuggle a
    real archive root past it.
    """
    resolved = path.resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if not resolved.is_relative_to(temporary):
        raise UnsafeTestTargetError(
            f"refusing to use {resolved} as a test archive root: it is outside "
            f"{temporary}. Archive tests write and delete segments, and a real "
            f"root here would delete recorded flight data."
        )
    return resolved


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def configured_test_database_url() -> str | None:
    """The test database URL, validated, or None when tests cannot run."""
    url = os.environ.get(TEST_DATABASE_ENV)
    if not url:
        return None
    return require_test_database_url(url)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = configured_test_database_url()
    if url is None:
        pytest.skip(
            f"{TEST_DATABASE_ENV} is not set; `make test-db` sets it and "
            f"`make up` starts the server"
        )
    return url


@pytest.fixture(scope="session")
async def prepared_database(test_database_url: str) -> AsyncIterator[str]:
    """Create the test database, migrate it, and drop it afterwards.

    Dropped at the end so a failing run cannot leave state that makes the next
    one pass, and so the database can never be mistaken for one in use.
    """
    name = database_name(test_database_url)
    admin_url = with_database(test_database_url, MAINTENANCE_DATABASE)

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
            await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    await _create_extensions(test_database_url)
    # On its own thread: alembic's env.py calls asyncio.run(), which cannot
    # run inside the loop this async fixture is already on.
    await asyncio.to_thread(_migrate, test_database_url)

    try:
        yield test_database_url
    finally:
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as connection:
                # Sessions left open by a failed test would block the drop.
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
async def engine(prepared_database: str) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(prepared_database)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    """A guaranteed-temporary archive root.

    Every archive fixture goes through this, so an archive test cannot be
    pointed at a real root even by editing one line.
    """
    return require_temporary_path(tmp_path / "archive")


async def _create_extensions(url: str) -> None:
    """The extensions the migrations need, as initdb would have created them."""
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for extension in ("timescaledb", "postgis", "btree_gist"):
                await connection.execute(
                    sa.text(f"CREATE EXTENSION IF NOT EXISTS {extension}")
                )
    finally:
        await engine.dispose()


def _migrate(url: str) -> None:
    """Run the telemetry migrations against the test database.

    The same migrations production runs. Building the schema any other way
    would test a shape nobody deploys.
    """
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "infra" / "migrations" / "telemetry" / "alembic.ini"))
    previous = os.environ.get("TELEMETRY_DATABASE_URL")
    os.environ["TELEMETRY_DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("TELEMETRY_DATABASE_URL", None)
        else:
            os.environ["TELEMETRY_DATABASE_URL"] = previous
