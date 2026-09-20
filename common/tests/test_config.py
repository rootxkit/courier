"""Configuration is validated at startup, or the process does not start."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.config import (
    ConfigurationError,
    Environment,
    LogLevel,
    NatsSettings,
    PostgresSettings,
    RedisSettings,
    ServiceSettings,
    load_settings,
)

VALID_ENVIRONMENT = {
    "DATABASE_URL": "postgresql+asyncpg://courier:s3cret@db:5432/courier",
    "REDIS_URL": "redis://cache:6379/0",
    "NATS_URL": "nats://bus:4222",
}


class ExampleSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    service_name: str = "example"


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit the developer's shell.

    Every call below also passes `_env_file=None`, so a .env sitting in the
    repository root cannot make a test pass that would otherwise fail.
    """
    for name in (*VALID_ENVIRONMENT, "COURIER_ENV", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_valid_environment_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, **VALID_ENVIRONMENT)

    settings = load_settings(ExampleSettings, _env_file=None)

    assert settings.service_name == "example"
    assert settings.env is Environment.DEV
    assert settings.log_level is LogLevel.INFO
    assert str(settings.redis_url) == "redis://cache:6379/0"


def test_missing_variable_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, REDIS_URL="redis://cache:6379/0", NATS_URL="nats://bus:4222")

    with pytest.raises(ConfigurationError) as raised:
        load_settings(ExampleSettings, _env_file=None)

    message = str(raised.value)
    assert "ExampleSettings" in message
    assert "database_url" in message.lower()


def test_malformed_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, **{**VALID_ENVIRONMENT, "DATABASE_URL": "not-a-url"})

    with pytest.raises(ConfigurationError):
        load_settings(ExampleSettings, _env_file=None)


def test_wrong_scheme_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis URL in the NATS slot is a plausible copy-paste, so it must fail."""
    _set(monkeypatch, **{**VALID_ENVIRONMENT, "NATS_URL": "redis://bus:4222"})

    with pytest.raises(ConfigurationError):
        load_settings(ExampleSettings, _env_file=None)


def test_settings_are_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuration that changes under a running process is irreproducible."""
    _set(monkeypatch, **VALID_ENVIRONMENT)
    settings = load_settings(ExampleSettings, _env_file=None)

    with pytest.raises(ValidationError):
        settings.service_name = "renamed"


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_development_credentials_are_refused_outside_dev(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """The example .env must not be able to start a real deployment."""
    _set(
        monkeypatch,
        **{
            **VALID_ENVIRONMENT,
            "DATABASE_URL": "postgresql+asyncpg://courier:courier_dev@db:5432/courier",
            "COURIER_ENV": env,
        },
    )

    with pytest.raises(ConfigurationError) as raised:
        load_settings(ExampleSettings, _env_file=None)

    assert "database_url" in str(raised.value)


def test_development_credentials_are_allowed_in_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(
        monkeypatch,
        **{
            **VALID_ENVIRONMENT,
            "DATABASE_URL": "postgresql+asyncpg://courier:courier_dev@db:5432/courier",
            "COURIER_ENV": "dev",
        },
    )

    assert load_settings(ExampleSettings, _env_file=None).env is Environment.DEV


def test_log_level_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, **{**VALID_ENVIRONMENT, "LOG_LEVEL": "CHATTY"})

    with pytest.raises(ConfigurationError):
        load_settings(ExampleSettings, _env_file=None)


def test_unrelated_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service must not refuse to start because of its neighbours' config."""
    _set(monkeypatch, **VALID_ENVIRONMENT, TELEMETRY_DATABASE_URL="postgresql://x/y")

    assert load_settings(ExampleSettings, _env_file=None).service_name == "example"
