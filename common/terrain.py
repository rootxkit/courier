"""Ground elevation for a position, from locally installed DEM tiles. P5-00.

Height above ground is not in any telemetry (`GLOBAL_POSITION_INT` gives
height above home, and ArduPilot's `TERRAIN_REPORT` was measured reporting
0.0 m through a whole real flight with no terrain loaded). It is computed:

    height_above_ground_m = alt_amsl_m - Terrain.elevation(lat, lon).elevation_m

## Where the tiles come from

`tools/terrain_fetch.py` converts Copernicus DEM tiles (GLO-30 where the
public release has them, GLO-90 elsewhere) into one PGM per 1 x 1 degree
cell, named after its south-west corner (`N41E044.pgm`), and writes an
`index.json` of every cell it was asked for: the dataset used, or "sea" for
a cell neither dataset has a tile for (Copernicus has no ocean tiles; the
height there is 0). A cell not in the index was never fetched, and its
elevation is unknown, not zero.

## What the number is

- Height of the **surface**, not the ground: Copernicus is a surface model,
  so over a city it is the roofs and over a forest the canopy. For clearance
  that errs safe (the aircraft looks lower than it is); for an altitude
  limit it means the limit is applied above the roofs.
- Orthometric height on **EGM2008**, as Copernicus publishes it.
- Accurate to a few metres, and interpolated between samples 30 or 90 m
  apart, so steep slopes add error. The dataset and spacing come with every
  answer so a caller can say which it had.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from common import pgm
from common.pgm import Pgm

# Stored samples: elevation = OFFSET + SCALE * value; NODATA is reserved.
OFFSET_M = -500.0
SCALE_M = 0.2
NODATA = 0xFFFF
SEA = "sea"


class TerrainFileError(ValueError):
    """A tile or index that is not what tools/terrain_fetch.py writes."""


@dataclass(frozen=True, slots=True)
class Elevation:
    elevation_m: float
    # "COP-DEM GLO-30", "COP-DEM GLO-90", or "sea".
    dataset: str
    # Distance between samples along a meridian; 0 at sea.
    spacing_m: float


def cell_name(lat_deg: float, lon_deg: float) -> str:
    lat = math.floor(lat_deg)
    lon = math.floor(lon_deg)
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


@dataclass(frozen=True)
class TerrainTile:
    grid: Pgm
    dataset: str
    lat_first_deg: float
    lon_first_deg: float
    lat_step_deg: float
    lon_step_deg: float

    @classmethod
    def parse(cls, data: bytes) -> TerrainTile:
        try:
            grid = pgm.parse(data)
            offset, scale = grid.number("Offset"), grid.number("Scale")
            tile = cls(
                grid=grid,
                dataset=grid.header.get("Dataset", ""),
                lat_first_deg=grid.number("LatFirst"),
                lon_first_deg=grid.number("LonFirst"),
                lat_step_deg=grid.number("LatStep"),
                lon_step_deg=grid.number("LonStep"),
            )
        except pgm.PgmError as error:
            raise TerrainFileError(str(error)) from error
        if (offset, scale) != (OFFSET_M, SCALE_M):
            raise TerrainFileError(f"Offset/Scale {offset}/{scale}, not this format's")
        if not tile.dataset:
            raise TerrainFileError("no Dataset in the header")
        if tile.lat_step_deg <= 0 or tile.lon_step_deg <= 0:
            raise TerrainFileError("steps must be positive")
        return tile

    def elevation_m(self, lat_deg: float, lon_deg: float) -> float | None:
        """Bilinear between the four surrounding samples; None if any is
        nodata. At the tile's edge the nearest samples are used: the next
        tile's first row is not read."""
        fy = (self.lat_first_deg - lat_deg) / self.lat_step_deg
        fx = (lon_deg - self.lon_first_deg) / self.lon_step_deg
        fy = min(max(fy, 0.0), self.grid.height - 1.0)
        fx = min(max(fx, 0.0), self.grid.width - 1.0)
        iy = min(int(fy), self.grid.height - 2)
        ix = min(int(fx), self.grid.width - 2)
        fy -= iy
        fx -= ix
        corners = (
            self.grid.raw(ix, iy),
            self.grid.raw(ix + 1, iy),
            self.grid.raw(ix, iy + 1),
            self.grid.raw(ix + 1, iy + 1),
        )
        if NODATA in corners:
            return None
        v00, v01, v10, v11 = corners
        top = (1 - fx) * v00 + fx * v01
        bottom = (1 - fx) * v10 + fx * v11
        return OFFSET_M + SCALE_M * ((1 - fy) * top + fy * bottom)

    @property
    def spacing_m(self) -> float:
        return self.lat_step_deg * 111_320.0


@dataclass
class Terrain:
    """The tiles in one directory, read when first needed and then kept."""

    directory: Path
    index: dict[str, str] = field(init=False)
    _tiles: dict[str, TerrainTile] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        path = self.directory / "index.json"
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TerrainFileError(f"cannot read {path}: {error}") from error
        cells = index.get("cells") if isinstance(index, dict) else None
        if not isinstance(cells, dict):
            raise TerrainFileError(f"{path} has no cells")
        self.index = {str(k): str(v) for k, v in cells.items()}

    def elevation(self, lat_deg: float, lon_deg: float) -> Elevation | None:
        """None when the position is outside what was fetched, or a sample
        around it is nodata: unknown, never zero."""
        if not (math.isfinite(lat_deg) and math.isfinite(lon_deg)):
            raise ValueError("latitude and longitude must be finite")
        name = cell_name(lat_deg, lon_deg)
        dataset = self.index.get(name)
        if dataset is None:
            return None
        if dataset == SEA:
            return Elevation(elevation_m=0.0, dataset=SEA, spacing_m=0.0)
        tile = self._tile(name)
        value = tile.elevation_m(lat_deg, lon_deg)
        if value is None:
            return None
        return Elevation(
            elevation_m=value, dataset=tile.dataset, spacing_m=tile.spacing_m
        )

    def _tile(self, name: str) -> TerrainTile:
        if name not in self._tiles:
            path = self.directory / f"{name}.pgm"
            try:
                self._tiles[name] = TerrainTile.parse(path.read_bytes())
            except OSError as error:
                raise TerrainFileError(
                    f"index lists {name} but {path}: {error}"
                ) from error
        return self._tiles[name]
