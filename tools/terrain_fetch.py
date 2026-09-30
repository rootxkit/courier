"""Fetch Copernicus DEM tiles for an area and convert them for common/terrain.py. P5-00.

    python -m tools.terrain_fetch --bbox 39.9,41.0,46.8,43.6 --out local/terrain

For every 1 x 1 degree cell the box touches, takes the first dataset that
has a tile for it, in the order given by --datasets (default: GLO-30, then
GLO-90). The public GLO-30 release withholds some countries' tiles; GLO-90
covers the whole world except the sea. A cell neither has is recorded as
sea, where Copernicus says the height is 0.

Writes, into --out:
  <cell>.pgm   16-bit samples, elevation = -500 m + 0.2 m * value, 65535
               for no data; the header names the dataset, the source URL,
               its SHA-256 and the sample grid
  index.json   every cell asked for, and what it holds
  SOURCE.json  where and when it came from, and the attribution the
               Copernicus licence requires

Tiles come from the AWS Open Data copies (copernicus-dem-30m, -90m), which
serve the Copernicus DEM 2021 release as Cloud Optimised GeoTIFFs. Standard
library only; conversion takes seconds per tile and happens once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from common import pgm
from common.terrain import NODATA, OFFSET_M, SCALE_M, SEA, TerrainTile, cell_name
from tools import geotiff


@dataclass(frozen=True)
class Dataset:
    name: str
    bucket: str
    arc_seconds: int


DATASETS = {
    "glo30": Dataset(
        "COP-DEM GLO-30", "https://copernicus-dem-30m.s3.amazonaws.com", 10
    ),
    "glo90": Dataset(
        "COP-DEM GLO-90", "https://copernicus-dem-90m.s3.amazonaws.com", 30
    ),
}

# The licence's notices for adapted data (the tiles are requantised to
# 0.2 m), from the Copernicus Contributing Mission licence for COP-DEM.
ATTRIBUTION = {
    "COP-DEM GLO-30": (
        "produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) "
        "Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by "
        "the European Union and ESA; all rights reserved"
    ),
    "COP-DEM GLO-90": (
        "produced using Copernicus WorldDEM-90 (c) DLR e.V. 2010-2014 and (c) "
        "Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by "
        "the European Union and ESA; all rights reserved"
    ),
}
LIABILITY = (
    "The organisations in charge of the Copernicus programme by law or by "
    "delegation do not incur any liability for any use of the Copernicus "
    "WorldDEM-30 or WorldDEM-90"
)
# Copernicus DEM heights are orthometric on EGM2008 (EPSG:3855).
EGM2008_HEIGHT = 3855
MAX_CELLS = 400


def tile_url(dataset: Dataset, lat: int, lon: int) -> str:
    ns = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00"
    ew = f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00"
    name = f"Copernicus_DSM_COG_{dataset.arc_seconds}_{ns}_{ew}_DEM"
    return f"{dataset.bucket}/{name}/{name}.tif"


def cells(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    return [
        (lat, lon)
        for lat in range(math.floor(min_lat), math.ceil(max_lat))
        for lon in range(math.floor(min_lon), math.ceil(max_lon))
    ]


def download(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            data: bytes = response.read()
            return data
    except urllib.error.HTTPError as error:
        # S3 answers 403 or 404 for a key that does not exist.
        if error.code in (403, 404):
            return None
        raise


def to_samples(raster: geotiff.Raster) -> bytes:
    """Floats to big-endian uint16 at 0.2 m, 65535 for no data."""
    out = bytearray(2 * raster.width * raster.height)
    nodata = raster.nodata
    top = (NODATA - 1) * SCALE_M + OFFSET_M
    for i, value in enumerate(raster.values):
        if (nodata is not None and value == nodata) or math.isnan(value):
            stored = NODATA
        else:
            if not OFFSET_M <= value <= top:
                raise ValueError(f"elevation {value} m is outside {OFFSET_M}..{top} m")
            stored = int((value - OFFSET_M) / SCALE_M + 0.5)
        out[2 * i] = stored >> 8
        out[2 * i + 1] = stored & 0xFF
    return bytes(out)


def check_placement(raster: geotiff.Raster, lat: int, lon: int) -> None:
    """The tile covers the cell its name says, with the first sample at the
    north-west corner (within one sample)."""
    if not (lat + 1 - 2 * raster.lat_step_deg <= raster.lat_first_deg <= lat + 1):
        raise ValueError(
            f"first sample latitude {raster.lat_first_deg} is not at {lat + 1}"
        )
    if not (lon <= raster.lon_first_deg <= lon + 2 * raster.lon_step_deg):
        raise ValueError(
            f"first sample longitude {raster.lon_first_deg} is not at {lon}"
        )


def convert(raster: geotiff.Raster, data: bytes, dataset: Dataset, url: str) -> bytes:
    vertical = geotiff.vertical_datum(data)
    if vertical is not None and vertical != EGM2008_HEIGHT:
        raise ValueError(f"vertical datum EPSG:{vertical}, expected EGM2008 (3855)")
    header = {
        "Description": "courier terrain tile (tools/terrain_fetch.py)",
        "Dataset": dataset.name,
        "Source": url,
        "SHA256": hashlib.sha256(data).hexdigest(),
        "VerticalDatum": "EGM2008"
        if vertical == EGM2008_HEIGHT
        else "EGM2008 (per the dataset)",
        "Offset": str(OFFSET_M),
        "Scale": str(SCALE_M),
        "Nodata": str(NODATA),
        "LatFirst": repr(raster.lat_first_deg),
        "LonFirst": repr(raster.lon_first_deg),
        "LatStep": repr(raster.lat_step_deg),
        "LonStep": repr(raster.lon_step_deg),
    }
    return pgm.encode(raster.width, raster.height, header, to_samples(raster))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/terrain_fetch.py")
    parser.add_argument("--bbox", required=True, help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--datasets", default="glo30,glo90")
    parser.add_argument("--dry-run", action="store_true", help="list, do not download")
    parser.add_argument(
        "--refetch", action="store_true", help="download tiles already in --out again"
    )
    args = parser.parse_args(argv)

    try:
        bbox = tuple(float(v) for v in args.bbox.split(","))
    except ValueError:
        parser.error("--bbox must be four numbers")
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        parser.error("--bbox is min_lon,min_lat,max_lon,max_lat")
    order = [DATASETS[name] for name in args.datasets.split(",") if name in DATASETS]
    if not order:
        parser.error(f"--datasets: choose from {', '.join(DATASETS)}")
    wanted = cells((bbox[0], bbox[1], bbox[2], bbox[3]))
    if len(wanted) > MAX_CELLS:
        parser.error(
            f"{len(wanted)} cells; more than {MAX_CELLS} is not an operating area"
        )

    args.out.mkdir(parents=True, exist_ok=True)
    index: dict[str, str] = {}
    sources: dict[str, dict[str, str]] = {}
    for lat, lon in wanted:
        name = cell_name(lat, lon)
        existing = args.out / f"{name}.pgm"
        if existing.exists() and not args.refetch and not args.dry_run:
            # A tile written by an earlier, interrupted run. Written whole or
            # not at all (.part, then rename), so a present tile is complete.
            tile = TerrainTile.parse(existing.read_bytes())
            index[name] = tile.dataset
            sources[name] = {
                "url": tile.grid.header.get("Source", ""),
                "sha256": tile.grid.header.get("SHA256", ""),
            }
            print(f"{name}: kept from an earlier run ({tile.dataset})", flush=True)
            continue
        for dataset in order:
            url = tile_url(dataset, lat, lon)
            if args.dry_run:
                print(f"{name}: would try {url}")
                continue
            data = download(url)
            if data is None:
                continue
            raster = geotiff.read(data)
            check_placement(raster, lat, lon)
            part = args.out / f"{name}.pgm.part"
            part.write_bytes(convert(raster, data, dataset, url))
            part.replace(args.out / f"{name}.pgm")
            index[name] = dataset.name
            sources[name] = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
            print(f"{name}: {dataset.name}, {len(data)} bytes", flush=True)
            break
        else:
            if args.dry_run:
                continue
            # GLO-90 covers every land cell; only its absence means sea. With
            # GLO-90 not asked for, a missing tile is unknown, not sea.
            if DATASETS["glo90"] in order:
                index[name] = SEA
                print(f"{name}: no tile in any dataset; sea")
            else:
                print(f"{name}: no tile in {args.datasets}; left unknown")
    if args.dry_run:
        return 0
    fetched_at = datetime.now(tz=UTC).isoformat()
    (args.out / "index.json").write_text(
        json.dumps(
            {"bbox": list(bbox), "fetched_at": fetched_at, "cells": index}, indent=1
        )
    )
    (args.out / "SOURCE.json").write_text(
        json.dumps(
            {
                "attribution": sorted(
                    {ATTRIBUTION[d] for d in index.values() if d in ATTRIBUTION}
                ),
                "liability": LIABILITY,
                "fetched_at": fetched_at,
                "tiles": sources,
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
