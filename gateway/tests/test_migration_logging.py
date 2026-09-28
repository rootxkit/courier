"""Running the migrations must not silence the Gateway's loggers.

`alembic`'s env.py configures logging with `fileConfig`, which by default
disables every logger that already exists. The database fixture runs the
migrations in-process, so for the rest of a test session `gateway.*` wrote
nothing - and a test asserting that nothing was logged passed whatever the
code under test did. Found when two log-capturing tests failed only when run
after the database tests.
"""

from __future__ import annotations

import logging

import pytest

import gateway.relay_server  # noqa: F401 - creates the logger before migrating

pytestmark = pytest.mark.postgres


def test_migrating_leaves_existing_gateway_loggers_enabled(
    prepared_database: str,
) -> None:
    """`prepared_database` has run the migrations by the time this body runs;
    the logger existed before that, because the import above created it."""
    logger = logging.getLogger("gateway.relay_server")

    assert logger.disabled is False
