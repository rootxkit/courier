"""Alembic environment for the relational database (PostgreSQL + PostGIS).

The URL comes from `DATABASE_URL`, the same variable the API reads. It is
never written into `alembic.ini`: credentials belong in the environment.

The version table is `alembic_version_relational`, named for this tree, so
pointing it at the telemetry database finds no history and says so rather than
quietly running this tree there. The two trees are never merged (CLAUDE.md).
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
    # disable_existing_loggers=False: fileConfig's default disables every
    # logger that already exists. Run in-process - as the database test
    # fixture does - that silenced `gateway.*` for the rest of the session,
    # so a test asserting that nothing was logged passed whatever the code
    # did. From the alembic CLI it changes nothing: no other logger exists yet.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate is deliberately not wired, as in the telemetry tree: geometry
# columns, GiST indexes and the append-only trigger on `events` are written
# by hand, and a migration that looks right and drops a trigger is worse than
# no autogenerate.
target_metadata = None

VERSION_TABLE = "alembic_version_relational"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy infra/.env.example to "
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
