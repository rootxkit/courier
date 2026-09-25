"""The guards that stop a test reaching data it did not create.

These exist because on 2026-09-24 the retention tests marked 14,288
`archive_segments` rows deleted on a live station, and nothing failed. The
index rows were real; only the file paths were wrong. Had the archive root
matched, recorded flight data would have gone.

A guard nobody has watched reject anything is a guard nobody knows works, so
each one is pointed at a bad value here and asserted to refuse. They need no
database and no stack: they are the cheapest tests in the suite and the ones
whose absence is most expensive.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gateway.tests.conftest import (
    TEST_DATABASE_SUFFIX,
    UnsafeTestTargetError,
    database_name,
    require_temporary_path,
    require_test_database_url,
    with_database,
)

PRODUCTION = "postgresql+asyncpg://courier:pw@127.0.0.1:5433/courier_telemetry"
TEST = "postgresql+asyncpg://courier:pw@127.0.0.1:5433/courier_telemetry_test"


# --- the database guard ----------------------------------------------------


def test_the_development_database_is_refused() -> None:
    """The exact URL that caused the incident.

    `courier_telemetry` is the database the Gateway writes real telemetry to.
    """
    with pytest.raises(UnsafeTestTargetError, match="must end in"):
        require_test_database_url(PRODUCTION)


def test_a_test_database_is_accepted() -> None:
    """The paired presence test: the guard must not refuse everything."""
    assert require_test_database_url(TEST) == TEST


@pytest.mark.parametrize(
    "name",
    [
        "courier_telemetry",
        "postgres",
        "courier_telemetry_testing",  # ends in "testing", not "_test"
        "test_courier",  # the suffix is the rule, not the prefix
        "",
    ],
)
def test_names_that_are_not_test_databases_are_refused(name: str) -> None:
    with pytest.raises(UnsafeTestTargetError):
        require_test_database_url(with_database(TEST, name))


def test_the_refusal_says_which_database_and_why() -> None:
    """Someone hitting this at 3am needs the reason, not just a stack trace."""
    with pytest.raises(UnsafeTestTargetError) as caught:
        require_test_database_url(PRODUCTION)

    message = str(caught.value)
    assert "courier_telemetry" in message
    assert TEST_DATABASE_SUFFIX in message
    # The incident is named, so the rule does not look arbitrary.
    assert "14,288" in message


def test_the_guard_refuses_rather_than_skips() -> None:
    """A skip is how a guard gets quietly disabled.

    If this raised `pytest.skip.Exception`, a misconfigured run would report
    green with every database test silently absent.
    """
    with pytest.raises(UnsafeTestTargetError) as caught:
        require_test_database_url(PRODUCTION)

    assert not isinstance(caught.value, pytest.skip.Exception)


def test_the_database_name_is_read_from_the_url() -> None:
    assert database_name(TEST) == "courier_telemetry_test"
    assert database_name(PRODUCTION) == "courier_telemetry"


# --- the archive guard -----------------------------------------------------


def test_a_real_archive_root_is_refused() -> None:
    """The root the Gateway actually writes to during a hardware run."""
    with pytest.raises(UnsafeTestTargetError, match="outside"):
        require_temporary_path(Path("local/e2e/archive"))


def test_a_temporary_path_is_accepted(tmp_path: Path) -> None:
    """The paired presence test."""
    accepted = require_temporary_path(tmp_path / "archive")
    assert accepted == (tmp_path / "archive").resolve()


def test_dotdot_segments_are_resolved_before_the_check(tmp_path: Path) -> None:
    """The comparison is on the resolved path, not the string as written.

    The first version of this test asserted that a `..`-laden path would be
    *refused*, and it was wrong: three levels up from pytest's tmp_path still
    lands inside the system temporary directory, so accepting it is correct.
    What matters is that the path is normalised before the check and returned
    normalised, so whatever later writes or deletes through it goes to the
    place the guard actually inspected.
    """
    winding = tmp_path / "somewhere" / ".." / "archive"

    accepted = require_temporary_path(winding)

    assert accepted == (tmp_path / "archive").resolve()
    assert ".." not in accepted.parts


def test_the_repository_itself_is_refused() -> None:
    """A root under the working tree is a real one, not a scratch directory."""
    with pytest.raises(UnsafeTestTargetError):
        require_temporary_path(Path.cwd() / "archive")


def test_the_refusal_names_the_path_and_the_temporary_directory() -> None:
    with pytest.raises(UnsafeTestTargetError) as caught:
        require_temporary_path(Path.cwd() / "archive")

    message = str(caught.value)
    assert str(Path(tempfile.gettempdir()).resolve()) in message
    assert "flight data" in message
