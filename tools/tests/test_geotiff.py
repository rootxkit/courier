"""The GeoTIFF reader agrees with GDAL on every value. P5-00.

Each fixture in data/geotiff/ was written by GDAL
(`infra/terrain/make_geotiff_fixtures.py`), in the layout Copernicus DEM
tiles use and in variations the reader must also handle, and the expected
values and georeferencing beside it are GDAL's reading of the same file.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from tools import geotiff

DATA = Path(__file__).parent / "data" / "geotiff"
CASES = sorted(p.stem for p in DATA.glob("*.tif"))


def load(name: str) -> tuple[bytes, dict[str, Any]]:
    return (DATA / f"{name}.tif").read_bytes(), json.loads(
        (DATA / f"{name}.json").read_text(encoding="utf-8")
    )


def test_the_fixtures_cover_the_copernicus_profile_and_its_variations() -> None:
    assert CASES == [
        "strips_deflate_pred3_nodata",
        "strips_uncompressed",
        "tiled_deflate_pred3_area",
        "tiled_deflate_pred3_point",
    ]


@pytest.mark.parametrize("name", CASES)
def test_every_value_matches_gdal(name: str) -> None:
    data, expected = load(name)
    raster = geotiff.read(data)

    assert (raster.width, raster.height) == (expected["width"], expected["height"])
    assert list(raster.values) == expected["values"]


@pytest.mark.parametrize("name", CASES)
def test_the_first_sample_is_where_gdal_puts_it(name: str) -> None:
    data, expected = load(name)
    raster = geotiff.read(data)

    for field in ("lat_first_deg", "lon_first_deg", "lat_step_deg", "lon_step_deg"):
        assert getattr(raster, field) == pytest.approx(expected[field], abs=1e-12), (
            field
        )
    assert raster.nodata == expected["nodata"]


def test_a_point_raster_is_placed_like_an_area_raster() -> None:
    """GDAL wrote the same grid both ways: the sample centres must agree."""
    area = geotiff.read(load("tiled_deflate_pred3_area")[0])
    point = geotiff.read(load("tiled_deflate_pred3_point")[0])

    assert point.lat_first_deg == pytest.approx(area.lat_first_deg, abs=1e-12)
    assert point.lon_first_deg == pytest.approx(area.lon_first_deg, abs=1e-12)


def test_a_file_that_is_not_a_tiff_is_refused() -> None:
    with pytest.raises(geotiff.GeoTiffError, match="not a TIFF"):
        geotiff.read(b"GIF89a" + bytes(20))


def test_an_unsupported_compression_is_refused_by_name() -> None:
    data = bytearray(load("strips_uncompressed")[0])
    # Rewrite the Compression entry's value in place: find tag 259 in the
    # first directory, as the reader itself walks it.
    first = struct.unpack_from("<I", data, 4)[0]
    (count,) = struct.unpack_from("<H", data, first)
    for n in range(count):
        at = first + 2 + 12 * n
        if struct.unpack_from("<H", data, at)[0] == geotiff.COMPRESSION:
            struct.pack_into("<H", data, at + 8, 5)  # LZW
    assert geotiff.read(load("strips_uncompressed")[0])
    with pytest.raises(geotiff.GeoTiffError, match="compression 5"):
        geotiff.read(bytes(data))
