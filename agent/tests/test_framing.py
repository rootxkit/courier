"""Record framing round-trips, and refuses to guess at truncation."""

from __future__ import annotations

import pytest

from agent.framing import (
    MAX_DATAGRAM_BYTES,
    RECORD_HEADER_BYTES,
    FramingError,
    Record,
    batch_size_bytes,
    decode_records,
    encode_records,
)


def record(seq: int = 0, payload: bytes = b"\xfd\x09\x00", ts: int = 1) -> Record:
    return Record(seq=seq, recv_utc_ns=ts, datagram=payload)


def test_header_is_eighteen_bytes() -> None:
    """u64 + i64 + u16. Pinned because the Gateway decodes against this."""
    assert RECORD_HEADER_BYTES == 18


def test_single_record_round_trips() -> None:
    original = record(seq=7, payload=b"hello", ts=1758412800123456789)

    assert decode_records(encode_records([original])) == [original]


def test_many_records_round_trip_in_order() -> None:
    originals = [record(seq=n, payload=bytes([n]) * (n + 1)) for n in range(50)]

    assert decode_records(encode_records(originals)) == originals


def test_empty_batch_round_trips() -> None:
    assert encode_records([]) == b""
    assert decode_records(b"") == []


def test_empty_datagram_is_preserved() -> None:
    """A zero-length UDP payload is legal and must survive."""
    original = record(seq=3, payload=b"")

    assert decode_records(encode_records([original])) == [original]


def test_large_sequence_numbers_survive() -> None:
    """seq is u64; the Gateway dedupes on it, so it must not wrap or truncate."""
    original = record(seq=2**64 - 1)

    assert decode_records(encode_records([original]))[0].seq == 2**64 - 1


def test_negative_timestamps_survive() -> None:
    """recv_utc_ns is signed: a laptop clock set before 1970 is not our problem."""
    original = record(ts=-1_000_000_000)

    assert decode_records(encode_records([original]))[0].recv_utc_ns == -1_000_000_000


def test_maximum_datagram_round_trips() -> None:
    original = record(payload=b"\x00" * MAX_DATAGRAM_BYTES)

    assert decode_records(encode_records([original])) == [original]


def test_oversized_datagram_is_refused() -> None:
    with pytest.raises(FramingError, match="exceeds"):
        encode_records([record(payload=b"\x00" * (MAX_DATAGRAM_BYTES + 1))])


def test_truncated_header_raises() -> None:
    """A short read must never be read as 'the rest was empty'."""
    encoded = encode_records([record(seq=1), record(seq=2)])

    with pytest.raises(FramingError, match="truncated record header"):
        decode_records(encoded[:-25])


def test_truncated_payload_raises() -> None:
    encoded = encode_records([record(seq=1, payload=b"abcdefgh")])

    with pytest.raises(FramingError, match="truncated datagram"):
        decode_records(encoded[:-3])


def test_a_lost_final_record_is_an_error_not_silence() -> None:
    """The failure this protects against: telemetry vanishing quietly."""
    encoded = encode_records([record(seq=n) for n in range(5)])

    with pytest.raises(FramingError):
        decode_records(encoded[:-1])


def test_batch_size_matches_the_encoding() -> None:
    records = [record(seq=n, payload=b"x" * n) for n in range(10)]

    assert batch_size_bytes(records) == len(encode_records(records))


def test_datagram_is_carried_verbatim() -> None:
    """Not parsed, not validated, not repaired - including when malformed."""
    garbage = bytes(range(256))
    original = record(payload=garbage)

    assert decode_records(encode_records([original]))[0].datagram == garbage
