"""A simulated Remote ID aircraft, sent to the ingest as a receiver would. P1-15.

    python tools/remote_id_sim.py --start-lat 41.7151 --start-lon 44.8221 \\
        --alt-amsl-m 480 --track-deg 90 --speed-ms 8 --duration-s 90 \\
        --geoid local/geoid/egm2008-2_5.pgm

Flies a straight line from the start point at a constant AMSL altitude and
sends, once a second, what a Bluetooth 5 or Wi-Fi Remote ID module would
broadcast: a message pack of Basic ID, Location and Operator ID. The bytes
come from `gateway.odid`'s encoder, which is pinned to the reference
library, so the ingest decodes exactly what a real module would send.

The broadcast altitude is height above the ellipsoid, as the standard
requires, so the simulator needs the same geoid as the ingest to turn the
AMSL altitude it is asked to fly into what it broadcasts. Without
`--geoid`, give `--alt-hae-m` directly.

Nothing here is a flight instruction: it is test traffic for the monitor.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from common.geoid import GeoidGrid
from gateway import odid

EARTH_RADIUS_M = 6_371_000.0


def position_after(
    lat_deg: float, lon_deg: float, track_deg: float, distance_m: float
) -> tuple[float, float]:
    """Flat-earth step: accurate to centimetres over the few km a test flies."""
    track = math.radians(track_deg)
    north_m = distance_m * math.cos(track)
    east_m = distance_m * math.sin(track)
    lat = lat_deg + math.degrees(north_m / EARTH_RADIUS_M)
    lon = lon_deg + math.degrees(
        east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat_deg)))
    )
    return lat, lon


def broadcast(
    *,
    ua_id: str,
    operator_id: str,
    lat_deg: float,
    lon_deg: float,
    alt_hae_m: float,
    track_deg: float,
    speed_ms: float,
    seconds_after_hour: float,
) -> bytes:
    return odid.encode_pack(
        [
            odid.encode_basic_id(
                odid.BasicId(
                    id_type=odid.IdType.SERIAL_NUMBER,
                    ua_type=2,  # helicopter or multirotor
                    ua_id=ua_id,
                )
            ),
            odid.encode_location(
                odid.Location(
                    status=odid.Status.AIRBORNE,
                    direction_deg=track_deg % 360.0,
                    speed_horizontal_ms=speed_ms,
                    speed_vertical_ms=0.0,
                    lat_deg=lat_deg,
                    lon_deg=lon_deg,
                    alt_baro_m=None,
                    alt_hae_m=alt_hae_m,
                    height_reference=odid.HeightReference.OVER_TAKEOFF,
                    height_m=None,
                    horiz_accuracy=10,  # < 10 m
                    vert_accuracy=4,  # < 10 m
                    baro_accuracy=0,
                    speed_accuracy=3,  # < 1 m/s
                    ts_accuracy=0,
                    seconds_after_hour=seconds_after_hour,
                )
            ),
            odid.encode_operator_id(
                odid.OperatorId(operator_id_type=0, operator_id=operator_id)
            ),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/remote_id_sim.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14600)
    parser.add_argument("--receiver-id", default="sim-receiver")
    parser.add_argument("--transmitter", default="02:00:00:00:00:01")
    parser.add_argument("--ua-id", default="SIM-RID-0001")
    parser.add_argument("--operator-id", default="GEO-SIM-OP-1")
    parser.add_argument("--start-lat", type=float, required=True)
    parser.add_argument("--start-lon", type=float, required=True)
    altitude = parser.add_mutually_exclusive_group(required=True)
    altitude.add_argument("--alt-amsl-m", type=float, help="needs --geoid")
    altitude.add_argument("--alt-hae-m", type=float)
    parser.add_argument("--geoid", type=Path)
    parser.add_argument("--track-deg", type=float, required=True)
    parser.add_argument("--speed-ms", type=float, required=True)
    parser.add_argument("--duration-s", type=float, required=True)
    parser.add_argument("--rate-hz", type=float, default=1.0)
    args = parser.parse_args(argv)

    if args.alt_amsl_m is not None:
        if args.geoid is None:
            parser.error("--alt-amsl-m needs --geoid: the broadcast is HAE")
        undulation_m = GeoidGrid.load(args.geoid).undulation_m(
            args.start_lat, args.start_lon
        )
        alt_hae_m = args.alt_amsl_m + undulation_m
        print(
            f"geoid undulation at start {undulation_m:.2f} m; broadcasting HAE {alt_hae_m:.1f} m"
        )
    else:
        alt_hae_m = args.alt_hae_m

    period_s = 1.0 / args.rate_hz
    started = time.monotonic()
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        while (elapsed := time.monotonic() - started) < args.duration_s:
            lat, lon = position_after(
                args.start_lat, args.start_lon, args.track_deg, args.speed_ms * elapsed
            )
            now = datetime.now(tz=UTC)
            payload = broadcast(
                ua_id=args.ua_id,
                operator_id=args.operator_id,
                lat_deg=lat,
                lon_deg=lon,
                alt_hae_m=alt_hae_m,
                track_deg=args.track_deg,
                speed_ms=args.speed_ms,
                seconds_after_hour=now.minute * 60 + now.second + now.microsecond / 1e6,
            )
            report = {
                "receiver_id": args.receiver_id,
                "transmitter": args.transmitter,
                "payload_hex": payload.hex(),
                "rssi_dbm": -60,
            }
            sock.sendto(json.dumps(report).encode(), (args.host, args.port))
            sent += 1
            time.sleep(max(0.0, started + sent * period_s - time.monotonic()))
    print(f"sent {sent} broadcasts as {args.ua_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
