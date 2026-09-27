"""relay-v1 control messages, server side.

The forward-compatibility rules in §2 and §14 get their own tests in both
directions: an unknown *type* and an unknown *field* must both be tolerated,
and a known message missing a required field must not be.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gateway.relay_messages import (
    ControlMessageError,
    Gap,
    Hello,
    IgnoredMessage,
    Status,
    build_ack,
    build_welcome,
    parse_control_message,
)

EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"

HELLO: dict[str, Any] = {
    "type": "hello",
    "station_id": "tbilisi-base-1",
    "epoch": EPOCH,
    "relay_version": "0.1.0",
    "protocol_version": 1,
    "oldest_seq_held": 41203,
    "newest_seq_held": 58817,
    "monotonic_ns": 992847110000000,
    "utc_ns": 1758412800123456789,
}

STATUS: dict[str, Any] = {
    "type": "status",
    "queue_depth": 1240,
    "queue_bytes": 2310450,
    "dropped_intake_total": 0,
    "dropped_cap_total": 0,
    "last_datagram_age_ms": 38,
    "uptime_s": 7321,
    "monotonic_ns": 992847110000000,
    "utc_ns": 1758412800123456789,
}

GAP: dict[str, Any] = {
    "type": "gap",
    "epoch": EPOCH,
    "from_seq": 58120,
    "to_seq": 61099,
    "reason": "queue_cap",
}


def parse(body: dict[str, Any]) -> Any:
    return parse_control_message(json.dumps(body))


# --- the documented examples parse -----------------------------------------


def test_the_documented_hello_parses() -> None:
    message = parse(HELLO)
    assert isinstance(message, Hello)
    assert message.station_id == "tbilisi-base-1"
    assert message.epoch == EPOCH
    assert message.newest_seq_held == 58817


def test_the_documented_status_parses() -> None:
    message = parse(STATUS)
    assert isinstance(message, Status)
    assert message.queue_depth == 1240
    assert message.last_datagram_age_ms == 38


def test_the_documented_gap_parses_with_an_exclusive_upper_bound() -> None:
    """§11 spells the arithmetic out, so the test does too.

    "With `resume_from_seq = 58120` and `oldest_seq_held = 61099` ... 58120
    through 61098 are gone." That is 2979 records, not 2980.
    """
    message = parse(GAP)
    assert isinstance(message, Gap)
    assert (message.from_seq, message.to_seq) == (58120, 61099)
    assert message.missing_count == 2979


# --- forward compatibility, both directions --------------------------------


def test_an_unknown_message_type_is_ignored_not_rejected() -> None:
    """§14: additive changes must not need a version bump."""
    message = parse_control_message(
        json.dumps({"type": "telemetry_hint", "whatever": [1, 2, 3]})
    )
    assert message == IgnoredMessage(type="telemetry_hint")


def test_unknown_fields_on_a_known_message_are_ignored() -> None:
    message = parse({**STATUS, "cpu_temp_c": 61.5, "future_field": None})
    assert isinstance(message, Status)
    assert message.uptime_s == 7321


def test_a_known_message_missing_a_required_field_is_rejected() -> None:
    """The other half of the rule. Tolerating this would invent a number.

    `dropped_intake_total` is the only evidence an intake drop ever happened
    (§11 loss #2), so defaulting it to zero would silently erase the loss it
    exists to report.
    """
    body = {k: v for k, v in STATUS.items() if k != "dropped_intake_total"}
    with pytest.raises(ControlMessageError, match="dropped_intake_total"):
        parse(body)


# --- field validation ------------------------------------------------------


def test_last_datagram_age_ms_may_be_null() -> None:
    """Null means no datagram has ever arrived, which is not zero.

    Zero would mean one arrived this instant. The distinction was a real
    specification gap found while writing the sink.
    """
    message = parse({**STATUS, "last_datagram_age_ms": None})
    assert isinstance(message, Status)
    assert message.last_datagram_age_ms is None


def test_a_boolean_is_not_accepted_as_an_integer() -> None:
    """`bool` subclasses `int`, so `True` would quietly become 1."""
    with pytest.raises(ControlMessageError, match="must be an integer"):
        parse({**STATUS, "queue_depth": True})


@pytest.mark.parametrize(
    "epoch",
    [
        "9F2C1B7D4E6A58039AB1C2D3E4F50617",  # uppercase
        "9f2c1b7d4e6a58039ab1c2d3e4f5061",  # 31 chars
        "9f2c1b7d4e6a58039ab1c2d3e4f506177",  # 33 chars
        "9f2c1b7d-4e6a-5803-9ab1-c2d3e4f50617",  # hyphenated
    ],
)
def test_a_malformed_epoch_is_rejected(epoch: str) -> None:
    """§4 fixes the shape, and the epoch is half of the dedupe key.

    A station sending a differently-shaped epoch would partition its own
    records under two identities and neither would be complete.
    """
    with pytest.raises(ControlMessageError, match="32 lowercase hex"):
        parse({**HELLO, "epoch": epoch})


def test_a_hello_from_a_future_protocol_version_is_rejected() -> None:
    with pytest.raises(ControlMessageError, match="protocol_version"):
        parse({**HELLO, "protocol_version": 2})


def test_an_empty_queue_reports_newest_one_below_oldest() -> None:
    """§5: with an empty queue, newest_seq_held == oldest_seq_held - 1."""
    message = parse({**HELLO, "oldest_seq_held": 900, "newest_seq_held": 899})
    assert isinstance(message, Hello)
    assert message.newest_seq_held == 899


def test_a_hello_whose_sequence_range_is_impossible_is_rejected() -> None:
    with pytest.raises(ControlMessageError, match="inconsistent"):
        parse({**HELLO, "oldest_seq_held": 900, "newest_seq_held": 800})


def test_a_gap_that_covers_no_records_is_rejected() -> None:
    """`to_seq` is exclusive, so `to_seq == from_seq` describes nothing."""
    with pytest.raises(ControlMessageError, match="covers no records"):
        parse({**GAP, "to_seq": 58120})


def test_a_gap_reason_is_an_open_string() -> None:
    """§11: "an open string so later reasons can be added"."""
    message = parse({**GAP, "reason": "operator_purge"})
    assert isinstance(message, Gap)
    assert message.reason == "operator_purge"


def test_a_frame_that_is_not_json_is_rejected() -> None:
    with pytest.raises(ControlMessageError, match="not valid JSON"):
        parse_control_message("{not json")


def test_a_json_array_is_rejected() -> None:
    with pytest.raises(ControlMessageError, match="expected a JSON object"):
        parse_control_message("[1, 2, 3]")


def test_a_message_with_no_type_is_rejected() -> None:
    with pytest.raises(ControlMessageError, match="no string 'type'"):
        parse_control_message(json.dumps({"station_id": "x"}))


# --- what the server sends -------------------------------------------------


def test_welcome_carries_the_protocol_version_and_resume_point() -> None:
    assert json.loads(build_welcome(58120)) == {
        "type": "welcome",
        "protocol_version": 1,
        "resume_from_seq": 58120,
    }


def test_welcome_for_an_unknown_epoch_is_zero_not_an_error() -> None:
    assert json.loads(build_welcome(0))["resume_from_seq"] == 0


def test_ack_carries_the_epoch() -> None:
    """§7: the epoch is what lets a late ack be discarded.

    Without it, an ack arriving after the relay has started a new epoch would
    delete records it does not describe.
    """
    assert json.loads(build_ack(EPOCH, 58904)) == {
        "type": "ack",
        "epoch": EPOCH,
        "seq": 58904,
    }


def test_an_ack_cannot_be_built_with_a_malformed_epoch() -> None:
    with pytest.raises(ValueError, match="not a relay-v1 epoch"):
        build_ack("nonsense", 1)
