#!/usr/bin/env python3
"""Probe a QGroundControl MAVLink forwarding endpoint.

Answers the P1-00 question: what does QGC's forwarding actually give us, and is
the channel usable in both directions?

Listen mode needs nothing but the standard library, so it runs on a bare Windows
Python outside the venv. Round-trip mode needs pymavlink.

    python tools/mavlink_probe.py listen
    python tools/mavlink_probe.py listen --seconds 60 --json report.json
    python tools/mavlink_probe.py roundtrip --param SYSID_THISMAV

Configure QGC first:
    Application Settings -> General -> MAVLink -> Enable MAVLink forwarding
    Host: 127.0.0.1:14445
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 14445

MAVLINK_V1_MAGIC = 0xFE
MAVLINK_V2_MAGIC = 0xFD

# Only the messages this project cares about. Anything else is reported by
# numeric ID, which is enough to notice that it is arriving.
MESSAGE_NAMES = {
    0: "HEARTBEAT",
    1: "SYS_STATUS",
    2: "SYSTEM_TIME",
    22: "PARAM_VALUE",
    24: "GPS_RAW_INT",
    26: "SCALED_IMU",
    27: "RAW_IMU",
    29: "SCALED_PRESSURE",
    30: "ATTITUDE",
    32: "LOCAL_POSITION_NED",
    33: "GLOBAL_POSITION_INT",
    34: "RC_CHANNELS_SCALED",
    35: "RC_CHANNELS_RAW",
    36: "SERVO_OUTPUT_RAW",
    42: "MISSION_CURRENT",
    46: "MISSION_ITEM_REACHED",
    62: "NAV_CONTROLLER_OUTPUT",
    65: "RC_CHANNELS",
    74: "VFR_HUD",
    116: "SCALED_IMU2",
    125: "POWER_STATUS",
    136: "TERRAIN_REPORT",
    141: "ALTITUDE",
    147: "BATTERY_STATUS",
    165: "HIGH_LATENCY",
    193: "EKF_STATUS_REPORT",
    241: "VIBRATION",
    242: "HOME_POSITION",
    253: "STATUSTEXT",
}

# Messages the telemetry pipeline depends on. Their absence is a finding.
REQUIRED = {0, 1, 24, 33, 74, 147, 253}

PARAM_VALUE_MSGID = 22

MAVLINK_V1_HEADER_LEN = 6
MAVLINK_V2_HEADER_LEN = 10

# Roundtrip timing. The injection interval is deliberately longer than the
# correlation window so the channel is quiet between attempts: if every moment
# fell inside some window, correlating a reply with an injection would prove
# nothing at all.
PEER_DISCOVERY_S = 15.0
BASELINE_S = 15.0
INJECT_INTERVAL_S = 5.0
CORRELATION_WINDOW_S = 2.0
REQUIRED_CORRELATIONS = 3

# PARAM_VALUE payload layout.
#
# MAVLink orders fields on the wire by descending type size, not by their order
# in the XML definition, so param_id does not start where the message
# documentation reads as though it should:
#
#     float    param_value   offset  0, 4 bytes
#     uint16   param_count   offset  4, 2 bytes
#     uint16   param_index   offset  6, 2 bytes
#     char     param_id[16]  offset  8, 16 bytes
#     uint8    param_type    offset 24, 1 byte
#
# These offsets are asserted against frames built by pymavlink in
# tools/tests/test_mavlink_probe.py rather than trusted from memory.
_PARAM_ID_START = 8
_PARAM_ID_END = 24


def payload_of(frame: bytes) -> bytes | None:
    """Return a frame's payload, or None if the frame is unusable.

    Slices forward by the declared payload length rather than backwards from
    the end of the frame. A signed MAVLink v2 frame carries a 13-byte signature
    after the checksum, so counting back from the end silently yields the wrong
    bytes for exactly the frames that are hardest to notice going wrong.
    """
    if len(frame) < 2:
        return None

    magic = frame[0]
    if magic == MAVLINK_V2_MAGIC:
        start = MAVLINK_V2_HEADER_LEN
    elif magic == MAVLINK_V1_MAGIC:
        start = MAVLINK_V1_HEADER_LEN
    else:
        return None

    payload_len = frame[1]
    payload = frame[start : start + payload_len]
    if len(payload) < payload_len:
        return None
    return payload


def extract_param_id(frame: bytes) -> str | None:
    """Return the param_id carried by a PARAM_VALUE frame, or None.

    None means "could not read a name", which the caller must treat as a
    failure. It must never be treated as a wildcard match: doing so is how a
    one-way link gets reported as bidirectional.
    """
    payload = payload_of(frame)
    if payload is None:
        return None

    # MAVLink v2 truncates trailing zero bytes from the payload. The bytes it
    # removed were zeros by definition, so restoring them is exact, not a guess.
    padded = payload.ljust(_PARAM_ID_END, b"\x00")
    try:
        name = padded[_PARAM_ID_START:_PARAM_ID_END].split(b"\x00")[0].decode("ascii")
    except (UnicodeDecodeError, IndexError):
        return None

    return name or None


def decode_header(pkt: bytes) -> tuple[int, int, int] | None:
    """Return (sysid, compid, msgid) or None if the frame is unparseable."""
    if len(pkt) < 8:
        return None
    magic = pkt[0]
    if magic == MAVLINK_V2_MAGIC:
        if len(pkt) < 10:
            return None
        sysid, compid = pkt[5], pkt[6]
        msgid = pkt[7] | (pkt[8] << 8) | (pkt[9] << 16)
        return sysid, compid, msgid
    if magic == MAVLINK_V1_MAGIC:
        return pkt[3], pkt[4], pkt[5]
    return None


def split_frames(buf: bytes) -> list[bytes]:
    """A UDP datagram may carry several MAVLink frames back to back."""
    frames: list[bytes] = []
    i = 0
    n = len(buf)
    while i < n:
        magic = buf[i]
        if magic == MAVLINK_V2_MAGIC:
            if i + 10 > n:
                break
            payload_len = buf[i + 1]
            incompat = buf[i + 2]
            total = 12 + payload_len + (13 if incompat & 0x01 else 0)
        elif magic == MAVLINK_V1_MAGIC:
            if i + 6 > n:
                break
            total = 8 + buf[i + 1]
        else:
            i += 1  # resynchronise
            continue
        if i + total > n:
            break
        frames.append(buf[i : i + total])
        i += total
    return frames


def cmd_listen(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port} -- {exc}", file=sys.stderr)
        print("Another listener may already hold the port.", file=sys.stderr)
        return 2
    sock.settimeout(1.0)

    print(f"Listening on {args.host}:{args.port} for {args.seconds}s.")
    print("Enable MAVLink forwarding in QGC if nothing arrives.\n")

    counts: Counter[tuple[int, int]] = Counter()
    first_seen: dict[tuple[int, int], float] = {}
    last_seen: dict[tuple[int, int], float] = {}
    sysids: set[int] = set()
    sources: set[str] = set()
    datagrams = 0
    total_bytes = 0
    unparseable = 0

    start = time.monotonic()
    deadline = start + args.seconds
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            now = time.monotonic()
            datagrams += 1
            total_bytes += len(data)
            sources.add(f"{addr[0]}:{addr[1]}")
            for frame in split_frames(data):
                header = decode_header(frame)
                if header is None:
                    unparseable += 1
                    continue
                sysid, _compid, msgid = header
                sysids.add(sysid)
                key = (sysid, msgid)
                counts[key] += 1
                first_seen.setdefault(key, now)
                last_seen[key] = now
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        sock.close()

    elapsed = time.monotonic() - start
    if not counts:
        print("NOTHING RECEIVED.")
        print("  - Is forwarding enabled in QGC and pointed at this host:port?")
        print("  - Is QGC actually connected to a vehicle?")
        print("  - On Windows, check that the firewall is not blocking loopback.")
        return 1

    print(f"Duration        {elapsed:.1f}s")
    print(f"Datagrams       {datagrams}  ({total_bytes / elapsed / 1024:.1f} KiB/s)")
    print(f"Source(s)       {', '.join(sorted(sources))}")
    print(f"Vehicle SYSIDs  {sorted(sysids)}")
    if unparseable:
        print(f"Unparseable     {unparseable} frames")
    print()

    per_sys: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
    for (sysid, msgid), count in counts.items():
        span = max(last_seen[(sysid, msgid)] - first_seen[(sysid, msgid)], 1e-6)
        rate = (count - 1) / span if count > 1 else 0.0
        per_sys[sysid].append((msgid, count, rate))

    for sysid in sorted(per_sys):
        print(f"SYSID {sysid}")
        print(f"  {'message':<24} {'count':>7} {'Hz':>7}")
        for msgid, count, rate in sorted(per_sys[sysid], key=lambda r: -r[1]):
            name = MESSAGE_NAMES.get(msgid, f"#{msgid}")
            print(f"  {name:<24} {count:>7} {rate:>7.2f}")
        missing = REQUIRED - {m for m, _, _ in per_sys[sysid]}
        if missing:
            names = ", ".join(sorted(MESSAGE_NAMES.get(m, str(m)) for m in missing))
            print(f"  MISSING (required by the pipeline): {names}")
            print("  Raise the relevant SR*_ stream rate parameters.")
        print()

    if args.json:
        report = {
            "host": args.host,
            "port": args.port,
            "duration_s": round(elapsed, 2),
            "datagrams": datagrams,
            "bytes": total_bytes,
            "sources": sorted(sources),
            "sysids": sorted(sysids),
            "unparseable_frames": unparseable,
            "messages": [
                {
                    "sysid": sysid,
                    "msgid": msgid,
                    "name": MESSAGE_NAMES.get(msgid, f"#{msgid}"),
                    "count": count,
                    "rate_hz": round(rate, 3),
                }
                for sysid in sorted(per_sys)
                for msgid, count, rate in sorted(per_sys[sysid])
            ],
        }
        with Path(args.json).open("w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"Report written to {args.json}")

    return 0


def _discover_peer(
    sock: socket.socket, timeout_s: float
) -> tuple[tuple[str, int] | None, int | None]:
    """Wait for any frame, to learn who to answer and which vehicle it is."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except TimeoutError:
            continue
        for frame in split_frames(data):
            header = decode_header(frame)
            if header is not None:
                return addr, header[0]
    return None, None


def _count_unsolicited_param_values(
    sock: socket.socket, seconds: float, wanted: str
) -> tuple[int, int]:
    """Listen without injecting anything. Returns (any PARAM_VALUE, matching)."""
    total = 0
    matching = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
        except TimeoutError:
            continue
        for frame in split_frames(data):
            header = decode_header(frame)
            if header is None or header[2] != PARAM_VALUE_MSGID:
                continue
            total += 1
            name = extract_param_id(frame)
            if name is not None and name.upper() == wanted.upper():
                matching += 1
    return total, matching


def _inject_and_correlate(
    sock: socket.socket,
    peer: tuple[str, int],
    packed: bytes,
    wanted: str,
    timeout_s: float,
) -> tuple[int, int, int]:
    """Inject on a fixed interval, counting only responses that follow one.

    Returns (correlated, uncorrelated, attempts). The injection interval is
    deliberately longer than the correlation window, so the channel is quiet
    between attempts. Without that gap every arrival would fall inside some
    window and the correlation would prove nothing.
    """
    correlated = 0
    uncorrelated = 0
    attempts = 0
    last_injection: float | None = None

    next_send_at = time.monotonic()
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline and correlated < REQUIRED_CORRELATIONS:
        now = time.monotonic()
        if now >= next_send_at:
            sock.sendto(packed, peer)
            attempts += 1
            last_injection = now
            next_send_at = now + INJECT_INTERVAL_S

        try:
            data, _ = sock.recvfrom(4096)
        except TimeoutError:
            continue

        arrived = time.monotonic()
        for frame in split_frames(data):
            header = decode_header(frame)
            if header is None or header[2] != PARAM_VALUE_MSGID:
                continue
            name = extract_param_id(frame)
            # An unreadable name is a failure, never a match. Treating it as one
            # is how a one-way link gets reported as bidirectional.
            if name is None or name.upper() != wanted.upper():
                continue
            if (
                last_injection is not None
                and arrived - last_injection <= CORRELATION_WINDOW_S
            ):
                correlated += 1
            else:
                uncorrelated += 1

    return correlated, uncorrelated, attempts


def cmd_roundtrip(args: argparse.Namespace) -> int:
    """Test whether traffic injected on the forwarding socket reaches the vehicle.

    The naive version of this test — send a PARAM_REQUEST_READ, call any
    PARAM_VALUE a success — cannot distinguish a reply from ordinary traffic.
    QGC requests parameters on its own, so PARAM_VALUE is already flowing
    through the forwarded stream whether or not our injection goes anywhere.

    A false negative here costs nothing: it leaves us on Stage 0, which is the
    plan. A false positive is the expensive one, because it would have us
    believe the server can reach the aircraft when it cannot. So the test
    refuses to guess:

      1. Measure a quiet baseline with no injection at all.
      2. If PARAM_VALUE arrives unsolicited, this method cannot answer the
         question on this setup. Say so and stop.
      3. Otherwise inject on a fixed interval, and count only the requested
         param_id arriving inside the window after an injection, repeatedly.
    """
    try:
        from pymavlink.dialects.v20 import ardupilotmega as mav_dialect
    except ImportError:
        print("roundtrip mode needs pymavlink:", file=sys.stderr)
        print("  .\\.venv\\Scripts\\python -m pip install pymavlink", file=sys.stderr)
        return 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port} -- {exc}", file=sys.stderr)
        return 2
    sock.settimeout(1.0)

    try:
        print(f"Waiting for a frame on {args.host}:{args.port} to learn the peer...")
        peer, target_sys = _discover_peer(sock, PEER_DISCOVERY_S)
        if peer is None or target_sys is None:
            print(
                "No traffic arrived. Run 'listen' first and fix that.", file=sys.stderr
            )
            return 1

        print(f"Peer {peer[0]}:{peer[1]}, vehicle SYSID {target_sys}")

        # Step 1 — baseline. Nothing is injected during this window.
        print(f"\nBaseline: listening {BASELINE_S:.0f}s without injecting anything.")
        unsolicited, unsolicited_match = _count_unsolicited_param_values(
            sock, BASELINE_S, args.param
        )
        print(
            f"  unsolicited PARAM_VALUE: {unsolicited} "
            f"({unsolicited_match} matching {args.param})"
        )

        if unsolicited:
            print()
            print("RESULT: INCONCLUSIVE.")
            print()
            print(
                f"PARAM_VALUE is already arriving without us asking "
                f"({unsolicited} in {BASELINE_S:.0f}s), so a reply to an injected"
            )
            print("request cannot be told apart from traffic that was going to")
            print("arrive anyway. This method cannot answer the question here.")
            print()
            print("Either quiet the ground station first — close other GCS")
            print("instances, let QGC finish its initial parameter download, then")
            print("re-run — or answer the question a different way.")
            print()
            print("Do NOT record this as bidirectional. Record it as untested.")
            return 3

        # Step 2 — inject, and require repeated correlation.
        mav = mav_dialect.MAVLink(None, srcSystem=255, srcComponent=190)
        request = mav_dialect.MAVLink_param_request_read_message(
            target_system=target_sys,
            target_component=1,
            param_id=args.param.encode("ascii"),
            param_index=-1,
        )
        packed = request.pack(mav)

        print(
            f"\nInjecting PARAM_REQUEST_READ for {args.param!r} "
            f"every {INJECT_INTERVAL_S:.0f}s for up to {args.timeout}s."
        )
        print(
            f"Counting a reply only within {CORRELATION_WINDOW_S:.0f}s of an "
            f"injection; {REQUIRED_CORRELATIONS} needed."
        )
        correlated, uncorrelated, attempts = _inject_and_correlate(
            sock, peer, packed, args.param, float(args.timeout)
        )
        print(
            f"  attempts {attempts}, correlated {correlated}, "
            f"uncorrelated {uncorrelated}"
        )
    finally:
        sock.close()

    print()
    if correlated >= REQUIRED_CORRELATIONS:
        print("RESULT: BIDIRECTIONAL on this QGC build.")
        print()
        print(f"{correlated} replies for {args.param} each followed an injection")
        print(f"within {CORRELATION_WINDOW_S:.0f}s, against a silent baseline.")
        print()
        print("Do not rely on it regardless. It is undocumented, it varies by")
        print("QGC build, and it would let the server affect flight through a")
        print("path nobody designed for that. Record the exact QGC version.")
        return 0

    print("RESULT: TELEMETRY-ONLY.")
    print()
    if uncorrelated:
        print(f"{uncorrelated} matching PARAM_VALUE arrived outside any injection")
        print("window, which is not evidence of a reply. Re-run if this is high.")
        print()
    print("This is the expected outcome and the one the plan assumes.")
    print("Missions stay in QGC until mavlink-router replaces forwarding (P3B-01).")
    return 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=DEFAULT_HOST)
    common.add_argument("--port", type=int, default=DEFAULT_PORT)

    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0], parents=[common]
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_listen = sub.add_parser(
        "listen", help="inventory message types and rates", parents=[common]
    )
    p_listen.add_argument("--seconds", type=int, default=30)
    p_listen.add_argument("--json", help="write a JSON report to this path")
    p_listen.set_defaults(func=cmd_listen)

    p_rt = sub.add_parser(
        "roundtrip",
        help="test whether the channel is bidirectional",
        parents=[common],
    )
    p_rt.add_argument("--param", default="SYSID_THISMAV")
    # Budget for the injection phase only; the baseline window runs first, so
    # the whole command takes roughly BASELINE_S longer than this. The default
    # allows well over the REQUIRED_CORRELATIONS injections needed at
    # INJECT_INTERVAL_S apart, so a slow radio link is not mistaken for silence.
    p_rt.add_argument("--timeout", type=int, default=45)
    p_rt.set_defaults(func=cmd_roundtrip)

    args = parser.parse_args()
    # argparse hands back Any; name the contract the subparsers were built to.
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
