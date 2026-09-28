"""The relational database fixtures, shared with the API's tests."""

from api.tests.conftest import (  # noqa: F401 - fixtures, used by name
    prepared_relational_database,
    relational_engine,
    relational_test_database_url,
)
