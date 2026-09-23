"""The parser boundary: opaque datagram in, MAVLink messages out.

Everything upstream of here moves bytes. This is the first place that decides
what they mean, and `docs/specs/p1-02-gateway-ingest.md` §4 obligation 9 is
that it happens *after* the transport has stored and acknowledged: a parsing
bug must not become a transport failure.

**pymavlink does the decoding.** CLAUDE.md is explicit that a wire-format
offset must never be written from memory, and §6.2 says the Gateway should
prefer pymavlink's own decoder where it can. It can, here, completely - so
this module contains no offsets at all. The only numbers it names are message
ids and enum values, and each is read from pymavlink by name rather than
written as a literal.

**A fresh parser per datagram.** `MAVLink.parse_buffer` keeps a buffer of
unconsumed bytes, so a shared parser would prepend one station's trailing bytes
to the next station's datagram and decode a frame that never existed. Measured
cost of a new parser each time: 166,000 datagrams/s against the ~840/s a
ten-vehicle fleet produces, so the isolation is free. Reusing one and clearing
its buffer is 1.3x faster and not worth the coupling.

**Corrupt frames are counted, not hidden.** With `robust_parsing`, pymavlink
returns a `BAD_DATA` pseudo-message and carries on decoding the rest of the
datagram - verified, not assumed. Those are reported rather than silently
dropped: relay-v1 §6 forwards malformed datagrams unchanged precisely so the
corruption worth investigating reaches somewhere it can be seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from pymavlink.dialects.v20 import ardupilotmega as mavlink

# pymavlink's name for the pseudo-message it emits for undecodable bytes.
BAD_DATA: Final = "BAD_DATA"


@dataclass(frozen=True, slots=True)
class SourceId:
    """Who sent a frame: the MAVLink address, not an identity.

    `docs/specs/p1-02-gateway-ingest.md` §7 is emphatic that a SYSID is a
    flight-time address set by a parameter and reused across airframes. This is
    the key classification and binding are done on; it is never a `drone_id`.

    The component id is part of it because a gimbal or companion computer
    heartbeats under the vehicle's own SYSID with a different component id, and
    ADR-001 observed exactly that shape: the autopilot at 1/1, QGC at 255/190.
    """

    sysid: int
    compid: int

    def __str__(self) -> str:
        return f"{self.sysid}/{self.compid}"


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """One decoded MAVLink message with the address it came from."""

    source: SourceId
    message_id: int
    name: str
    # pymavlink's decoded message. Kept as-is: unit conversion is P1-03's job
    # and doing it here would spread the parser boundary across two tasks.
    payload: Any

    def field(self, name: str) -> Any:
        return getattr(self.payload, name)


@dataclass(frozen=True, slots=True)
class ParsedDatagram:
    """What one UDP datagram contained."""

    messages: list[ParsedMessage] = field(default_factory=list)
    # Frames pymavlink could not decode. Evidence of a damaged link, or of a
    # sender this dialect does not know; either way a rising count is a finding
    # and a silent zero is not the same as never looking.
    bad_frame_count: int = 0

    def __len__(self) -> int:
        return len(self.messages)


def parse_datagram(datagram: bytes) -> ParsedDatagram:
    """Decode every MAVLink frame in one datagram.

    A datagram normally carries exactly one frame - ADR-001 measured 1.00
    frames per datagram on a real QGC link - but nothing in the protocol
    promises that, so all of them are decoded.
    """
    parser = mavlink.MAVLink(None)
    parser.robust_parsing = True

    decoded = parser.parse_buffer(datagram)
    if decoded is None:
        # Not an error: a datagram that holds no complete frame decodes to
        # nothing. Whether that is a truncation or simply not MAVLink is not
        # this function's call to make.
        return ParsedDatagram()

    messages: list[ParsedMessage] = []
    bad = 0
    for message in decoded:
        name = message.get_type()
        if name == BAD_DATA:
            bad += 1
            continue
        messages.append(
            ParsedMessage(
                source=SourceId(
                    sysid=message.get_srcSystem(), compid=message.get_srcComponent()
                ),
                message_id=message.get_msgId(),
                name=name,
                payload=message,
            )
        )
    return ParsedDatagram(messages=messages, bad_frame_count=bad)
