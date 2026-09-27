"""Alembic environment for the telemetry database.

The URL comes from `TELEMETRY_DATABASE_URL`, the same variable the Gateway
reads, so a migration cannot be run against a database the service does not
use. It is never written into `alembic.ini`: credentials belong in the
environment, not in the repository.

The version table is `alembic_version_telemetry`, named for this tree. Pointing
this configuration at the relational database would find no such table and
offer to run the whole history against it, which is exactly the accident two
trees are meant to prevent - so the name makes the mistake loud rather than
silent.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate is deliberately not wired to a metadata object. The tables here
# are written by hand: TimescaleDB hypertables, retention policies and
# partial indexes are not things autogenerate produces correctly, and a
# migration that looks right and drops a policy is worse than no autogenerate.
target_metadata = None

VERSION_TABLE = "alembic_version_telemetry"


def _database_url() -> str:
    url = os.environ.get("TELEMETRY_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "TELEMETRY_DATABASE_URL is not set. Copy infra/.env.example to "
            ".env and export it, or pass it on the command line. The URL is "
            "deliberately not stored in alembic.ini."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review before applying."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
