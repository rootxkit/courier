"""The repository layout is a contract, so it is tested like one.

CLAUDE.md documents a fixed set of directories and the tooling configuration
names the same set in three separate places (setuptools packages, mypy files,
pytest testpaths). These tests fail loudly when a package is added to one of
them and forgotten in the others, which is otherwise silent: mypy simply stops
checking the module it was never told about.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The Python packages in the layout documented in CLAUDE.md.
PYTHON_PACKAGES = (
    "common",
    "agent",
    "gateway",
    "api",
    "dispatch",
    "airspace",
)

# Directories that exist but are not Python.
NON_PYTHON_DIRS = ("web-pilot", "app-customer", "infra", "sim", "docs")

# mypy --strict applies to these. CLAUDE.md names gateway, dispatch and
# airspace; common is included because they import it.
STRICT_PACKAGES = ("common", "gateway", "dispatch", "airspace")

# The per-module half of mypy's --strict bundle. The rest of the bundle is set
# globally and so is not repeated in the override.
STRICT_FLAGS = frozenset(
    {
        "disallow_any_generics",
        "disallow_subclassing_any",
        "disallow_untyped_calls",
        "disallow_untyped_defs",
        "disallow_incomplete_defs",
        "check_untyped_defs",
        "disallow_untyped_decorators",
        "warn_return_any",
        "strict_equality",
        "extra_checks",
    }
)


@pytest.fixture(scope="module")
def pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


@pytest.mark.parametrize("package", PYTHON_PACKAGES)
def test_package_is_importable(package: str) -> None:
    assert importlib.import_module(package) is not None


@pytest.mark.parametrize("directory", PYTHON_PACKAGES + NON_PYTHON_DIRS)
def test_documented_directory_exists(directory: str) -> None:
    assert (REPO_ROOT / directory).is_dir()


def test_setuptools_declares_every_python_package(pyproject: dict) -> None:
    declared = pyproject["tool"]["setuptools"]["packages"]
    assert sorted(declared) == sorted(PYTHON_PACKAGES)


def test_mypy_checks_every_python_package(pyproject: dict) -> None:
    checked = pyproject["tool"]["mypy"]["files"]
    assert sorted(checked) == sorted(PYTHON_PACKAGES)


def test_mypy_is_strict_on_the_safety_relevant_packages(pyproject: dict) -> None:
    """The strict bundle must be expanded, and cover exactly those packages.

    mypy's `strict` flag is global: setting it inside a per-module override
    turns strict on everywhere, which is why the flags are listed out. If
    someone collapses them back to `strict = true`, this fails.
    """
    overrides = pyproject["tool"]["mypy"]["overrides"]
    strict_override = next(
        override
        for override in overrides
        if set(override["module"]) == {f"{name}.*" for name in STRICT_PACKAGES}
    )

    assert "strict" not in strict_override, (
        "mypy's `strict` is a global flag; expand the bundle instead"
    )
    assert strict_override.keys() >= STRICT_FLAGS
    assert all(strict_override[flag] is True for flag in STRICT_FLAGS)


def test_coverage_is_measured_on_the_safety_relevant_packages(
    pyproject: dict,
) -> None:
    measured = pyproject["tool"]["coverage"]["run"]["source"]
    assert sorted(measured) == sorted(STRICT_PACKAGES)
