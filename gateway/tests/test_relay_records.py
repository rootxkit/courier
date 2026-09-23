"""Server-side decoding of relay-v1 binary batches.

The first test is the one with teeth: the Gateway's decoder is fed bytes
produced by the *relay's* encoder. The two were written independently from
`relay-v1.md` §6, which is the point - if either misread the document, this is
where the disagreement shows up rather than in a flight.
"""

from __future__ import annotations

import struct

import pytest

from agent.framing import Record as RelayRecord
from agent.framing import encode_records
from gateway.relay_records import (
    MAX_DATAGRAM_BYTES,
    RECORD_HEADER_BYTES,
    Record,
    RecordFramingError,
    decode_records,
)


def test_the_gateway_decodes_what_the_relay_encodes() -> None:
    """The interop test. Two implementations, one document.

    Not a round trip through one codec: `encode_records` is the relay's, in
    `agent/framing.py`, and `decode_records` is the Gateway's. A shared helper
    would make this test pass no matter what the document says.
    """
    sent = [
        RelayRecord(seq=0, recv_utc_ns=1_758_412_800_123_456_789, datagram=b"\xfd\x09"),
        RelayRecord(seq=1, recv_utc_ns=1_758_412_800_223_456_789, datagram=b""),
        RelayRecord(seq=2, recv_utc_ns=-1, datagram=bytes(range(256))),
    ]

    received = decode_records(encode_records(sent))

    assert received == [
        Record(seq=r.seq, recv_utc_ns=r.recv_utc_ns, datagram=r.datagram) for r in sent
    ]


def test_recv_utc_ns_survives_a_negative_value() -> None:
    """`recv_utc_ns` is i64, not u64.

    A ground station whose clock is set before the epoch produces a negative
    value. Unpacking it as unsigned would turn it into something around 1.8e19
    and the record would sort to the end of the flight rather than the start -
    a plausible number, silently wrong, which is the shape of every wire-format
    bug this project has had.
    """
    frame = encode_records(
        [RelayRecord(seq=7, recv_utc_ns=-1_000_000_000, datagram=b"x")]
    )
    assert decode_records(frame)[0].recv_utc_ns == -1_000_000_000


def test_the_header_is_18_bytes_in_the_documented_order() -> None:
    """Pins u64 + i64 + u16 little-endian with no padding.

    Derived from the struct, not asserted as a number remembered from the
    document, and cross-checked by unpacking a frame the relay built.
    """
    assert RECORD_HEADER_BYTES == 8 + 8 + 2

    frame = encode_records(
        [RelayRecord(seq=0x0102030405060708, recv_utc_ns=0x11, datagram=b"ab")]
    )
    seq, recv_utc_ns, length = struct.unpack("<QqH", frame[:RECORD_HEADER_BYTES])
    assert (seq, recv_utc_ns, length) == (0x0102030405060708, 0x11, 2)
    assert frame[RECORD_HEADER_BYTES:] == b"ab"


def test_an_empty_frame_decodes_to_nothing() -> None:
    assert decode_records(b"") == []


def test_a_truncated_header_is_rejected() -> None:
    frame = encode_records([RelayRecord(seq=0, recv_utc_ns=0, datagram=b"abc")])
    with pytest.raises(RecordFramingError, match="truncated record header"):
        decode_records(frame[: RECORD_HEADER_BYTES - 1])


def test_a_truncated_datagram_is_rejected() -> None:
    frame = encode_records([RelayRecord(seq=0, recv_utc_ns=0, datagram=b"abcdef")])
    with pytest.raises(RecordFramingError, match="truncated datagram"):
        decode_records(frame[:-2])


def test_a_length_larger_than_a_datagram_is_rejected() -> None:
    """A `len` no UDP payload could have means the frame is not what it says."""
    frame = struct.pack("<QqH", 0, 0, MAX_DATAGRAM_BYTES + 1)
    with pytest.raises(RecordFramingError, match="cannot exceed"):
        decode_records(frame)


def test_records_out_of_order_within_a_batch_are_rejected() -> None:
    """§6 requires ascending order with no gaps inside one batch."""
    frame = encode_records(
        [
            RelayRecord(seq=5, recv_utc_ns=0, datagram=b"a"),
            RelayRecord(seq=4, recv_utc_ns=0, datagram=b"b"),
        ]
    )
    with pytest.raises(RecordFramingError, match="ascend with no gaps"):
        decode_records(frame)


def test_a_hole_within_a_batch_is_rejected() -> None:
    frame = encode_records(
        [
            RelayRecord(seq=5, recv_utc_ns=0, datagram=b"a"),
            RelayRecord(seq=7, recv_utc_ns=0, datagram=b"b"),
        ]
    )
    with pytest.raises(RecordFramingError, match="ascend with no gaps"):
        decode_records(frame)


def test_a_datagram_that_is_not_mavlink_is_carried_unchanged() -> None:
    """§6: the payload is opaque.

    The decoder must not judge the contents. A relay that discarded "invalid"
    traffic would hide exactly the corruption worth investigating, and so would
    a Gateway that refused to decode it.
    """
    junk = b"\x00\xff\xfeGET / HTTP/1.1\r\n\r\n"
    frame = encode_records([RelayRecord(seq=0, recv_utc_ns=0, datagram=junk)])
    assert decode_records(frame)[0].datagram == junk
