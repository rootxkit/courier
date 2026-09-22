"""Relay configuration: valid TOML loads, invalid TOML explains itself.

The reader of these error messages is a pilot on a laptop, not a developer
with a debugger, so "what is wrong and where" matters more than usual.
"""

from __future__ import annotations

import sys
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


# --- files a pilot might actually produce -----------------------------------
#
# TOML was chosen because a pilot edits it. Notepad's "Unicode" save option
# writes UTF-16 with a BOM, which is a plausible way for this file to arrive
# and an implausible thing for anyone to diagnose from a UnicodeDecodeError.


def test_utf16_config_is_refused_with_a_readable_message(tmp_path: Path) -> None:
    path = tmp_path / "relay.toml"
    path.write_text(MINIMAL, encoding="utf-16")

    with pytest.raises(ConfigurationError) as raised:
        load_config(path)

    message = str(raised.value)
    assert "UTF-8" in message
    assert str(path) in message, "the message must say which file"
    assert "Traceback" not in message


def test_utf16_token_is_refused_with_a_readable_message(tmp_path: Path) -> None:
    path = tmp_path / "relay.token"
    path.write_text("secret-value", encoding="utf-16")

    with pytest.raises(ConfigurationError) as raised:
        read_token(path)

    message = str(raised.value)
    assert "UTF-8" in message
    assert str(path) in message


def test_a_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    """Notepad's other option. This one is valid UTF-8 and should just work."""
    path = tmp_path / "relay.toml"
    path.write_text(MINIMAL, encoding="utf-8-sig")

    assert load_config(path).station_id == "tbilisi-base-1"


# --- the plaintext rule -----------------------------------------------------


@pytest.mark.parametrize(
    "host", ["192.168.1.50", "10.0.0.7", "gateway.example.org", "8.8.8.8"]
)
def test_plaintext_to_a_remote_host_is_refused(tmp_path: Path, host: str) -> None:
    """The bearer token is a request header; ws:// puts it on the wire."""
    body = MINIMAL.replace("wss://gateway.example.org/relay/v1", f"ws://{host}:8443")

    with pytest.raises(ConfigurationError) as raised:
        load_config(write(tmp_path, body))

    message = str(raised.value)
    assert "plaintext" in message
    assert "wss://" in message


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]", "127.0.0.5"])
def test_plaintext_to_loopback_is_allowed(tmp_path: Path, host: str) -> None:
    """A relay and a sink on one laptop never put the token on a wire."""
    body = MINIMAL.replace("wss://gateway.example.org/relay/v1", f"ws://{host}:8443")

    assert load_config(write(tmp_path, body)).uses_tls is False


@pytest.mark.parametrize("host", ["192.168.1.50", "gateway.example.org"])
def test_tls_to_a_remote_host_is_allowed(tmp_path: Path, host: str) -> None:
    """The presence half: wss:// to the same hosts is exactly what we want."""
    body = MINIMAL.replace("wss://gateway.example.org/relay/v1", f"wss://{host}:8443")

    assert load_config(write(tmp_path, body)).uses_tls is True


def test_a_utf8_bom_token_does_not_become_an_invisible_prefix(
    tmp_path: Path,
) -> None:
    """A BOM on the token would surface as an unexplained 401 from the Gateway."""
    path = tmp_path / "relay.token"
    path.write_text("secret-value", encoding="utf-8-sig")

    assert read_token(path) == "secret-value"


# --- where relative paths point ---------------------------------------------
#
# Found on the first local run: token_path = "relay.token" resolved against the
# process's working directory, so the relay started from one directory and
# failed from another, with nothing in the message explaining why.


def test_relative_paths_resolve_against_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`token_path = "relay.token"` means the file beside relay.toml."""
    station = tmp_path / "station"
    station.mkdir()
    config_path = station / "relay.toml"
    config_path.write_text(MINIMAL, encoding="utf-8")

    # Start from somewhere else entirely, as a pilot in a shell would.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(config_path)

    assert config.token_path == (station / "relay.token").resolve()
    assert config.queue_path == (station / "relay-queue.sqlite3").resolve()


def test_resolution_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same file read from two directories must give the same answer."""
    station = tmp_path / "station"
    station.mkdir()
    config_path = station / "relay.toml"
    config_path.write_text(MINIMAL, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    from_here = load_config(config_path)
    monkeypatch.chdir(station)
    from_there = load_config(config_path)

    assert from_here.token_path == from_there.token_path
    assert from_here.queue_path == from_there.queue_path


def test_relative_ca_path_resolves_too(tmp_path: Path) -> None:
    station = tmp_path / "station"
    station.mkdir()
    config_path = station / "relay.toml"
    config_path.write_text(MINIMAL + '\nca_path = "dev-ca.crt"\n', encoding="utf-8")

    config = load_config(config_path)

    assert config.ca_path == (station / "dev-ca.crt").resolve()


def test_absolute_paths_are_left_alone(tmp_path: Path) -> None:
    station = tmp_path / "station"
    station.mkdir()
    absolute = (tmp_path / "secrets" / "relay.token").resolve()
    body = MINIMAL.replace(
        'token_path = "relay.token"',
        f"token_path = {str(absolute).replace(chr(92), '/')!r}",
    )
    config_path = station / "relay.toml"
    config_path.write_text(body, encoding="utf-8")

    assert load_config(config_path).token_path == absolute


def test_a_subdirectory_path_resolves_relative_to_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    station = tmp_path / "station"
    station.mkdir()
    config_path = station / "relay.toml"
    config_path.write_text(
        MINIMAL.replace(
            'token_path = "relay.token"', 'token_path = "secrets/relay.token"'
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    config = load_config(config_path)

    assert config.token_path == (station / "secrets" / "relay.token").resolve()


# --- Windows paths in TOML --------------------------------------------------

BACKSLASH = chr(92)


def test_a_backslash_windows_path_is_refused_with_guidance(tmp_path: Path) -> None:
    """The error must name the cause, not just say "invalid escape".

    A pilot pasting a path from Explorer gets backslashes. TOML reads them as
    escape sequences, and tomllib's own message says nothing about paths.
    """
    body = MINIMAL.replace(
        'token_path = "relay.token"',
        f'token_path = "C:{BACKSLASH}Users{BACKSLASH}pilot{BACKSLASH}relay.token"',
    )

    with pytest.raises(ConfigurationError) as raised:
        load_config(write(tmp_path, body))

    message = str(raised.value)
    assert "backslash" in message.lower()
    assert "C:/Users/pilot/relay.token" in message, "must show the forward-slash form"
    assert "single-quoted" in message, "must offer the literal-string form"


def test_a_forward_slash_windows_path_is_accepted(tmp_path: Path) -> None:
    """The point is that it PARSES; the backslash form does not.

    Whether a drive-letter path counts as absolute is the platform's business.
    On Windows it is, and is left alone. On Linux it is not, so it is resolved
    against the configuration file like any other relative path - which is the
    least-wrong answer, since the path cannot exist there either way and the
    resulting error names it in full.
    """
    body = MINIMAL.replace(
        'token_path = "relay.token"', 'token_path = "C:/Users/pilot/relay.token"'
    )

    config = load_config(write(tmp_path, body))

    assert config.token_path.as_posix().endswith("C:/Users/pilot/relay.token")


@pytest.mark.skipif(
    sys.platform != "win32", reason="drive letters are only absolute on Windows"
)
def test_a_drive_letter_path_is_absolute_on_windows(tmp_path: Path) -> None:
    """On the platform a pilot actually uses, it is left exactly as written."""
    body = MINIMAL.replace(
        'token_path = "relay.token"', 'token_path = "C:/Users/pilot/relay.token"'
    )

    config = load_config(write(tmp_path, body))

    assert config.token_path == Path("C:/Users/pilot/relay.token")


def test_a_single_quoted_windows_path_is_accepted(tmp_path: Path) -> None:
    """A TOML literal string takes backslashes exactly as written."""
    literal = f"'C:{BACKSLASH}Users{BACKSLASH}pilot{BACKSLASH}relay.token'"
    body = MINIMAL.replace('token_path = "relay.token"', f"token_path = {literal}")

    config = load_config(write(tmp_path, body))

    assert str(config.token_path).endswith("relay.token")
    assert "Users" in str(config.token_path)


def test_an_unrelated_toml_error_does_not_get_the_windows_hint(
    tmp_path: Path,
) -> None:
    """The hint must not appear on every parse failure, or it becomes noise."""
    with pytest.raises(ConfigurationError) as raised:
        load_config(write(tmp_path, "station_id = = ="))

    assert "backslash" not in str(raised.value).lower()


# --- uplink keepalive -------------------------------------------------------
#
# A half-open link is only detectable from missing pongs, and the time that
# takes is the sum of three settings - including close_timeout, which counts
# because the close handshake waits for a frame a dead link cannot deliver.


def test_keepalive_defaults_bound_detection_at_25_seconds(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL))

    assert config.uplink_ping_interval_s == 10.0
    assert config.uplink_ping_timeout_s == 10.0
    assert config.uplink_close_timeout_s == 5.0
    assert config.worst_case_detection_s == 25.0


def test_the_worst_case_is_the_sum_of_all_three(tmp_path: Path) -> None:
    """close_timeout is the term most easily forgotten, so it is asserted."""
    body = MINIMAL + (
        "\nuplink_ping_interval_s = 4.0\n"
        "uplink_ping_timeout_s = 3.0\n"
        "uplink_close_timeout_s = 2.0\n"
    )

    assert load_config(write(tmp_path, body)).worst_case_detection_s == 9.0


def test_keepalive_that_outlasts_the_operator_alert_is_refused(
    tmp_path: Path,
) -> None:
    """The websockets defaults, which is what the relay used to ship with.

    20 + 20 + 10 = 50 s. The operator would be told the link was down while
    the relay still believed it was healthy.
    """
    body = MINIMAL + (
        "\nuplink_ping_interval_s = 20.0\n"
        "uplink_ping_timeout_s = 20.0\n"
        "uplink_close_timeout_s = 10.0\n"
    )

    with pytest.raises(ConfigurationError) as raised:
        load_config(write(tmp_path, body))

    message = str(raised.value)
    assert "50s" in message
    assert "30s" in message
    assert "still believe" in message


def test_exactly_the_alert_threshold_is_refused(tmp_path: Path) -> None:
    """At 30 s the two events race; below it the ordering is guaranteed."""
    body = MINIMAL + (
        "\nuplink_ping_interval_s = 15.0\n"
        "uplink_ping_timeout_s = 10.0\n"
        "uplink_close_timeout_s = 5.0\n"
    )

    with pytest.raises(ConfigurationError, match=r"30s"):
        load_config(write(tmp_path, body))


def test_just_under_the_threshold_is_accepted(tmp_path: Path) -> None:
    """The presence half: a longer-than-default budget is allowed if bounded."""
    body = MINIMAL + (
        "\nuplink_ping_interval_s = 14.0\n"
        "uplink_ping_timeout_s = 10.0\n"
        "uplink_close_timeout_s = 5.0\n"
    )

    assert load_config(write(tmp_path, body)).worst_case_detection_s == 29.0


@pytest.mark.parametrize(
    "field",
    ["uplink_ping_interval_s", "uplink_ping_timeout_s", "uplink_close_timeout_s"],
)
def test_a_zero_keepalive_setting_is_refused(tmp_path: Path, field: str) -> None:
    """Zero would disable pings entirely, and a dead link would never be seen."""
    with pytest.raises(ConfigurationError, match=field):
        load_config(write(tmp_path, MINIMAL + f"\n{field} = 0\n"))
