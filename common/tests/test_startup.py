"""start_service wires configuration to logging, in that order."""

from __future__ import annotations

import json

import pytest

from common.config import ConfigurationError, ServiceSettings
from common.startup import start_service


class ExampleSettings(ServiceSettings):
    service_name: str = "example"


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COURIER_ENV", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_returns_settings_and_announces_the_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, log = start_service(ExampleSettings)

    assert settings.service_name == "example"

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["service"] == "example"
    assert payload["message"] == "service starting"
    assert payload["env"] == "dev"

    # The returned logger carries the context, so callers need not repeat it.
    log.info("ready")
    assert json.loads(capsys.readouterr().out.strip())["env"] == "dev"


def test_invalid_configuration_stops_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "CHATTY")

    with pytest.raises(ConfigurationError):
        start_service(ExampleSettings)


def test_configured_level_is_applied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    _, log = start_service(ExampleSettings)
    capsys.readouterr()

    log.info("suppressed")
    assert capsys.readouterr().out == ""

    log.error("surfaced")
    assert json.loads(capsys.readouterr().out.strip())["message"] == "surfaced"
