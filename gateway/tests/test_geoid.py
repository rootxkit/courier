"""The geoid grid reader, against grids the test builds. P1-15.

A grid whose samples are a known function of row and column pins the
layout: which row is north, which column is longitude 0, the byte order, the
offset and scale, and that interpolation wraps in longitude.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from gateway.geoid import GeoidFileError, GeoidGrid

# 10-degree spacing: 36 columns (0..350 east), 19 rows (90 N .. 90 S).
WIDTH, HEIGHT = 36, 19
OFFSET_M, SCALE_M = -100.0, 0.01


def sample(row: int, column: int) -> int:
    return 1000 + 100 * row + column


def pgm(
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    header_extra: bytes = b"",
    maxval: int = 65535,
    truncate: int = 0,
) -> bytes:
    header = (
        b"P5\n# Description test grid\n"
        + f"# Offset {OFFSET_M}\n# Scale {SCALE_M}\n".encode()
        + header_extra
        + f"{width} {height}\n{maxval}\n".encode()
    )
    body = b"".join(
        sample(r, c).to_bytes(2, "big") for r in range(height) for c in range(width)
    )
    return header + body[: len(body) - truncate]


def expected(row: float, column: float) -> float:
    return OFFSET_M + SCALE_M * (1000 + 100 * row + column)


@pytest.fixture
def grid() -> GeoidGrid:
    return GeoidGrid.parse(pgm())


def test_row_zero_is_north_and_column_zero_is_greenwich(grid: GeoidGrid) -> None:
    assert grid.undulation_m(90.0, 0.0) == pytest.approx(expected(0, 0))
    assert grid.undulation_m(0.0, 0.0) == pytest.approx(expected(9, 0))
    assert grid.undulation_m(-90.0, 0.0) == pytest.approx(expected(18, 0))
    assert grid.undulation_m(0.0, 10.0) == pytest.approx(expected(9, 1))


def test_between_samples_is_bilinear(grid: GeoidGrid) -> None:
    # 5 N, 5 E is halfway between rows 8 and 9 and columns 0 and 1.
    assert grid.undulation_m(5.0, 5.0) == pytest.approx(expected(8.5, 0.5))
    assert grid.undulation_m(2.5, 7.5) == pytest.approx(expected(8.75, 0.75))


def test_longitude_wraps_past_the_last_column(grid: GeoidGrid) -> None:
    # 355 E is halfway between column 35 and column 0 (360 = 0).
    halfway = OFFSET_M + SCALE_M * (1000 + 100 * 9 + (35 + 0) / 2)

    assert grid.undulation_m(0.0, 355.0) == pytest.approx(halfway)
    assert grid.undulation_m(0.0, -5.0) == pytest.approx(halfway)


def test_a_non_finite_position_is_refused(grid: GeoidGrid) -> None:
    with pytest.raises(ValueError):
        grid.undulation_m(math.nan, 0.0)


def test_comment_lines_in_any_order_are_read() -> None:
    parsed = GeoidGrid.parse(pgm(header_extra=b"# MaxBilinearError 0.1\n"))

    assert parsed.undulation_m(0.0, 0.0) == pytest.approx(expected(9, 0))


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (b"P6\n" + pgm()[3:], "P5"),
        (pgm(maxval=255), "maxval"),
        (pgm(truncate=2), "bytes of samples"),
        (pgm(width=35), "raster"),
        (pgm(height=18, truncate=0), "raster"),
        (pgm().replace(b"# Scale 0.01\n", b""), "Scale"),
    ],
    ids=["not-p5", "maxval", "short", "odd-width", "even-height", "no-scale"],
)
def test_a_file_that_is_not_a_geoid_grid_is_refused(
    data: bytes, complaint: str
) -> None:
    assert GeoidGrid.parse(pgm())  # the same builder, unaltered, is accepted
    with pytest.raises(GeoidFileError, match=complaint):
        GeoidGrid.parse(data)


# --- the installed grid, against GeographicLib itself ----------------------------

# Produced by GeographicLib's own Geoid class (bilinear, egm96-15) on
# 2026-09-29, from the file infra/geoid/fetch_geoid.sh fetches. Over 5,005
# random points the largest difference from this reader was 4e-13 m; these are
# kept so the check can be repeated wherever the grid is installed.
GEOGRAPHICLIB_EGM96_15 = [
    (41.7151, 44.8271, 14.704748902079984),  # Tbilisi
    (41.6168, 41.6367, 20.907343384320001),  # Batumi
    (42.2679, 42.7181, 18.797618669280013),  # Kutaisi
    (0.0, 0.0, 17.162999999999997),
    (90.0, 0.0, 13.605000000000004),
    (-90.0, 0.0, -29.534999999999997),
    (38.628155, 269.779155, -31.608751617201605),
    (-14.621217, 305.021114, -2.9657334189564608),
    (46.874319, 102.448729, -43.616532163106015),
    (38.625473, 359.9995, 50.036272392023989),
    (27.9881, 86.925, -28.866312480000005),
    (51.5, -0.12, 45.951480000000004),
    (-45.0, 170.0, 7.6950000000000074),
    (64.1, -21.9, 66.390000000000015),
    (10.0, -84.0, 13.704000000000008),
]


def installed_grid() -> GeoidGrid:
    path = os.environ.get("GEOID_PATH")
    if not path or not Path(path).is_file():
        pytest.skip(
            "GEOID_PATH is not set; infra/geoid/fetch_geoid.sh installs the grid"
        )
    return GeoidGrid.load(Path(path))


@pytest.mark.parametrize(("lat", "lon", "undulation_m"), GEOGRAPHICLIB_EGM96_15)
def test_the_installed_grid_agrees_with_geographiclib(
    lat: float, lon: float, undulation_m: float
) -> None:
    assert installed_grid().undulation_m(lat, lon) == pytest.approx(
        undulation_m, abs=1e-9
    )


def test_a_grid_loads_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "grid.pgm"
    path.write_bytes(pgm())

    assert GeoidGrid.load(path).undulation_m(0.0, 0.0) == pytest.approx(expected(9, 0))


def test_blank_header_lines_are_skipped() -> None:
    data = pgm().replace(b"# Scale", b"\n# Scale", 1)

    assert GeoidGrid.parse(data).undulation_m(0.0, 0.0) == pytest.approx(expected(9, 0))


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (b"P5\n# Offset -1\n", "header ends"),
        (pgm().replace(b"36 19\n", b"36 x\n"), "unreadable"),
        (pgm().replace(b"# Scale 0.01", b"# Scale -0.01"), "positive"),
    ],
    ids=["no-end", "bad-size", "negative-scale"],
)
def test_more_malformed_headers_are_refused(data: bytes, complaint: str) -> None:
    with pytest.raises(GeoidFileError, match=complaint):
        GeoidGrid.parse(data)
