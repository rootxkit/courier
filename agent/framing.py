"""Binary record framing for relay-v1 §6.

    u64  seq
    i64  recv_utc_ns
    u16  len
    u8   datagram[len]

repeated, little-endian, no padding between records.

The datagram is carried verbatim. Nothing here parses MAVLink, validates a
checksum, or rejects a malformed payload: a relay that discarded traffic it
judged invalid would hide exactly the corruption worth investigating.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "MAX_DATAGRAM_BYTES",
    "RECORD_HEADER_BYTES",
    "UDP_RECEIVE_BUFFER_BYTES",
    "FramingError",
    "Record",
    "decode_records",
    "encode_records",
]

_HEADER = struct.Struct("<QqH")
RECORD_HEADER_BYTES = _HEADER.size

# A UDP payload cannot exceed 65507 bytes, so the u16 length field cannot
# overflow. The limit enforced here is the field's, not the protocol's.
MAX_DATAGRAM_BYTES = 0xFFFF

# Sized to the largest datagram the field can describe rather than to what QGC
# happens to send today. A buffer sized to observed traffic silently truncates
# anything larger, and a truncated datagram is indistinguishable from a corrupt
# one by the time it reaches the Gateway.
UDP_RECEIVE_BUFFER_BYTES = 65535


class FramingError(ValueError):
    """A byte sequence is not a valid relay-v1 batch."""


@dataclass(frozen=True, slots=True)
class Record:
    """One forwarded datagram, with the identity the Gateway dedupes on."""

    seq: int
    recv_utc_ns: int
    datagram: bytes

    @property
    def encoded_size(self) -> int:
        return RECORD_HEADER_BYTES + len(self.datagram)


def encode_records(records: Iterable[Record]) -> bytes:
    """Pack records into a single binary batch."""
    chunks: list[bytes] = []
    for record in records:
        if len(record.datagram) > MAX_DATAGRAM_BYTES:
            raise FramingError(
                f"datagram of {len(record.datagram)} bytes exceeds the "
                f"{MAX_DATAGRAM_BYTES}-byte length field"
            )
        if record.seq < 0:  # pragma: no cover - seq comes from a u64 counter we own
            raise FramingError(f"seq must be unsigned, got {record.seq}")
        chunks.append(
            _HEADER.pack(record.seq, record.recv_utc_ns, len(record.datagram))
        )
        chunks.append(record.datagram)
    return b"".join(chunks)


def decode_records(data: bytes) -> list[Record]:
    """Unpack a binary batch.

    Raises FramingError on a truncated or malformed batch. A short read is
    never interpreted as "the rest was empty": that would turn a transport bug
    into silently missing telemetry, which is the failure mode this whole
    protocol exists to avoid.
    """
    records: list[Record] = []
    offset = 0
    total = len(data)

    while offset < total:
        if offset + RECORD_HEADER_BYTES > total:
            raise FramingError(
                f"truncated record header at offset {offset}: "
                f"{total - offset} bytes available, {RECORD_HEADER_BYTES} needed"
            )
        seq, recv_utc_ns, length = _HEADER.unpack_from(data, offset)
        offset += RECORD_HEADER_BYTES

        end = offset + length
        if end > total:
            raise FramingError(
                f"truncated datagram for seq {seq}: {total - offset} bytes "
                f"available, {length} declared"
            )
        records.append(
            Record(seq=seq, recv_utc_ns=recv_utc_ns, datagram=data[offset:end])
        )
        offset = end

    return records


def batch_size_bytes(records: Sequence[Record]) -> int:
    """Encoded size of a batch, without encoding it."""
    return sum(record.encoded_size for record in records)
