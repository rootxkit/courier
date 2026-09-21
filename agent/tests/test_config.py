"""Relay configuration: valid TOML loads, invalid TOML explains itself.

The reader of these error messages is a pilot on a laptop, not a developer
with a debugger, so "what is wrong and where" matters more than usual.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent.config import RelayConfig, load_config, read_token
from common.config import ConfigurationError

EXAMPLE = Path(__file__).resolve().parent.parent / "relay.example.toml"

MINIMAL = """
station_id = "tbilisi-base-1"
gateway_url = "wss://gateway.example.org/relay/v1"
token_path = "relay.token"
queue_path = "relay-queue.sqlite3"
"""


def write(tmp_path: Path, body: str, name: str = "relay.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_minimal_configuration_loads(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL))

    assert config.station_id == "tbilisi-base-1"
    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 14445
    assert config.uses_tls


def test_the_shipped_example_is_valid() -> None:
    """The file a pilot copies must actually work."""
    config = load_config(EXAMPLE)

    assert config.station_id
    assert config.uses_tls


def test_the_example_documents_every_required_field() -> None:
    """A field absent from the example is a field nobody will know to set."""
    documented = set(tomllib.loads(EXAMPLE.read_text(encoding="utf-8")))
    required = {
        name for name, field in RelayConfig.model_fields.items() if field.is_required()
    }

    assert required <= documented


def test_the_example_sets_nothing_that_is_not_a_field() -> None:
    """A stray key would be silently rejected by extra='forbid' at startup."""
    documented = set(tomllib.loads(EXAMPLE.read_text(encoding="utf-8")))

    assert documented <= set(RelayConfig.model_fields)


def test_optional_fields_appear_as_commented_examples() -> None:
    """ca_path is off by default, but a pilot still has to know it exists.

    Leaving it out entirely would mean the only way to discover it is reading
    the source, which is not something the reader of this file does.
    """
    body = EXAMPLE.read_text(encoding="utf-8")
    documented = set(tomllib.loads(body))

    for name, field in RelayConfig.model_fields.items():
        if field.is_required() or name in documented:
            continue
        assert f"#{name} =" in body, f"{name} is neither set nor shown commented"


def test_configuration_is_frozen(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL))

    with pytest.raises(Exception, match=r"frozen|immutable"):
        config.station_id = "renamed"


def test_missing_file_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"relay\.example\.toml"):
        load_config(tmp_path / "absent.toml")


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not valid TOML"):
        load_config(write(tmp_path, "station_id = = ="))


def test_missing_required_field_names_the_field(tmp_path: Path) -> None:
    body = MINIMAL.replace('station_id = "tbilisi-base-1"\n', "")

    with pytest.raises(ConfigurationError, match="station_id"):
        load_config(write(tmp_path, body))


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    """A typo must fail loudly, not be silently ignored on a pilot's laptop."""
    with pytest.raises(ConfigurationError, match=r"quue_path|extra"):
        load_config(write(tmp_path, MINIMAL + '\nquue_path = "typo.sqlite3"\n'))


def test_bad_url_scheme_is_rejected(tmp_path: Path) -> None:
    body = MINIMAL.replace("wss://gateway.example.org/relay/v1", "https://example.org")

    with pytest.raises(ConfigurationError, match="gateway_url"):
        load_config(write(tmp_path, body))


def test_plain_ws_loads_but_is_flagged_as_insecure(tmp_path: Path) -> None:
    """Tests need ws://; production must not use it, and can tell."""
    body = MINIMAL.replace("wss://gateway.example.org", "ws://localhost:8765")

    assert load_config(write(tmp_path, body)).uses_tls is False


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_out_of_range_port_is_rejected(tmp_path: Path, port: int) -> None:
    with pytest.raises(ConfigurationError, match="bind_port"):
        load_config(write(tmp_path, MINIMAL + f"\nbind_port = {port}\n"))


def test_zero_queue_cap_is_rejected(tmp_path: Path) -> None:
    """An uncapped queue is the P7-11 failure; zero is not a way round it."""
    with pytest.raises(ConfigurationError, match="queue_max_bytes"):
        load_config(write(tmp_path, MINIMAL + "\nqueue_max_bytes = 0\n"))


# --- the token --------------------------------------------------------------


def test_token_is_read_and_stripped(tmp_path: Path) -> None:
    path = tmp_path / "relay.token"
    path.write_text("  secret-value\n", encoding="utf-8")

    assert read_token(path) == "secret-value"


def test_missing_token_explains_where_it_comes_from(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Gateway operator"):
        read_token(tmp_path / "relay.token")


def test_empty_token_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "relay.token"
    path.write_text("   \n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="empty"):
        read_token(path)


def test_the_example_does_not_contain_a_token() -> None:
    """The reason the token lives in its own file at all."""
    body = EXAMPLE.read_text(encoding="utf-8")
    config = tomllib.loads(body)

    assert "token" not in config
    assert "token_path" in config
