"""Build the GeoTIFF fixtures for tools/tests/test_geotiff.py with GDAL. P5-00.

Kept out of the Python packages on purpose: it needs rasterio and numpy,
which are not project dependencies and are not type checked here.

    python infra/terrain/make_geotiff_fixtures.py OUT_DIR

Needs rasterio (which bundles GDAL) and numpy, in a separate environment:
neither is a project dependency. Each fixture is written by GDAL with the
layout Copernicus DEM tiles use (or a variation this reader must also
handle), and read back by GDAL; the expected values and the centre of the
first sample come from GDAL's reading, not from this project's reader.
"""

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

CASES = {
    # The Copernicus profile: tiled, DEFLATE, floating-point predictor,
    # PixelIsArea, with a partial last tile in both directions.
    "tiled_deflate_pred3_area": dict(
        tiled=True, blockxsize=32, blockysize=32, compress="deflate", predictor=3
    ),
    "tiled_deflate_pred3_point": dict(
        tiled=True, blockxsize=32, blockysize=32, compress="deflate", predictor=3,
        point=True,
    ),
    "strips_uncompressed": dict(tiled=False),
    "strips_deflate_pred3_nodata": dict(
        tiled=False, compress="deflate", predictor=3, nodata=-32767.0
    ),
}


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    width, height = 70, 45
    west, north, step = 44.0, 42.0, 1 / 1200
    for name, options in CASES.items():
        options = dict(options)
        point = options.pop("point", False)
        nodata = options.get("nodata")
        rows, cols = np.mgrid[0:height, 0:width]
        data = (500 + 3.5 * rows - 1.25 * cols + rng.normal(0, 7, (height, width))).astype(
            "float32"
        )
        data[0, 0] = -12.5
        data[5, 7] = 8848.86
        if nodata is not None:
            data[10, 10] = nodata
        path = out / f"{name}.tif"
        profile = dict(
            driver="GTiff", width=width, height=height, count=1, dtype="float32",
            crs="EPSG:4326", transform=from_origin(west, north, step, step), **options,
        )
        with rasterio.Env(GTIFF_POINT_GEO_IGNORE=False):
            with rasterio.open(path, "w", **profile) as dst:
                if point:
                    dst.update_tags(AREA_OR_POINT="Point")
                dst.write(data, 1)
        with rasterio.open(path) as src:
            back = src.read(1)
            t = src.transform
            expected = {
                "width": src.width,
                "height": src.height,
                # GDAL's transform is always corner-based; the first sample's
                # centre is half a pixel in.
                "lat_first_deg": t.f + t.e / 2,
                "lon_first_deg": t.c + t.a / 2,
                "lat_step_deg": -t.e,
                "lon_step_deg": t.a,
                "nodata": src.nodata,
                "area_or_point": src.tags().get("AREA_OR_POINT"),
                "values": [float(v) for v in back.ravel()],
            }
        (out / f"{name}.json").write_text(json.dumps(expected))
        print(name, path.stat().st_size, "bytes", expected["area_or_point"])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
