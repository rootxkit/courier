"""The console entry point, end to end through main().

`python -m agent` is the only thing a pilot ever types, and until now nothing
executed it. The refusal paths matter most: someone reading these messages is
on a laptop at a flying site, not at a debugger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.__main__ import main

VALID = """
station_id = "cli-test"
gateway_url = "wss://gateway.example.org/relay/v1"
token_path = "relay.token"
queue_path = "relay-queue.sqlite3"
"""


def capture(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """Return the JSON log lines main() emitted."""
    out = capsys.readouterr()
    return [
        json.loads(line)
        for line in (out.out + out.err).splitlines()
        if line.startswith("{")
    ]


def test_missing_config_exits_two_with_a_readable_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--config", str(tmp_path / "absent.toml")])

    assert code == 2
    records = capture(capsys)
    assert records, "nothing was logged"
    assert records[-1]["message"] == "cannot start"
    assert "relay.example.toml" in str(records[-1]["reason"])


def test_malformed_config_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "relay.toml"
    config.write_text("station_id = = =", encoding="utf-8")

    assert main(["--config", str(config)]) == 2
    assert "not valid TOML" in str(capture(capsys)[-1]["reason"])


def test_missing_token_file_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configuration is fine; the credential it points at is not."""
    config = tmp_path / "relay.toml"
    config.write_text(VALID, encoding="utf-8")

    assert main(["--config", str(config)]) == 2
    assert "Gateway operator" in str(capture(capsys)[-1]["reason"])


def test_plaintext_to_a_remote_host_is_refused_at_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The relay will not send a bearer token across a LAN in the clear.

    This is the rule the sink mirrors. Checked here through main() rather than
    against the validator alone, because a rule that never reaches the entry
    point protects nothing.
    """
    config = tmp_path / "relay.toml"
    config.write_text(
        VALID.replace("wss://gateway.example.org", "ws://192.168.1.50:8443"),
        encoding="utf-8",
    )

    assert main(["--config", str(config)]) == 2

    reason = str(capture(capsys)[-1]["reason"])
    assert "plaintext" in reason
    assert "wss://" in reason


def test_log_level_is_honoured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--log-level ERROR must still show the reason the relay would not start."""
    code = main(["--config", str(tmp_path / "absent.toml"), "--log-level", "ERROR"])

    assert code == 2
    assert capture(capsys)[-1]["level"] == "ERROR"


def test_the_default_config_path_is_relay_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m agent` with no arguments looks for relay.toml here."""
    monkeypatch.chdir(tmp_path)

    assert main([]) == 2
    assert "relay.toml" in str(capture(capsys)[-1]["reason"])
