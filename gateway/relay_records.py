"""Decoding of relay-v1 binary record batches, server side.

Written from `docs/protocols/relay-v1.md` §6, deliberately not shared with
`agent/framing.py`. The two are independent implementations of one document,
which is the only way a misreading of it shows up as a disagreement rather than
as two components agreeing on the same mistake. `docs/specs/p1-02-gateway-ingest.md`
§0 and §3 say to borrow the reading of the protocol, never the implementation.

The wire layout, quoted from §6:

    repeated:
      u64  seq              record sequence number within the epoch
      i64  recv_utc_ns      wall-clock time the datagram was received
      u16  len              length of the datagram in bytes
      u8   datagram[len]    the UDP payload, exactly as received

All integers are little-endian. There is no batch header and no padding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

# `<` is little-endian AND standard sizes with no alignment padding. Native
# order ('=' or no prefix) would silently insert padding on some platforms and
# produce a header of the wrong length.
_HEADER: Final = struct.Struct("<QqH")

RECORD_HEADER_BYTES: Final = _HEADER.size

# A UDP payload cannot exceed 65507 bytes (65535 less the 8-byte UDP header and
# the 20-byte IPv4 header), so a longer `len` cannot have come from a datagram.
# relay-v1 §6 relies on this to justify `len` being u16.
MAX_DATAGRAM_BYTES: Final = 65507


class RecordFramingError(ValueError):
    """A binary frame does not conform to relay-v1 §6.

    Raised only for what the document makes impossible, never for a datagram's
    *contents*: §6 says the payload is opaque and that a malformed or
    non-MAVLink datagram is forwarded unchanged, so judging it here would
    discard exactly the corruption worth investigating.
    """


@dataclass(frozen=True, slots=True)
class Record:
    """One relayed datagram with the identity the relay gave it."""

    seq: int
    recv_utc_ns: int
    datagram: bytes


def decode_records(frame: bytes) -> list[Record]:
    """Decode one binary batch.

    Raises `RecordFramingError` if the frame is truncated, if a declared length
    exceeds what a datagram can be, or if the sequence numbers are not strictly
    ascending — §6 requires ascending order with no gaps *within* a batch.
    """
    records: list[Record] = []
    offset = 0
    total = len(frame)

    while offset < total:
        if total - offset < RECORD_HEADER_BYTES:
            raise RecordFramingError(
                f"truncated record header at offset {offset}: "
                f"{total - offset} bytes left, {RECORD_HEADER_BYTES} needed"
            )

        seq, recv_utc_ns, datagram_len = _HEADER.unpack_from(frame, offset)
        offset += RECORD_HEADER_BYTES

        if datagram_len > MAX_DATAGRAM_BYTES:
            raise RecordFramingError(
                f"record seq={seq} declares {datagram_len} bytes; "
                f"a UDP payload cannot exceed {MAX_DATAGRAM_BYTES}"
            )
        if total - offset < datagram_len:
            raise RecordFramingError(
                f"truncated datagram for seq={seq}: {total - offset} bytes "
                f"left, {datagram_len} declared"
            )

        records.append(
            Record(
                seq=seq,
                recv_utc_ns=recv_utc_ns,
                datagram=frame[offset : offset + datagram_len],
            )
        )
        offset += datagram_len

    _check_ascending(records)
    return records


def _check_ascending(records: list[Record]) -> None:
    """Enforce §6's "ascending `seq` order with no gaps" within one batch.

    Checked because the Gateway's dedupe and watermark arithmetic both assume
    it. A batch that arrived out of order would still be stored correctly, but
    it would mean the relay is not doing what the document says, and that is
    worth failing loudly over rather than absorbing.
    """
    for previous, current in pairwise(records):
        if current.seq != previous.seq + 1:
            raise RecordFramingError(
                f"records within a batch must ascend with no gaps: "
                f"seq={previous.seq} is followed by seq={current.seq}"
            )
