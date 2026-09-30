"""Copernicus tiles to terrain tiles, without the network. P5-00."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from common.terrain import Terrain, TerrainTile
from tools import geotiff, terrain_fetch

FIXTURES = Path(__file__).parent / "data" / "geotiff"


def fixture(name: str = "tiled_deflate_pred3_area") -> tuple[bytes, dict[str, Any]]:
    return (FIXTURES / f"{name}.tif").read_bytes(), json.loads(
        (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    )


def test_tile_urls_follow_the_bucket_readme() -> None:
    # The readme's own example: s3://copernicus-dem-90m/Copernicus_DSM_COG_30_S90_00_W178_00_DEM/
    url = terrain_fetch.tile_url(terrain_fetch.DATASETS["glo90"], -90, -178)

    assert url == (
        "https://copernicus-dem-90m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_30_S90_00_W178_00_DEM/Copernicus_DSM_COG_30_S90_00_W178_00_DEM.tif"
    )
    assert "COG_10_N41_00_E044_00" in terrain_fetch.tile_url(
        terrain_fetch.DATASETS["glo30"], 41, 44
    )


def test_cells_cover_the_box() -> None:
    assert terrain_fetch.cells((39.9, 41.0, 46.8, 43.6)) == [
        (lat, lon) for lat in (41, 42, 43) for lon in range(39, 47)
    ]


def test_a_converted_tile_reads_back_the_values_gdal_read(tmp_path: Path) -> None:
    data, expected = fixture()
    raster = geotiff.read(data)
    dataset = terrain_fetch.DATASETS["glo30"]

    tile = TerrainTile.parse(terrain_fetch.convert(raster, data, dataset, "https://x"))

    for row, column in ((0, 0), (5, 7), (20, 33), (44, 69)):
        value = expected["values"][row * expected["width"] + column]
        lat = expected["lat_first_deg"] - row * expected["lat_step_deg"]
        lon = expected["lon_first_deg"] + column * expected["lon_step_deg"]
        # 0.2 m quantisation: within 0.1 m of what GDAL read.
        assert tile.elevation_m(lat, lon) == pytest.approx(value, abs=0.1001)
    assert tile.dataset == "COP-DEM GLO-30"
    assert tile.grid.header["Source"] == "https://x"


def test_no_data_stays_no_data() -> None:
    data, expected = fixture("strips_deflate_pred3_nodata")
    raster = geotiff.read(data)
    tile = TerrainTile.parse(
        terrain_fetch.convert(raster, data, terrain_fetch.DATASETS["glo90"], "u")
    )
    lat = expected["lat_first_deg"] - 10 * expected["lat_step_deg"]
    lon = expected["lon_first_deg"] + 10 * expected["lon_step_deg"]

    assert tile.elevation_m(lat, lon) is None


def test_an_elevation_outside_the_format_is_refused() -> None:
    data, _ = fixture()
    raster = geotiff.read(data)
    raster.values[3] = 20_000.0

    with pytest.raises(ValueError, match="outside"):
        terrain_fetch.to_samples(raster)


def test_a_tile_in_the_wrong_place_is_refused() -> None:
    raster = geotiff.read(fixture()[0])  # first sample near 42.0 N 44.0 E

    terrain_fetch.check_placement(raster, 41, 44)
    with pytest.raises(ValueError, match="latitude"):
        terrain_fetch.check_placement(raster, 40, 44)
    with pytest.raises(ValueError, match="longitude"):
        terrain_fetch.check_placement(raster, 41, 45)


def test_a_fetch_writes_tiles_an_index_and_the_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GLO-30 for one cell, GLO-90 for the next, nothing for the third."""
    data, _ = fixture()
    served = {
        terrain_fetch.tile_url(terrain_fetch.DATASETS["glo30"], 41, 44): data,
        terrain_fetch.tile_url(terrain_fetch.DATASETS["glo90"], 41, 45): data,
    }
    monkeypatch.setattr(terrain_fetch, "download", served.get)
    monkeypatch.setattr(terrain_fetch, "check_placement", lambda raster, lat, lon: None)

    code = terrain_fetch.main(["--bbox", "44.1,41.1,46.9,41.9", "--out", str(tmp_path)])

    assert code == 0
    index = json.loads((tmp_path / "index.json").read_text())["cells"]
    assert index == {
        "N41E044": "COP-DEM GLO-30",
        "N41E045": "COP-DEM GLO-90",
        "N41E046": "sea",
    }
    source = json.loads((tmp_path / "SOURCE.json").read_text())
    assert any("WorldDEM-30" in a for a in source["attribution"])
    assert any("WorldDEM-90" in a for a in source["attribution"])
    assert "liability" in source
    assert Terrain(tmp_path).elevation(41.95, 44.05) is not None


def test_without_glo90_a_missing_tile_is_unknown_not_sea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terrain_fetch, "download", lambda url: None)

    terrain_fetch.main(
        ["--bbox", "44.1,41.1,44.9,41.9", "--out", str(tmp_path), "--datasets", "glo30"]
    )

    assert json.loads((tmp_path / "index.json").read_text())["cells"] == {}


def test_the_dry_run_downloads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(url: str) -> bytes | None:
        raise AssertionError("downloaded in a dry run")

    monkeypatch.setattr(terrain_fetch, "download", refuse)

    terrain_fetch.main(
        ["--bbox", "44.1,41.1,44.9,41.9", "--out", str(tmp_path), "--dry-run"]
    )

    assert "would try" in capsys.readouterr().out
    assert not (tmp_path / "index.json").exists()


def test_an_interrupted_run_keeps_what_it_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, _ = fixture()
    url = terrain_fetch.tile_url(terrain_fetch.DATASETS["glo30"], 41, 44)
    monkeypatch.setattr(terrain_fetch, "check_placement", lambda raster, lat, lon: None)
    monkeypatch.setattr(terrain_fetch, "download", {url: data}.get)
    terrain_fetch.main(["--bbox", "44.1,41.1,44.9,41.9", "--out", str(tmp_path)])

    def refuse(url: str) -> bytes | None:
        raise AssertionError("downloaded a tile that was already there")

    monkeypatch.setattr(terrain_fetch, "download", refuse)
    terrain_fetch.main(["--bbox", "44.1,41.1,44.9,41.9", "--out", str(tmp_path)])

    index = json.loads((tmp_path / "index.json").read_text())["cells"]
    assert index == {"N41E044": "COP-DEM GLO-30"}
    source = json.loads((tmp_path / "SOURCE.json").read_text())
    assert source["tiles"]["N41E044"]["url"] == url
