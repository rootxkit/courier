"""The DEM against the flight controller's terrain, from an archive. P5-00."""

from __future__ import annotations

from pathlib import Path

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from common.terrain import Terrain
from common.tests.test_terrain import STEP, install, tile_bytes
from gateway.archive import RawArchive
from gateway.relay_records import Record
from tools.terrain_compare import compare

T0_NS = 1_790_000_000_000_000_000


def terrain_report(
    sysid: int, lat: float, lon: float, height_m: float, loaded: int
) -> bytes:
    link = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=1)
    message = link.terrain_report_encode(
        int(lat * 1e7), int(lon * 1e7), 100, height_m, 0.0, 0, loaded
    )
    return bytes(message.pack(link))


def archive_of(tmp_path: Path, datagrams: list[bytes]) -> Path:
    root = tmp_path / "archive"
    records = [
        Record(seq=i + 1, recv_utc_ns=T0_NS + i * 1_000_000, datagram=d)
        for i, d in enumerate(datagrams)
    ]
    RawArchive(root=root).append("station-1", "0" * 32, records)
    return root


def terrain(tmp_path: Path) -> Terrain:
    return install(
        tmp_path / "t", {"N41E044": "COP-DEM GLO-30"}, {"N41E044": tile_bytes()}
    )


def test_the_difference_is_dem_minus_flight_controller(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    lat, lon = 42.0 - 2 * STEP, 44.0 + 3 * STEP  # DEM: 423 m
    root = archive_of(
        tmp_path,
        [
            terrain_report(201, lat, lon, 420.0, 336),
            terrain_report(201, lat, lon, 426.0, 336),
        ],
    )

    tallies = compare(root, terrain(tmp_path))

    tally = tallies["sysid 201 / N41E044 / COP-DEM GLO-30"]
    assert tally.differences_m == pytest.approx([3.0, -3.0], abs=0.11)
    assert tally.summary()["compared"] == 2


def test_loaded_zero_is_counted_and_never_compared(tmp_path: Path) -> None:
    """The real aircraft said 0.0 m with nothing loaded, for a whole flight."""
    (tmp_path / "t").mkdir()
    root = archive_of(tmp_path, [terrain_report(1, 41.7, 44.8, 0.0, 0)] * 3)

    tallies = compare(root, terrain(tmp_path))

    assert tallies["sysid 1 / -"].not_loaded == 3
    assert tallies["sysid 1 / -"].differences_m == []
    assert tallies["sysid 1 / -"].summary()["mean_dem_minus_fc_m"] is None


def test_a_position_outside_the_dem_is_counted_apart(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    root = archive_of(tmp_path, [terrain_report(202, 10.5, 10.5, 50.0, 10)])

    assert compare(root, terrain(tmp_path))["sysid 202 / -"].outside_dem == 1


def test_one_sysid_in_two_cells_is_two_tallies(tmp_path: Path) -> None:
    """The same SITL SYSID flies Tbilisi one day and the mountains the next."""
    (tmp_path / "t").mkdir()
    here = install(
        tmp_path / "t",
        {"N41E044": "COP-DEM GLO-30", "N42E044": "COP-DEM GLO-30"},
        {"N41E044": tile_bytes(), "N42E044": tile_bytes(lat_first=43.0)},
    )
    root = archive_of(
        tmp_path,
        [
            terrain_report(201, 42.0 - 2 * STEP, 44.0 + 3 * STEP, 420.0, 336),
            terrain_report(201, 43.0 - 2 * STEP, 44.0 + 3 * STEP, 420.0, 336),
        ],
    )

    tallies = compare(root, here)

    assert tallies["sysid 201 / N41E044 / COP-DEM GLO-30"].summary()["compared"] == 1
    assert tallies["sysid 201 / N42E044 / COP-DEM GLO-30"].summary()["compared"] == 1
