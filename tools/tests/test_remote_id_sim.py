"""The simulator sends what the ingest decodes, where it says it is. P1-15."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from gateway import odid
from tools.remote_id_sim import broadcast, position_after


def test_position_after_moves_the_distance_along_the_track() -> None:
    lat, lon = position_after(41.7, 44.8, 90.0, 1000.0)
    north_lat, north_lon = position_after(41.7, 44.8, 0.0, 1000.0)

    assert lat == pytest.approx(41.7)
    east_m = math.radians(lon - 44.8) * 6_371_000 * math.cos(math.radians(41.7))
    assert east_m == pytest.approx(1000.0, rel=1e-6)
    assert north_lon == pytest.approx(44.8)
    assert math.radians(north_lat - 41.7) * 6_371_000 == pytest.approx(1000.0, rel=1e-9)


def test_a_broadcast_decodes_to_what_was_flown() -> None:
    messages = odid.decode(
        broadcast(
            ua_id="SIM-1",
            operator_id="OP-1",
            lat_deg=41.7151,
            lon_deg=44.8271,
            alt_hae_m=505.0,
            track_deg=270.0,
            speed_ms=8.0,
            seconds_after_hour=61.2,
        )
    )

    basic = next(m for m in messages if isinstance(m, odid.BasicId))
    location = next(m for m in messages if isinstance(m, odid.Location))
    operator = next(m for m in messages if isinstance(m, odid.OperatorId))
    assert basic.ua_id == "SIM-1"
    assert basic.id_type == odid.IdType.SERIAL_NUMBER
    assert location.lat_deg == pytest.approx(41.7151)
    assert location.alt_hae_m == 505.0
    assert location.direction_deg == 270.0
    assert location.speed_horizontal_ms == 8.0
    assert location.status == odid.Status.AIRBORNE
    assert operator.operator_id == "OP-1"


def test_with_a_key_file_every_datagram_is_signed_as_the_receiver(
    tmp_path: Path,
) -> None:
    """The simulator's datagrams pass the ingest's own check."""
    import base64
    import socket
    import time

    from gateway.remote_id_auth import ReceiverAuthenticator, load_keys
    from tools.remote_id_sim import main

    keys = tmp_path / "keys"
    keys.write_text(f"sim-receiver: {base64.b64encode(bytes(32)).decode()}\n")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(2.0)
        port = sink.getsockname()[1]
        assert (
            main(
                [
                    "--port",
                    str(port),
                    "--start-lat",
                    "41.7",
                    "--start-lon",
                    "44.8",
                    "--alt-hae-m",
                    "500",
                    "--track-deg",
                    "90",
                    "--speed-ms",
                    "5",
                    "--duration-s",
                    "0.25",
                    "--rate-hz",
                    "10",
                    "--key-file",
                    str(keys),
                ]
            )
            == 0
        )
        received = [sink.recv(4096) for _ in range(2)]

    auth = ReceiverAuthenticator(keys=load_keys(keys))
    for datagram in received:
        report = json.loads(auth.check(datagram, now_s=time.time()))
        assert report["receiver_id"] == "sim-receiver"
