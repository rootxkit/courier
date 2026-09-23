"""relay-v1 control messages, server side.

Written from `docs/protocols/relay-v1.md` §5, §7, §8 and §11.

Two rules from the document shape this module and are easy to get backwards:

- **§2 and §14: unknown JSON `type` values and unknown fields must be ignored,
  not rejected.** That is what lets a newer relay talk to an older Gateway
  within version 1. So an unrecognised `type` yields `IgnoredMessage` rather
  than an error, and extra keys on a known message are dropped silently.
- A *known* message that is missing a required field, or carries one of the
  wrong type, is a protocol error. Ignoring those would mean inventing values
  for fields the Gateway's loss accounting depends on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

PROTOCOL_VERSION: Final = 1

# relay-v1 §4: "string, 32 lowercase hex chars", a random 128-bit value. The
# pattern is enforced because the epoch is half of the dedupe key: a station
# that sent a differently-shaped epoch would partition its own records.
_EPOCH_PATTERN: Final = re.compile(r"\A[0-9a-f]{32}\Z")


class ControlMessageError(ValueError):
    """A known relay-v1 control message is malformed."""


@dataclass(frozen=True, slots=True)
class Hello:
    """relay-v1 §5. Sent once per connection, before any data frame."""

    station_id: str
    epoch: str
    relay_version: str
    protocol_version: int
    oldest_seq_held: int
    newest_seq_held: int
    monotonic_ns: int
    utc_ns: int


@dataclass(frozen=True, slots=True)
class Status:
    """relay-v1 §8. Sent every second, whether or not data is flowing."""

    queue_depth: int
    queue_bytes: int
    dropped_intake_total: int
    dropped_cap_total: int
    # None until a datagram has ever arrived on the relay's UDP socket. This
    # was a specification gap found by writing the sink; it is not the same as
    # zero, which would mean one arrived just now.
    last_datagram_age_ms: int | None
    uptime_s: int
    monotonic_ns: int
    utc_ns: int


@dataclass(frozen=True, slots=True)
class Gap:
    """relay-v1 §11. Records the cap destroyed, which will never arrive.

    `from_seq` is inclusive and `to_seq` is exclusive: the missing records are
    `[from_seq, to_seq)`. Storing this off by one either loses a record from
    the flight history or claims one is missing that is not.
    """

    epoch: str
    from_seq: int
    to_seq: int
    reason: str

    @property
    def missing_count(self) -> int:
        return self.to_seq - self.from_seq


@dataclass(frozen=True, slots=True)
class IgnoredMessage:
    """A control message this Gateway does not know.

    Not an error: §14 requires unknown types to be ignored so that additive
    protocol changes need no version bump. Carried as a value rather than a
    silent `None` so the caller can count them - a rising count means a relay
    is speaking a dialect this Gateway does not, which is worth seeing.
    """

    type: str


ControlMessage = Hello | Status | Gap | IgnoredMessage


def parse_control_message(payload: str) -> ControlMessage:
    """Parse one text frame from a relay."""
    try:
        decoded: Any = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ControlMessageError(f"not valid JSON: {error}") from error

    if not isinstance(decoded, dict):
        raise ControlMessageError(
            f"expected a JSON object, got {type(decoded).__name__}"
        )

    message_type = decoded.get("type")
    if not isinstance(message_type, str):
        raise ControlMessageError("message has no string 'type' field")

    if message_type == "hello":
        return _parse_hello(decoded)
    if message_type == "status":
        return _parse_status(decoded)
    if message_type == "gap":
        return _parse_gap(decoded)
    return IgnoredMessage(type=message_type)


def _parse_hello(body: dict[str, Any]) -> Hello:
    protocol_version = _require_int(body, "protocol_version")
    if protocol_version != PROTOCOL_VERSION:
        raise ControlMessageError(
            f"hello declares protocol_version {protocol_version}; "
            f"this endpoint serves version {PROTOCOL_VERSION}"
        )

    oldest_seq_held = _require_int(body, "oldest_seq_held", minimum=0)
    newest_seq_held = _require_int(body, "newest_seq_held", minimum=-1)

    # §5: with an empty queue, `oldest_seq_held` is the next sequence number to
    # be assigned and `newest_seq_held` is that value minus one. So the only
    # relationship that always holds is newest >= oldest - 1.
    if newest_seq_held < oldest_seq_held - 1:
        raise ControlMessageError(
            f"hello is inconsistent: newest_seq_held={newest_seq_held} is "
            f"below oldest_seq_held={oldest_seq_held} - 1"
        )

    return Hello(
        station_id=_require_str(body, "station_id"),
        epoch=_require_epoch(body, "epoch"),
        relay_version=_require_str(body, "relay_version"),
        protocol_version=protocol_version,
        oldest_seq_held=oldest_seq_held,
        newest_seq_held=newest_seq_held,
        monotonic_ns=_require_int(body, "monotonic_ns"),
        utc_ns=_require_int(body, "utc_ns"),
    )


def _parse_status(body: dict[str, Any]) -> Status:
    return Status(
        queue_depth=_require_int(body, "queue_depth", minimum=0),
        queue_bytes=_require_int(body, "queue_bytes", minimum=0),
        dropped_intake_total=_require_int(body, "dropped_intake_total", minimum=0),
        dropped_cap_total=_require_int(body, "dropped_cap_total", minimum=0),
        last_datagram_age_ms=_optional_int(body, "last_datagram_age_ms", minimum=0),
        uptime_s=_require_int(body, "uptime_s", minimum=0),
        monotonic_ns=_require_int(body, "monotonic_ns"),
        utc_ns=_require_int(body, "utc_ns"),
    )


def _parse_gap(body: dict[str, Any]) -> Gap:
    from_seq = _require_int(body, "from_seq", minimum=0)
    to_seq = _require_int(body, "to_seq", minimum=0)
    if to_seq <= from_seq:
        raise ControlMessageError(
            f"gap covers no records: from_seq={from_seq}, to_seq={to_seq}. "
            f"to_seq is exclusive, so it must exceed from_seq"
        )
    return Gap(
        epoch=_require_epoch(body, "epoch"),
        from_seq=from_seq,
        to_seq=to_seq,
        # §11: "an open string so later reasons can be added without a version
        # bump", so this is not validated against a known set.
        reason=_require_str(body, "reason"),
    )


def build_welcome(resume_from_seq: int) -> str:
    """relay-v1 §5. The server is authoritative about what it holds."""
    if resume_from_seq < 0:
        raise ValueError(f"resume_from_seq cannot be negative: {resume_from_seq}")
    return json.dumps(
        {
            "type": "welcome",
            "protocol_version": PROTOCOL_VERSION,
            "resume_from_seq": resume_from_seq,
        }
    )


def build_ack(epoch: str, seq: int) -> str:
    """relay-v1 §7. Cumulative, and only for what is durably stored.

    The epoch is not decoration: it is what lets the relay discard an ack that
    arrives after it has started a new epoch, instead of deleting records the
    ack does not describe.
    """
    if not _EPOCH_PATTERN.match(epoch):
        raise ValueError(f"not a relay-v1 epoch: {epoch!r}")
    if seq < 0:
        raise ValueError(f"cannot acknowledge a negative seq: {seq}")
    return json.dumps({"type": "ack", "epoch": epoch, "seq": seq})


# --- field helpers ----------------------------------------------------------


def _require_str(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str):
        raise ControlMessageError(
            f"field {field!r} must be a string, got {_describe(value)}"
        )
    if not value:
        raise ControlMessageError(f"field {field!r} is empty")
    return value


def _require_epoch(body: dict[str, Any], field: str) -> str:
    value = _require_str(body, field)
    if not _EPOCH_PATTERN.match(value):
        raise ControlMessageError(
            f"field {field!r} must be 32 lowercase hex characters, got {value!r}"
        )
    return value


def _require_int(
    body: dict[str, Any], field: str, *, minimum: int | None = None
) -> int:
    value = body.get(field)
    # bool is a subclass of int, and `True` silently becoming 1 in a sequence
    # number or a drop counter would be a wrong number rather than an error.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ControlMessageError(
            f"field {field!r} must be an integer, got {_describe(value)}"
        )
    if minimum is not None and value < minimum:
        raise ControlMessageError(
            f"field {field!r} must be at least {minimum}, got {value}"
        )
    return value


def _optional_int(
    body: dict[str, Any], field: str, *, minimum: int | None = None
) -> int | None:
    if body.get(field) is None:
        return None
    return _require_int(body, field, minimum=minimum)


def _describe(value: object) -> str:
    return "null" if value is None else f"{type(value).__name__} {value!r}"
