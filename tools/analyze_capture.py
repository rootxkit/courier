#!/usr/bin/env python3
"""Offline integrity analysis of a relay-v1 capture.

A passing sink report proves relay -> sink. It says nothing about QGC -> relay,
and the relay's own counters cannot see that link: `dropped_intake_total`
counts datagrams the relay took off the socket and could not hand on, but a
datagram the OS discarded from the UDP receive buffer before `recvfrom` was
never counted anywhere.

MAVLink carries its own per-(sysid, compid) sequence number, one byte, wrapping
at 256. Every stored record still holds the frames exactly as they arrived, so
a hole in that sequence is loss upstream of the relay's queue. Over USB there
is no radio to lose frames, so on a USB capture such a hole is loss between QGC
and the relay.

This is deliberately not part of the sink. The sink verifies the transport; this
verifies the stream that travelled through it. Keeping them apart means a bug in
one cannot excuse the other.

    python tools/analyze_capture.py local/sink-data
    python tools/analyze_capture.py local/sink-data --bucket-seconds 10 \\
        --mark-from 16:16:59 --mark-to 16:19:33
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from tools.mavlink_probe import decode_header, split_frames

# relay-v1 §6 record layout: u64 seq, i64 recv_utc_ns, u16 len, payload.
_RECORD_HEADER = struct.Struct("<QqH")
_RECORD_HEADER_BYTES = _RECORD_HEADER.size

# The MAVLink sequence byte. Its offset differs by version, and getting this
# wrong is silent: v2's offset-2 byte is incompat_flags, which is 0 on every
# unsigned frame, so a wrong offset compares a constant and reports no loss no
# matter what happened.
#
#   v1: magic, len, SEQ, sysid, compid, msgid            -> offset 2
#   v2: magic, len, incompat, compat, SEQ, sysid, compid -> offset 4
#
# Derived by packing two frames that differ only in seq and diffing them;
# pinned in tools/tests/test_analyze_capture.py.
_SEQ_OFFSET_V1 = 2
_SEQ_OFFSET_V2 = 4
_SEQ_MODULO = 256

MAVLINK_V1_MAGIC = 0xFE
MAVLINK_V2_MAGIC = 0xFD

# A gap larger than this is more likely a stream that stopped and restarted
# than a run of lost frames, and reporting it as loss would be misleading.
MAX_PLAUSIBLE_GAP = 200


@dataclass
class SourceStats:
    frames: int = 0
    first_utc_ns: int | None = None
    last_utc_ns: int | None = None
    previous_seq: int | None = None
    lost: int = 0
    gaps: list[tuple[int, int, int]] = field(default_factory=list)
    suspicious_jumps: int = 0
    messages: Counter[int] = field(default_factory=Counter)


def sequence_offset(frame: bytes) -> int | None:
    """Where this frame's sequence byte sits, by MAVLink version."""
    if not frame:
        return None
    if frame[0] == MAVLINK_V2_MAGIC:
        return _SEQ_OFFSET_V2
    if frame[0] == MAVLINK_V1_MAGIC:
        return _SEQ_OFFSET_V1
    return None


def read_records(path: Path) -> list[tuple[int, int, bytes]]:
    """Return (seq, recv_utc_ns, datagram) from a sink records file."""
    data = path.read_bytes()
    out: list[tuple[int, int, bytes]] = []
    offset = 0
    total = len(data)
    while offset + _RECORD_HEADER_BYTES <= total:
        seq, recv_utc_ns, length = _RECORD_HEADER.unpack_from(data, offset)
        offset += _RECORD_HEADER_BYTES
        end = offset + length
        if end > total:
            print(f"  (truncated tail at byte {offset}; ignored)", file=sys.stderr)
            break
        out.append((seq, recv_utc_ns, data[offset:end]))
        offset = end
    return out


def analyse(
    records: list[tuple[int, int, bytes]],
) -> tuple[dict[tuple[int, int], SourceStats], list[tuple[int, int]]]:
    """Walk every frame, tracking the MAVLink sequence per (sysid, compid)."""
    sources: dict[tuple[int, int], SourceStats] = defaultdict(SourceStats)
    datagram_times: list[tuple[int, int]] = []

    for _seq, recv_utc_ns, datagram in records:
        frames = split_frames(datagram)
        datagram_times.append((recv_utc_ns, len(frames)))

        for frame in frames:
            header = decode_header(frame)
            offset = sequence_offset(frame)
            if header is None or offset is None or len(frame) <= offset:
                continue
            sysid, compid, msgid = header
            stats = sources[(sysid, compid)]
            stats.frames += 1
            stats.messages[msgid] += 1
            if stats.first_utc_ns is None:
                stats.first_utc_ns = recv_utc_ns
            stats.last_utc_ns = recv_utc_ns

            mav_seq = frame[offset]
            if stats.previous_seq is not None:
                # Wrap-aware: the byte counts 0..255 and rolls over.
                step = (mav_seq - stats.previous_seq) % _SEQ_MODULO
                if step == 0:
                    # A repeat of the same sequence number. Either a duplicated
                    # datagram or exactly 256 frames lost, which is unlikely.
                    stats.suspicious_jumps += 1
                elif step > 1:
                    missing = step - 1
                    if missing > MAX_PLAUSIBLE_GAP:
                        stats.suspicious_jumps += 1
                    else:
                        stats.lost += missing
                        stats.gaps.append((recv_utc_ns, stats.previous_seq, mav_seq))
            stats.previous_seq = mav_seq

    return sources, datagram_times


def _utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%H:%M:%S")


def _parse_mark(value: str | None) -> int | None:
    """Parse HH:MM:SS as a wall-clock time-of-day, in seconds."""
    if value is None:
        return None
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _seconds_of_day(ns: int) -> int:
    moment = datetime.fromtimestamp(ns / 1e9, tz=UTC)
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def report(
    sources: dict[tuple[int, int], SourceStats],
    datagram_times: list[tuple[int, int]],
    bucket_seconds: int,
    mark_from: int | None,
    mark_to: int | None,
) -> bool:
    """Print the analysis. Returns True if no upstream loss was found."""
    print("=" * 72)
    print("capture integrity analysis")
    print("=" * 72)

    if not datagram_times:
        print("\nNO RECORDS.")
        return False

    clean = True

    print()
    print("Per (sysid, compid), by MAVLink sequence number:")
    print()
    print(f"  {'source':<16} {'frames':>8} {'lost':>7} {'loss %':>8} {'gaps':>6}")
    for (sysid, compid), stats in sorted(sources.items()):
        expected = stats.frames + stats.lost
        loss_pct = (stats.lost / expected * 100) if expected else 0.0
        print(
            f"  {f'{sysid}/{compid}':<16} {stats.frames:>8} {stats.lost:>7} "
            f"{loss_pct:>7.3f}% {len(stats.gaps):>6}"
        )
        if stats.lost:
            clean = False

    for (sysid, compid), stats in sorted(sources.items()):
        if not stats.gaps and not stats.suspicious_jumps:
            continue
        print()
        print(f"  SYSID {sysid} / COMP {compid}")
        for when, before, after in stats.gaps[:20]:
            missing = (after - before) % _SEQ_MODULO - 1
            print(
                f"    {_utc(when)}  seq {before} -> {after}  "
                f"({missing} frame(s) missing)"
            )
        if len(stats.gaps) > 20:
            print(f"    ... and {len(stats.gaps) - 20} more")
        if stats.suspicious_jumps:
            print(
                f"    {stats.suspicious_jumps} jump(s) too large to be loss; "
                f"more likely a stream restart"
            )

    # --- frames per second, bucketed ---------------------------------------
    print()
    print(f"Frames per second, {bucket_seconds}-second buckets:")
    print()

    start_ns = datagram_times[0][0]
    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for recv_utc_ns, frame_count in datagram_times:
        index = int((recv_utc_ns - start_ns) / 1e9 // bucket_seconds)
        buckets[index][0] += 1
        buckets[index][1] += frame_count

    print(
        f"  {'time':<10} {'datagrams/s':>12} {'frames/s':>10} {'frames/datagram':>16}"
    )
    for index in sorted(buckets):
        datagrams, frames = buckets[index]
        when_ns = start_ns + int(index * bucket_seconds * 1e9)
        marker = ""
        if mark_from is not None and mark_to is not None:
            second = _seconds_of_day(when_ns)
            if mark_from <= second < mark_to:
                marker = "   <-- outage"
        per_datagram = frames / datagrams if datagrams else 0.0
        print(
            f"  {_utc(when_ns):<10} {datagrams / bucket_seconds:>12.1f} "
            f"{frames / bucket_seconds:>10.1f} {per_datagram:>16.2f}{marker}"
        )

    print()
    print("=" * 72)
    if clean:
        print("UPSTREAM: CLEAN - no MAVLink sequence gaps. Nothing was lost")
        print("between QGC and the relay during this capture.")
    else:
        total_lost = sum(s.lost for s in sources.values())
        print(f"UPSTREAM: {total_lost} FRAME(S) LOST before the relay's queue.")
        print("On a USB link there is no radio to blame: this is loss between")
        print("QGC and the relay, which no relay counter can see.")
    print("=" * 72)
    return clean


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze_capture.py",
        description="Check a relay-v1 capture for loss upstream of the relay.",
    )
    parser.add_argument(
        "capture", type=Path, help="a sink --out directory, or a .records file"
    )
    parser.add_argument(
        "--bucket-seconds",
        type=int,
        default=10,
        help="width of the frames-per-second buckets (default: 10)",
    )
    parser.add_argument(
        "--mark-from", help="mark buckets from this UTC HH:MM:SS as the outage"
    )
    parser.add_argument("--mark-to", help="end of the marked window, UTC HH:MM:SS")
    args = parser.parse_args(argv)

    if args.capture.is_dir():
        files = sorted(args.capture.rglob("*.records"))
    elif args.capture.is_file():
        files = [args.capture]
    else:
        # A mistyped path is the likeliest way to get here, and a traceback
        # would not say so.
        files = []

    if not files:
        print(
            f"analyze_capture: no .records files under {args.capture}", file=sys.stderr
        )
        return 2

    all_clean = True
    for path in files:
        print(f"\nfile: {path}")
        records = read_records(path)
        print(f"records: {len(records)}")
        sources, datagram_times = analyse(records)
        clean = report(
            sources,
            datagram_times,
            args.bucket_seconds,
            _parse_mark(args.mark_from),
            _parse_mark(args.mark_to),
        )
        all_clean = all_clean and clean

    return 0 if all_clean else 1


if __name__ == "__main__":
    sys.exit(main())
