"""P1-03 property tests: SI in `drone_state`, and not a digit lost on the way.

The example tests in `test_conversion.py` check chosen values. These check the
properties over the whole wire range. Each value is:

1. encoded into a real MAVLink v2 frame by pymavlink,
2. parsed and assembled by the ingest pipeline into a `drone_state` row, and
3. mapped back to the integer on the wire.

The recovered integer must equal the original exactly, for every value.

**The inverse deliberately does not use `gateway.units`.** Recovering the raw
value with the same factor the code used would pass whatever that factor was.
The unit string is read from pymavlink, which is the authority on what the
wire carries. The factor for each unit is defined here, independently, from
the MAVLink unit definitions. A converter that stored raw units, or scaled by
the wrong power of ten, fails in both directions.

Generated with a fixed seed rather than with a property-testing library. The
cases are reproducible, the range extremes are always included, and no new
dependency is needed. A failure names the field and the value.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.drone_state import DroneStateRow
from gateway.tests.test_pipeline import EPOCH, build, heartbeat, link, record

SEED = 20260928
CASES = 400

# MAVLink unit -> factor to SI. Written from the unit definitions in the
# MAVLink message spec, not from `gateway.units`, so the two can disagree.
INDEPENDENT_FACTORS: dict[str, float] = {
    "degE7": 1e-7,
    "mm": 1e-3,
    "cm/s": 1e-2,
    "cdeg": 1e-2,
}

# GLOBAL_POSITION_INT field -> drone_state column, with the wire range to
# draw from. Latitude and longitude are held to the valid range of the earth,
# since ArduPilot never sends more; the others span their wire type.
POSITION_FIELDS: dict[str, tuple[str, int, int]] = {
    "lat": ("lat_deg", -900_000_000, 900_000_000),
    "lon": ("lon_deg", -1_800_000_000, 1_800_000_000),
    "alt": ("alt_amsl_m", -(2**31), 2**31 - 1),
    "relative_alt": ("alt_above_home_m", -(2**31), 2**31 - 1),
    "vx": ("vx_ms", -(2**15), 2**15 - 1),
    "vy": ("vy_ms", -(2**15), 2**15 - 1),
    "vz": ("vz_ms", -(2**15), 2**15 - 1),
    # UINT16_MAX means "unknown" and is covered by its own test; the valid
    # range is 0..35999 centidegrees.
    "hdg": ("heading_deg", 0, 35_999),
}


def unit_of(field: str) -> str:
    units: dict[str, str] = (
        mavlink.MAVLink_global_position_int_message.fieldunits_by_name
    )
    return units[field]


def cases(low: int, high: int, rng: random.Random) -> list[int]:
    """The extremes, their neighbours, zero if in range, then random draws."""
    edges = {low, low + 1, high - 1, high}
    if low <= 0 <= high:
        edges |= {value for value in (-1, 0, 1) if low <= value <= high}
    return sorted(edges) + [rng.randint(low, high) for _ in range(CASES)]


def position_frame(values: dict[str, int]) -> bytes:
    sender = link()
    return bytes(
        sender.global_position_int_encode(
            0,
            values["lat"],
            values["lon"],
            values["alt"],
            values["relative_alt"],
            values["vx"],
            values["vy"],
            values["vz"],
            values["hdg"],
        ).pack(sender)
    )


async def rows_for(frames: list[bytes]) -> list[DroneStateRow]:
    pipeline, _, _, _ = build()
    batch = [record(0, heartbeat())] + [
        record(n, frame, offset_ns=n * 1_000_000) for n, frame in enumerate(frames, 1)
    ]
    return await pipeline.process(EPOCH, batch)


def typical(rng: random.Random) -> dict[str, int]:
    """A frame whose every field is valid, so only the field under test varies.

    Latitude and longitude are kept off 0,0 so the fallback rule for a missing
    EKF report (exact 0,0 means no position) does not null them.
    """
    return {
        "lat": 417_151_000,
        "lon": 448_271_000,
        "alt": 450_000,
        "relative_alt": 60_000,
        "vx": 0,
        "vy": 0,
        "vz": 0,
        "hdg": rng.randint(0, 35_999),
    }


def test_every_field_under_test_has_an_independent_factor() -> None:
    """If pymavlink changes a unit, this fails before a round trip lies."""
    for field in POSITION_FIELDS:
        assert unit_of(field) in INDEPENDENT_FACTORS, field


@pytest.mark.parametrize("field", sorted(POSITION_FIELDS))
async def test_a_field_survives_the_wire_and_comes_back_exact(field: str) -> None:
    column, low, high = POSITION_FIELDS[field]
    factor = INDEPENDENT_FACTORS[unit_of(field)]
    rng = random.Random(f"{SEED}-{field}")
    raws = cases(low, high, rng)

    frames = []
    for raw in raws:
        values = typical(rng)
        values[field] = raw
        frames.append(position_frame(values))

    rows = await rows_for(frames)

    assert len(rows) == len(raws)
    for raw, row in zip(raws, rows, strict=True):
        stored: Any = getattr(row, column)
        assert stored is not None, f"{field}={raw} stored as None"
        recovered = round(stored / factor)
        assert recovered == raw, (
            f"{field}: wire {raw} -> {column}={stored!r} -> {recovered}"
        )


@pytest.mark.parametrize("field", sorted(POSITION_FIELDS))
async def test_no_field_is_stored_in_raw_mavlink_units(field: str) -> None:
    """The presence half of the round trip: a value that is not zero must
    differ from its raw integer by exactly the unit factor. Storing the wire
    value unscaled would round-trip perfectly against a factor of one, so this
    pins that the factor is not one."""
    column, low, high = POSITION_FIELDS[field]
    factor = INDEPENDENT_FACTORS[unit_of(field)]
    rng = random.Random(f"{SEED}-raw-{field}")
    raw = rng.randint(max(low, 1000), high)
    values = typical(rng)
    values[field] = raw

    [row] = await rows_for([position_frame(values)])

    stored = getattr(row, column)
    assert stored != raw
    assert stored == pytest.approx(raw * factor, rel=1e-12)


async def test_an_unknown_heading_is_not_stored_as_a_number() -> None:
    values = typical(random.Random(SEED))
    values["hdg"] = 0xFFFF

    [row] = await rows_for([position_frame(values)])

    assert row.heading_deg is None
