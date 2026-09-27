"""The parser boundary.

No offsets are asserted here because the module contains none: pymavlink does
the decoding. What is asserted is the behaviour the Gateway relies on, and
every one of these was measured against pymavlink before being written down.
"""

from __future__ import annotations

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.parsing import ParsedDatagram, SourceId, parse_datagram


def link(sysid: int = 1, compid: int = 1) -> mavlink.MAVLink:
    built = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    built.signing.sign_outgoing = False
    return built


def heartbeat_bytes(sysid: int = 1, compid: int = 1) -> bytes:
    sender = link(sysid, compid)
    return bytes(
        sender.heartbeat_encode(
            mavlink.MAV_TYPE_QUADROTOR,
            mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            0,
            0,
            mavlink.MAV_STATE_ACTIVE,
        ).pack(sender)
    )


def attitude_bytes(sysid: int = 1, compid: int = 1) -> bytes:
    sender = link(sysid, compid)
    return bytes(sender.attitude_encode(0, 0.1, 0.2, 1.5, 0.0, 0.0, 0.0).pack(sender))


def test_one_frame_decodes_with_its_source_address() -> None:
    parsed = parse_datagram(heartbeat_bytes(sysid=7, compid=190))

    assert len(parsed) == 1
    message = parsed.messages[0]
    assert message.name == "HEARTBEAT"
    assert message.source == SourceId(sysid=7, compid=190)
    assert message.field("type") == mavlink.MAV_TYPE_QUADROTOR


def test_several_frames_in_one_datagram_all_decode() -> None:
    """ADR-001 measured 1.00 frames per datagram, but nothing promises it."""
    parsed = parse_datagram(heartbeat_bytes() + attitude_bytes() + heartbeat_bytes())

    assert [message.name for message in parsed.messages] == [
        "HEARTBEAT",
        "ATTITUDE",
        "HEARTBEAT",
    ]
    assert parsed.bad_frame_count == 0


def test_an_empty_datagram_decodes_to_nothing() -> None:
    assert parse_datagram(b"") == ParsedDatagram()


def test_a_datagram_that_is_not_mavlink_yields_no_messages() -> None:
    """Not an error. Deciding what non-MAVLink traffic means is not this job."""
    parsed = parse_datagram(b"GET / HTTP/1.1\r\nHost: example\r\n\r\n")
    assert parsed.messages == []


def test_a_corrupt_frame_is_counted_and_the_rest_still_decodes() -> None:
    """Measured behaviour, not assumed.

    pymavlink with robust_parsing emits a BAD_DATA pseudo-message for bytes it
    cannot decode and carries on. Counting those is the point: relay-v1 §6
    forwards malformed datagrams unchanged so the corruption worth
    investigating reaches somewhere it can be seen, and a parser that silently
    swallowed it would undo that.
    """
    good = heartbeat_bytes()
    damaged = bytearray(good)
    damaged[6] ^= 0xFF  # a payload byte, so the CRC fails

    parsed = parse_datagram(bytes(damaged) + attitude_bytes())

    assert parsed.bad_frame_count >= 1
    assert [message.name for message in parsed.messages] == ["ATTITUDE"]


def test_a_clean_datagram_counts_no_bad_frames() -> None:
    """The paired test: a counter that always reported damage is no counter."""
    assert parse_datagram(heartbeat_bytes()).bad_frame_count == 0


def test_a_truncated_frame_decodes_to_nothing_rather_than_guessing() -> None:
    parsed = parse_datagram(heartbeat_bytes()[:-4])
    assert parsed.messages == []


def test_parsers_do_not_share_state_between_datagrams() -> None:
    """The reason for a fresh parser each time.

    A shared parser keeps unconsumed bytes, so a trailing fragment from one
    station would be prepended to the next station's datagram and could decode
    as a frame that was never sent. Here the first datagram ends mid-frame and
    the second is whole: the second must decode on its own merits.
    """
    fragment = heartbeat_bytes()[:-4]
    assert parse_datagram(fragment).messages == []

    second = parse_datagram(attitude_bytes(sysid=9))
    assert [message.name for message in second.messages] == ["ATTITUDE"]
    assert second.messages[0].source == SourceId(sysid=9, compid=1)


def test_the_payload_is_left_in_raw_mavlink_units() -> None:
    """Unit conversion is P1-03, at this boundary but not in this task.

    GLOBAL_POSITION_INT carries lat/lon as int32 at 1e7 scale. It is stored
    unconverted here so the conversion has exactly one home.
    """
    sender = link()
    raw = sender.global_position_int_encode(
        0, 417151000, 448271000, 450000, 60000, 10, 20, 0, 9000
    ).pack(sender)

    message = parse_datagram(bytes(raw)).messages[0]

    assert message.field("lat") == 417151000
    assert message.field("relative_alt") == 60000
