"""Turning MAVLink's scaled integers into SI, without writing a factor down.

CLAUDE.md forbids writing a wire-format number from memory, and a scaling
factor is exactly that kind of number: `1e7` and `1000` are both plausible for
a latitude, and picking the wrong one produces a coordinate that is off by a
factor of a hundred rather than an error.

So nothing here is remembered. **pymavlink states the unit of every field** in
`fieldunits_by_name` - `degE7` for a latitude, `mm` for an altitude, `cm/s` for
a velocity - and the factor is derived from that string by a rule:

    degE7   -> 1e-7 deg      (an explicit power-of-ten suffix)
    mm      -> 1e-3 m        (an SI prefix on a base unit)
    cm/s    -> 1e-2 m/s
    d%      -> 1e-1 %        (deci-percent: SYS_STATUS.load is 0-1000)
    hJ      -> 1e2  J        (hecto-joules: BATTERY_STATUS.energy_consumed)
    m/s     -> 1.0  m/s      (already SI)

`gateway/tests/test_units.py` checks every one of these against the units in
pymavlink's *XML message definitions*, which ship with the package and are a
different source from the generated Python. A test that read the same attribute
the code reads would only prove the attribute was spelled correctly twice.

## Sentinels

MAVLink signals "unknown" in-band, with the maximum value of the field's type
or with -1, and which fields do it is documented per field rather than by any
rule. `UINT16_MAX` in a `cdeg` heading converts to **655.35 degrees** - a
number no compass can produce, but one that sails through any range check
written in radians, and one that a mean or an interpolation will happily
absorb. Sentinels are therefore resolved to `None` before scaling, never after.
"""

from __future__ import annotations

import re
from typing import Any, Final

# SI prefixes MAVLink actually uses in the messages this Gateway reads. Not the
# whole SI table: a prefix that never appears is a prefix that cannot be
# checked against anything, and an unrecognised unit is an error here rather
# than a guess.
SI_PREFIX_FACTORS: Final[dict[str, float]] = {
    "u": 1e-6,
    "m": 1e-3,
    "c": 1e-2,
    "d": 1e-1,
    "h": 1e2,
    "k": 1e3,
}

# Units that are already what the system stores, so the factor is 1.
BASE_UNITS: Final[frozenset[str]] = frozenset(
    {
        "m",
        "m/s",
        "s",
        "V",
        "A",
        "Ah",
        "J",
        "deg",
        "degC",
        "%",
        "rad",
        "rad/s",
    }
)

# `degE7`, `degE5`: the exponent is written into the unit itself.
_EXPONENT_UNIT: Final = re.compile(r"\A(?P<base>[A-Za-z/]+)E(?P<exponent>\d+)\Z")


class UnitError(ValueError):
    """A MAVLink unit string this module has no rule for.

    Raised rather than defaulted to 1.0. A silent factor of one on a field that
    needed 1e-7 is the failure this whole module exists to prevent.
    """


def scale_and_unit(mavlink_unit: str) -> tuple[float, str]:
    """Return the multiplier that converts to SI, and the SI unit name.

    >>> scale_and_unit("degE7")
    (1e-07, 'deg')
    >>> scale_and_unit("cm/s")
    (0.01, 'm/s')
    """
    if not mavlink_unit:
        raise UnitError("empty unit string")

    if mavlink_unit in BASE_UNITS:
        return 1.0, mavlink_unit

    exponent_match = _EXPONENT_UNIT.match(mavlink_unit)
    if exponent_match is not None:
        base = exponent_match.group("base")
        if base not in BASE_UNITS:
            raise UnitError(f"{mavlink_unit!r} scales an unknown base unit {base!r}")
        return 10.0 ** -int(exponent_match.group("exponent")), base

    prefix, base = mavlink_unit[0], mavlink_unit[1:]
    if prefix in SI_PREFIX_FACTORS and base in BASE_UNITS:
        return SI_PREFIX_FACTORS[prefix], base

    raise UnitError(
        f"no rule for MAVLink unit {mavlink_unit!r}. Add it deliberately "
        f"rather than letting it default to a factor of one"
    )


def field_scale(message_class: Any, field_name: str) -> tuple[float, str]:
    """The factor and SI unit for one field of one message type.

    Reads pymavlink's own metadata, so adding a message to the hot path does
    not mean writing any numbers.
    """
    units: dict[str, str] = getattr(message_class, "fieldunits_by_name", {})
    if field_name not in units:
        raise UnitError(
            f"{message_class.__name__} has no documented unit for "
            f"{field_name!r}; it may be dimensionless, in which case convert "
            f"it explicitly rather than through this function"
        )
    return scale_and_unit(units[field_name])


def to_si(message: Any, field_name: str) -> float | None:
    """Read a field and convert it to SI, or `None` if it is a sentinel."""
    raw = getattr(message, field_name)
    if is_sentinel(type(message), field_name, raw):
        return None
    factor, _ = field_scale(type(message), field_name)
    return float(raw) * factor


# --- sentinels -------------------------------------------------------------
#
# Keyed by (message name, field name) because there is no rule: MAVLink
# documents "if unknown, set to UINT16_MAX" on some fields and "-1" on others,
# per field. Every entry here is checked against pymavlink's XML field
# description by `test_units.py`, which fails if a field documents a sentinel
# that this table does not carry - so the table cannot quietly fall behind the
# dialect.

UINT8_MAX: Final = 0xFF
UINT16_MAX: Final = 0xFFFF
INT16_MAX: Final = 0x7FFF
INT32_MAX: Final = 0x7FFFFFFF

SENTINELS: Final[dict[tuple[str, str], tuple[int, ...]]] = {
    ("GLOBAL_POSITION_INT", "hdg"): (UINT16_MAX,),
    ("SYS_STATUS", "voltage_battery"): (UINT16_MAX,),
    ("SYS_STATUS", "current_battery"): (-1,),
    ("SYS_STATUS", "battery_remaining"): (-1,),
    ("GPS_RAW_INT", "eph"): (UINT16_MAX,),
    ("GPS_RAW_INT", "epv"): (UINT16_MAX,),
    ("GPS_RAW_INT", "vel"): (UINT16_MAX,),
    ("GPS_RAW_INT", "satellites_visible"): (UINT8_MAX,),
    ("GPS_RAW_INT", "yaw"): (UINT16_MAX,),
    ("GPS_RAW_INT", "cog"): (UINT16_MAX,),
    ("BATTERY_STATUS", "temperature"): (INT16_MAX,),
    ("BATTERY_STATUS", "current_battery"): (-1,),
    ("BATTERY_STATUS", "current_consumed"): (-1,),
    ("BATTERY_STATUS", "energy_consumed"): (-1,),
    ("BATTERY_STATUS", "battery_remaining"): (-1,),
    # Per-cell arrays, and the two disagree about what "unused" looks like:
    # `voltages` uses UINT16_MAX, `voltages_ext` uses 0. Found by the test that
    # reads the XML descriptions, not by reading the code. Applied per element
    # by `conversion._pack_voltage`.
    ("BATTERY_STATUS", "voltages"): (UINT16_MAX,),
    ("BATTERY_STATUS", "voltages_ext"): (0,),
}


def is_sentinel(message_class: Any, field_name: str, value: object) -> bool:
    """Whether a raw value means "unknown" rather than a measurement.

    Checked *before* scaling. `UINT16_MAX` in a `cdeg` heading becomes 655.35
    degrees, which no compass produces but which every downstream average,
    interpolation and plot will absorb without complaint.
    """
    key = (getattr(message_class, "name", ""), field_name)
    unknown = SENTINELS.get(key)
    if unknown is None:
        return False
    return isinstance(value, int) and value in unknown
