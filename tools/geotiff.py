"""Read a single-band float32 GeoTIFF, as Copernicus DEM tiles ship. P5-00.

Only what `tools/terrain_fetch.py` needs, and nothing it does not: one
image (the first directory; a COG's overviews follow it and are ignored),
one sample per pixel, 32-bit IEEE float, tiled or in strips, stored or
DEFLATE-compressed, with no predictor or the floating-point predictor, and
the GeoTIFF tags that place the raster on WGS-84 degrees. Anything else is
refused by name rather than read wrongly.

Standard library only, so the fetch runs on a bare Python. It is not fast
(seconds per 30 m tile) and does not need to be: tiles are converted once,
when they are fetched, and never read at run time.

The floating-point predictor (Predictor = 3) is undone as libtiff undoes it
(`libtiff/tif_predict.c`, fpAcc): each row of a tile is a sequence of bytes
differenced left to right, and once summed back those bytes are four byte
planes, most significant first. `tests/test_geotiff.py` checks every value of
rasters GDAL wrote against GDAL's own reading of them.
"""

from __future__ import annotations

import itertools
import struct
import zlib
from array import array
from dataclasses import dataclass
from typing import Any

# TIFF tags (TIFF 6.0 and the GeoTIFF 1.1 specification).
IMAGE_WIDTH = 256
IMAGE_LENGTH = 257
BITS_PER_SAMPLE = 258
COMPRESSION = 259
SAMPLES_PER_PIXEL = 277
ROWS_PER_STRIP = 278
STRIP_OFFSETS = 273
STRIP_BYTE_COUNTS = 279
PLANAR_CONFIGURATION = 284
PREDICTOR = 317
TILE_WIDTH = 322
TILE_LENGTH = 323
TILE_OFFSETS = 324
TILE_BYTE_COUNTS = 325
SAMPLE_FORMAT = 339
MODEL_PIXEL_SCALE = 33550
MODEL_TIEPOINT = 33922
GEO_KEY_DIRECTORY = 34735
GDAL_NODATA = 42113

GT_RASTER_TYPE_GEO_KEY = 1025
GEOGRAPHIC_TYPE_GEO_KEY = 2048
VERTICAL_CS_TYPE_GEO_KEY = 4096
EPSG_WGS84 = 4326
RASTER_PIXEL_IS_AREA = 1
RASTER_PIXEL_IS_POINT = 2

COMPRESSION_NONE = 1
COMPRESSION_DEFLATE = (8, 32946)
PREDICTOR_NONE = 1
PREDICTOR_FLOATING_POINT = 3
SAMPLE_FORMAT_IEEE_FLOAT = 3

# type -> (struct code, size)
_TYPES = {
    1: ("B", 1),
    2: ("c", 1),
    3: ("H", 2),
    4: ("I", 4),
    6: ("b", 1),
    8: ("h", 2),
    9: ("i", 4),
    11: ("f", 4),
    12: ("d", 8),
    16: ("Q", 8),
    17: ("q", 8),
}


class GeoTiffError(ValueError):
    """A file this reader does not read, and why."""


@dataclass(frozen=True)
class Raster:
    width: int
    height: int
    # Row-major from the north-west corner, width * height values.
    values: array[float]
    # The centre of the first (north-west) sample, and the spacing between
    # sample centres, in degrees. Rows run south, so the latitude step is
    # subtracted.
    lat_first_deg: float
    lon_first_deg: float
    lat_step_deg: float
    lon_step_deg: float
    nodata: float | None

    def at(self, row: int, column: int) -> float:
        return float(self.values[row * self.width + column])


def read(data: bytes) -> Raster:
    order, big = _header(data)
    first_ifd = struct.unpack_from(
        order + ("Q" if big else "I"), data, 8 if big else 4
    )[0]
    tags = _ifd(data, order, big, first_ifd)

    width = _one(tags, IMAGE_WIDTH)
    height = _one(tags, IMAGE_LENGTH)
    if _one(tags, SAMPLES_PER_PIXEL, 1) != 1:
        raise GeoTiffError("more than one sample per pixel")
    if (
        _one(tags, BITS_PER_SAMPLE) != 32
        or _one(tags, SAMPLE_FORMAT, 1) != SAMPLE_FORMAT_IEEE_FLOAT
    ):
        raise GeoTiffError("not 32-bit floating point samples")
    compression = _one(tags, COMPRESSION, COMPRESSION_NONE)
    if compression != COMPRESSION_NONE and compression not in COMPRESSION_DEFLATE:
        raise GeoTiffError(f"compression {compression} is not supported")
    predictor = _one(tags, PREDICTOR, PREDICTOR_NONE)
    if predictor not in (PREDICTOR_NONE, PREDICTOR_FLOATING_POINT):
        raise GeoTiffError(f"predictor {predictor} is not supported")
    if _one(tags, PLANAR_CONFIGURATION, 1) != 1:
        raise GeoTiffError("planar configuration is not supported")

    if TILE_WIDTH in tags:
        block_w, block_h = _one(tags, TILE_WIDTH), _one(tags, TILE_LENGTH)
        offsets, counts = tags[TILE_OFFSETS], tags[TILE_BYTE_COUNTS]
    else:
        block_w = width
        block_h = min(_one(tags, ROWS_PER_STRIP, height), height)
        offsets, counts = tags[STRIP_OFFSETS], tags[STRIP_BYTE_COUNTS]
    across = -(-width // block_w)
    down = -(-height // block_h)
    if len(offsets) != across * down or len(counts) != across * down:
        raise GeoTiffError("block count does not match the raster size")

    values = array("f", bytes(4 * width * height))
    for index, (offset, count) in enumerate(zip(offsets, counts, strict=True)):
        raw = data[offset : offset + count]
        if compression != COMPRESSION_NONE:
            raw = zlib.decompress(raw)
        rows_in_block = block_h
        # A strip at the bottom may be short; a tile is always full size.
        if TILE_WIDTH not in tags:
            rows_in_block = min(block_h, height - (index // across) * block_h)
        if len(raw) < 4 * block_w * rows_in_block:
            raise GeoTiffError(f"block {index} is shorter than its size")
        block = _unpredict(raw, block_w, rows_in_block, predictor, order)
        top = (index // across) * block_h
        left = (index % across) * block_w
        for r in range(rows_in_block):
            row = top + r
            if row >= height:
                break
            n = min(block_w, width - left)
            start = row * width + left
            values[start : start + n] = block[r * block_w : r * block_w + n]

    lat, lon, dlat, dlon = _georeference(tags)
    nodata = None
    if GDAL_NODATA in tags:
        text = (
            bytes(b"".join(tags[GDAL_NODATA])).rstrip(b"\x00").decode("ascii").strip()
        )
        if text:
            nodata = float(text)
    return Raster(width, height, values, lat, lon, dlat, dlon, nodata)


def _header(data: bytes) -> tuple[str, bool]:
    if data[:2] == b"II":
        order = "<"
    elif data[:2] == b"MM":
        order = ">"
    else:
        raise GeoTiffError("not a TIFF file")
    magic = struct.unpack_from(order + "H", data, 2)[0]
    if magic == 42:
        return order, False
    if magic == 43:
        return order, True
    raise GeoTiffError(f"TIFF magic {magic}")


def _ifd(data: bytes, order: str, big: bool, offset: int) -> dict[int, list[Any]]:
    count_fmt, entry_size, inline = ("Q", 20, 8) if big else ("H", 12, 4)
    (count,) = struct.unpack_from(order + count_fmt, data, offset)
    position = offset + (8 if big else 2)
    tags: dict[int, list[Any]] = {}
    for _ in range(count):
        if big:
            tag, kind, n = struct.unpack_from(order + "HHQ", data, position)
            value_at = position + 12
        else:
            tag, kind, n = struct.unpack_from(order + "HHI", data, position)
            value_at = position + 8
        position += entry_size
        if kind not in _TYPES:
            continue
        code, size = _TYPES[kind]
        if size * n > inline:
            value_at = struct.unpack_from(
                order + ("Q" if big else "I"), data, value_at
            )[0]
        tags[tag] = list(struct.unpack_from(f"{order}{n}{code}", data, value_at))
    return tags


def _one(tags: dict[int, list[Any]], tag: int, default: int | None = None) -> int:
    if tag not in tags:
        if default is None:
            raise GeoTiffError(f"tag {tag} is missing")
        return default
    return int(tags[tag][0])


def _add_byte(a: int, b: int) -> int:
    return (a + b) & 0xFF


def _unpredict(
    raw: bytes, width: int, rows: int, predictor: int, order: str
) -> array[float]:
    row_bytes = 4 * width
    out = array("f")
    for r in range(rows):
        row = raw[r * row_bytes : (r + 1) * row_bytes]
        if predictor == PREDICTOR_FLOATING_POINT:
            sums: list[int] = list(itertools.accumulate(row, _add_byte))
            summed = bytes(sums)
            # Byte planes, most significant first: big-endian floats.
            interleaved = bytearray(row_bytes)
            for plane in range(4):
                interleaved[plane::4] = summed[plane * width : (plane + 1) * width]
            out.extend(struct.unpack(f">{width}f", interleaved))
        else:
            out.extend(struct.unpack(f"{order}{width}f", row))
    return out


def _georeference(tags: dict[int, list[Any]]) -> tuple[float, float, float, float]:
    if MODEL_PIXEL_SCALE not in tags or MODEL_TIEPOINT not in tags:
        raise GeoTiffError("no ModelPixelScale or ModelTiepoint: not georeferenced")
    scale_x, scale_y = tags[MODEL_PIXEL_SCALE][0], tags[MODEL_PIXEL_SCALE][1]
    i, j, _, x, y, _ = tags[MODEL_TIEPOINT][:6]
    keys = _geo_keys(tags)
    if keys.get(GEOGRAPHIC_TYPE_GEO_KEY) != EPSG_WGS84:
        raise GeoTiffError(
            f"geographic CRS {keys.get(GEOGRAPHIC_TYPE_GEO_KEY)}, expected EPSG:4326"
        )
    raster_type = keys.get(GT_RASTER_TYPE_GEO_KEY, RASTER_PIXEL_IS_AREA)
    # The tiepoint ties raster position (i, j) to model (x, y). With
    # PixelIsArea, raster (0, 0) is the corner of the first pixel; with
    # PixelIsPoint, it is the centre of the first sample.
    lon0 = x - i * scale_x
    lat0 = y + j * scale_y
    if raster_type == RASTER_PIXEL_IS_AREA:
        lon0 += scale_x / 2
        lat0 -= scale_y / 2
    elif raster_type != RASTER_PIXEL_IS_POINT:
        raise GeoTiffError(f"raster type {raster_type}")
    return lat0, lon0, scale_y, scale_x


def _geo_keys(tags: dict[int, list[Any]]) -> dict[int, int]:
    """GeoKeyDirectory entries whose value is inline (location 0).

    The directory is a header of four shorts, then one entry of four shorts
    per key: key id, where the value is (0: in the entry itself), count, value.
    """
    directory = tags.get(GEO_KEY_DIRECTORY, [])
    return {
        int(directory[k]): int(directory[k + 3])
        for k in range(4, len(directory) - 3, 4)
        if directory[k + 1] == 0
    }


def vertical_datum(data: bytes) -> int | None:
    """The EPSG code of the vertical CRS, if the file declares one."""
    order, big = _header(data)
    first_ifd = struct.unpack_from(
        order + ("Q" if big else "I"), data, 8 if big else 4
    )[0]
    return _geo_keys(_ifd(data, order, big, first_ifd)).get(VERTICAL_CS_TYPE_GEO_KEY)
