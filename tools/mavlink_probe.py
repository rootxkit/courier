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
            except socket.timeout:
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
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"Report written to {args.json}")

    return 0


def cmd_roundtrip(args: argparse.Namespace) -> int:
    """Test whether traffic injected on the forwarding socket reaches the vehicle.

    Requests one parameter and waits for the matching PARAM_VALUE. A response
    proves the whole loop: probe -> QGC -> radio -> vehicle -> back.
    """
    try:
        from pymavlink import mavutil
        from pymavlink.dialects.v20 import ardupilotmega as mav_dialect
    except ImportError:
        print("roundtrip mode needs pymavlink:", file=sys.stderr)
        print("  .\\.venv\\Scripts\\python -m pip install pymavlink", file=sys.stderr)
        return 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.settimeout(1.0)

    print(f"Waiting for a frame on {args.host}:{args.port} to learn the peer...")
    peer = None
    target_sys = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and peer is None:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        for frame in split_frames(data):
            header = decode_header(frame)
            if header:
                target_sys = header[0]
                peer = addr
                break

    if peer is None:
        print("No traffic arrived. Run 'listen' first and fix that.", file=sys.stderr)
        sock.close()
        return 1

    print(f"Peer {peer[0]}:{peer[1]}, vehicle SYSID {target_sys}")
    print(f"Requesting parameter {args.param!r}...\n")

    mav = mav_dialect.MAVLink(None, srcSystem=255, srcComponent=190)
    msg = mav_dialect.MAVLink_param_request_read_message(
        target_system=target_sys,
        target_component=1,
        param_id=args.param.encode("ascii"),
        param_index=-1,
    )
    packed = msg.pack(mav)

    received = False
    deadline = time.monotonic() + args.timeout
    attempts = 0
    while time.monotonic() < deadline and not received:
        if attempts == 0 or time.monotonic() % 2 < 0.05:
            sock.sendto(packed, peer)
            attempts += 1
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        for frame in split_frames(data):
            header = decode_header(frame)
            if header and header[2] == 22:  # PARAM_VALUE
                try:
                    parsed = mavutil.mavlink.MAVLink_message  # noqa: F841
                    body = frame[10:-2] if frame[0] == MAVLINK_V2_MAGIC else frame[6:-2]
                    name = body[4:20].split(b"\x00")[0].decode("ascii", "replace")
                except Exception:
                    name = "<unparsed>"
                if args.param.upper() in name.upper() or name == "<unparsed>":
                    print(f"PARAM_VALUE received for {name!r} after {attempts} attempt(s).")
                    received = True
                    break

    sock.close()

    print()
    if received:
        print("RESULT: the forwarding socket is BIDIRECTIONAL on this QGC build.")
        print("Do not rely on it regardless -- it is undocumented and may change")
        print("between versions. Record the exact QGC version in the decision doc.")
        return 0

    print("RESULT: no response. Treat the channel as TELEMETRY-ONLY.")
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
    p_rt.add_argument("--timeout", type=int, default=20)
    p_rt.set_defaults(func=cmd_roundtrip)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
