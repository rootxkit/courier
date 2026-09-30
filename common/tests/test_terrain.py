"""Ground elevation from terrain tiles the test writes. P5-00."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import pgm
from common.terrain import (
    NODATA,
    OFFSET_M,
    SCALE_M,
    SEA,
    Terrain,
    TerrainFileError,
    TerrainTile,
    cell_name,
)

# A 5 x 5 tile over N41E044 at 0.25 degree spacing: first sample centre at
# 42.0 N 44.0 E, rows south, columns east. Elevation = 400 + 10 * row + column.
STEP = 0.25


def tile_bytes(
    *,
    dataset: str = "COP-DEM GLO-30",
    nodata_at: tuple[int, int] | None = None,
    offset: float = OFFSET_M,
) -> bytes:
    samples = bytearray()
    for row in range(5):
        for column in range(5):
            if (row, column) == nodata_at:
                stored = NODATA
            else:
                stored = round((400 + 10 * row + column - OFFSET_M) / SCALE_M)
            samples += stored.to_bytes(2, "big")
    header = {
        "Dataset": dataset,
        "Offset": str(offset),
        "Scale": str(SCALE_M),
        "LatFirst": "42.0",
        "LonFirst": "44.0",
        "LatStep": str(STEP),
        "LonStep": str(STEP),
    }
    return pgm.encode(5, 5, header, bytes(samples))


def install(tmp_path: Path, cells: dict[str, str], tiles: dict[str, bytes]) -> Terrain:
    (tmp_path / "index.json").write_text(json.dumps({"cells": cells}))
    for name, data in tiles.items():
        (tmp_path / f"{name}.pgm").write_bytes(data)
    return Terrain(tmp_path)


def test_cell_names_follow_the_south_west_corner() -> None:
    assert cell_name(41.7151, 44.8271) == "N41E044"
    assert cell_name(-0.5, -0.5) == "S01W001"
    assert cell_name(0.0, 0.0) == "N00E000"
    assert cell_name(-33.9, 151.2) == "S34E151"


def test_a_sample_centre_gives_its_own_value(tmp_path: Path) -> None:
    terrain = install(
        tmp_path, {"N41E044": "COP-DEM GLO-30"}, {"N41E044": tile_bytes()}
    )

    here = terrain.elevation(42.0 - 2 * STEP, 44.0 + 3 * STEP)

    assert here is not None
    assert here.elevation_m == pytest.approx(400 + 10 * 2 + 3)
    assert here.dataset == "COP-DEM GLO-30"
    assert here.spacing_m == pytest.approx(STEP * 111_320.0)


def test_between_samples_is_bilinear(tmp_path: Path) -> None:
    terrain = install(
        tmp_path, {"N41E044": "COP-DEM GLO-30"}, {"N41E044": tile_bytes()}
    )

    here = terrain.elevation(42.0 - 1.5 * STEP, 44.0 + 2.25 * STEP)

    assert here is not None
    assert here.elevation_m == pytest.approx(400 + 10 * 1.5 + 2.25)


def test_beyond_the_last_sample_the_edge_is_used() -> None:
    """Copernicus drops each tile's shared east and south edge, so the last
    strip of a cell lies past its last sample: the edge sample is used."""
    tile = TerrainTile.parse(tile_bytes())

    assert tile.elevation_m(40.9, 45.3) == pytest.approx(400 + 10 * 4 + 4)
    assert tile.elevation_m(42.2, 43.9) == pytest.approx(400)


def test_no_data_around_the_position_is_unknown_not_zero(tmp_path: Path) -> None:
    terrain = install(
        tmp_path,
        {"N41E044": "COP-DEM GLO-30"},
        {"N41E044": tile_bytes(nodata_at=(1, 1))},
    )

    assert terrain.elevation(42.0 - 0.5 * STEP, 44.0 + 0.5 * STEP) is None
    assert terrain.elevation(42.0 - 3.5 * STEP, 44.0 + 3.5 * STEP) is not None


def test_a_cell_never_fetched_is_unknown(tmp_path: Path) -> None:
    terrain = install(
        tmp_path, {"N41E044": "COP-DEM GLO-30"}, {"N41E044": tile_bytes()}
    )

    assert terrain.elevation(41.5, 45.5) is None


def test_the_sea_is_zero_and_says_so(tmp_path: Path) -> None:
    terrain = install(tmp_path, {"N42E039": SEA}, {})

    at_sea = terrain.elevation(42.5, 39.5)

    assert at_sea is not None
    assert (at_sea.elevation_m, at_sea.dataset) == (0.0, SEA)


def test_a_listed_tile_that_is_missing_is_an_error_not_a_guess(tmp_path: Path) -> None:
    terrain = install(tmp_path, {"N41E044": "COP-DEM GLO-30"}, {})

    with pytest.raises(TerrainFileError, match="index lists N41E044"):
        terrain.elevation(41.5, 44.5)


def test_a_non_finite_position_is_refused(tmp_path: Path) -> None:
    terrain = install(tmp_path, {}, {})

    with pytest.raises(ValueError, match="finite"):
        terrain.elevation(float("nan"), 44.0)


def test_no_index_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TerrainFileError, match="cannot read"):
        Terrain(tmp_path)
    (tmp_path / "index.json").write_text("[]")
    with pytest.raises(TerrainFileError, match="no cells"):
        Terrain(tmp_path)


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (tile_bytes(offset=-400.0), "Offset/Scale"),
        (tile_bytes(dataset=""), "Dataset"),
        (tile_bytes().replace(b"# LatStep 0.25", b"# LatStep -0.25"), "positive"),
        (tile_bytes().replace(b"# LonFirst 44.0\n", b""), "LonFirst"),
    ],
    ids=["other-scale", "no-dataset", "negative-step", "no-lon"],
)
def test_a_tile_this_format_did_not_write_is_refused(
    data: bytes, complaint: str
) -> None:
    assert TerrainTile.parse(tile_bytes())
    with pytest.raises(TerrainFileError, match=complaint):
        TerrainTile.parse(data)
