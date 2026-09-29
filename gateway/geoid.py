"""Geoid height: from height above the ellipsoid to height above sea level. P1-15.

Remote ID broadcasts height above the WGS-84 ellipsoid (HAE). The rest of the
system uses mean sea level (AMSL), and CLAUDE.md forbids mixing the two. The
difference is the geoid undulation N, so that

    alt_amsl_m = alt_hae_m - N(lat, lon)

N comes from the EGM96 model, in the grid GeographicLib distributes
(`egm96-15.pgm`: a 15-minute grid, fetched by `infra/geoid/fetch_geoid.sh`).
The file format and the interpolation are read from GeographicLib's own
reader (`src/Geoid.cpp`, `include/GeographicLib/Geoid.hpp`), not from memory:

- a binary PGM ("P5") whose comment lines carry `# Offset` and `# Scale`;
  a stored value v means N = Offset + Scale * v;
- 16-bit big-endian samples, row-major; row 0 is latitude +90, rows run
  south to latitude -90 (an odd number of rows, so the equator is one);
  column 0 is longitude 0, columns run east through 360 (an even number);
- bilinear interpolation between the four surrounding samples, wrapping in
  longitude.

`tests/test_geoid.py` checks this against a grid built by the test, and,
where the real grid is installed (GEOID_PATH), against values GeographicLib's
own Geoid class computed from it. Over 5,005 random points the largest
difference was 4e-13 m.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


class GeoidFileError(ValueError):
    """A file that is not a geoid grid this reader understands."""


@dataclass(frozen=True)
class GeoidGrid:
    width: int
    height: int
    offset_m: float
    scale_m: float
    samples: bytes  # width * height big-endian uint16

    @classmethod
    def load(cls, path: Path) -> GeoidGrid:
        return cls.parse(path.read_bytes())

    @classmethod
    def parse(cls, data: bytes) -> GeoidGrid:
        position = 0

        def line() -> bytes:
            nonlocal position
            end = data.find(b"\n", position)
            if end < 0:
                raise GeoidFileError("header ends before the data")
            text = data[position:end].strip()
            position = end + 1
            return text

        if line() != b"P5":
            raise GeoidFileError("not a binary PGM (P5)")
        offset: float | None = None
        scale: float | None = None
        while True:
            text = line()
            if not text:
                continue
            if not text.startswith(b"#"):
                break
            words = text.split()
            if len(words) >= 3 and words[1] == b"Offset":
                offset = float(words[2])
            elif len(words) >= 3 and words[1] == b"Scale":
                scale = float(words[2])
        try:
            width, height = (int(word) for word in text.split())
            maxval = int(line())
        except ValueError as error:
            raise GeoidFileError(
                f"unreadable raster size or maxval: {error}"
            ) from error
        if maxval != 0xFFFF:
            raise GeoidFileError(f"maxval {maxval}, expected 65535")
        if offset is None or scale is None:
            raise GeoidFileError("no Offset or Scale in the header")
        if scale <= 0:
            raise GeoidFileError("Scale must be positive")
        if width < 2 or height < 2 or width % 2 or not height % 2:
            raise GeoidFileError(f"a {width} x {height} raster is not a geoid grid")
        samples = data[position:]
        if len(samples) != 2 * width * height:
            raise GeoidFileError(
                f"{len(samples)} bytes of samples, expected {2 * width * height}"
            )
        return cls(width, height, offset, scale, samples)

    def _raw(self, ix: int, iy: int) -> int:
        ix %= self.width
        at = 2 * (iy * self.width + ix)
        return (self.samples[at] << 8) | self.samples[at + 1]

    def undulation_m(self, lat_deg: float, lon_deg: float) -> float:
        if not (math.isfinite(lat_deg) and math.isfinite(lon_deg)):
            raise ValueError("latitude and longitude must be finite")
        lat = max(-90.0, min(90.0, lat_deg))
        lon = lon_deg % 360.0
        fx = lon * self.width / 360.0
        fy = -lat * (self.height - 1) / 180.0
        ix = math.floor(fx)
        iy = min((self.height - 1) // 2 - 1, math.floor(fy))
        fx -= ix
        fy -= iy
        iy += (self.height - 1) // 2
        v00 = self._raw(ix, iy)
        v01 = self._raw(ix + 1, iy)
        v10 = self._raw(ix, iy + 1)
        v11 = self._raw(ix + 1, iy + 1)
        a = (1 - fx) * v00 + fx * v01
        b = (1 - fx) * v10 + fx * v11
        return self.offset_m + self.scale_m * ((1 - fy) * a + fy * b)
