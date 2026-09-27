"""Scaling factors, derived here from a different source than the code uses.

`gateway/units.py` reads `fieldunits_by_name` off pymavlink's generated Python
classes. These tests compare that against a table extracted from pymavlink's
**XML message definitions** by `tools/refresh_mavlink_units.py` and committed
as `data/mavlink_fields.json`. Two routes to the same fact: a test that read
the same attribute would only prove it was spelled correctly twice.

## Why the table is committed rather than read from the XML at test time

The XML is not installed on every platform. Measured 2026-09-24: pymavlink's
Windows wheels ship 19 definition files and the sdist 21, while the **manylinux
wheels ship none** - the same for 2.4.49 and 2.4.50, so it is packaging, not a
version change.

The first version of this test read the XML directly. It passed on a Windows
developer machine and failed on its first CI run with every field reporting
`xml=None`, because the helper skipped missing files and then compared
everything against nothing. Two faults: a source that does not exist
everywhere, and a silent degradation that turned "the data is absent" into
"every unit disagrees".

Both are fixed. The table is committed, so the comparison runs on every
platform, and `load_pinned_fields` fails loudly if it is missing or empty
rather than quietly comparing against an empty dict.

Where the XML *is* installed, `test_the_pinned_table_still_matches_the_xml`
checks the committed table against it, so the pin cannot silently drift from
upstream on the machines that can tell.
"""

from __future__ import annotations

import json
import pathlib
import xml.etree.ElementTree as ElementTree

import pymavlink
import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.classify import HOT_PATH_MESSAGE_NAMES
from gateway.units import (
    SENTINELS,
    UINT16_MAX,
    UnitError,
    field_scale,
    is_sentinel,
    scale_and_unit,
    to_si,
)

PINNED = pathlib.Path(__file__).parent / "data" / "mavlink_fields.json"
DEFINITIONS = pathlib.Path(pymavlink.__file__).parent / "message_definitions" / "v1.0"


def load_pinned_fields() -> dict[tuple[str, str], tuple[str, str]]:
    """(message, field) -> (units, description), from the committed table.

    Raises rather than returning an empty mapping. An empty table would make
    every comparison below pass or fail for the wrong reason, which is the
    exact failure this file already had once.
    """
    if not PINNED.exists():
        raise AssertionError(
            f"{PINNED} is missing. Regenerate it with "
            f"`python tools/refresh_mavlink_units.py` on a platform whose "
            f"pymavlink ships message_definitions (Windows wheel, or sdist)."
        )
    payload = json.loads(PINNED.read_text(encoding="utf-8"))
    messages = payload.get("messages", {})
    if not messages:
        raise AssertionError(f"{PINNED} contains no messages")

    flattened: dict[tuple[str, str], tuple[str, str]] = {}
    for message, fields in messages.items():
        for field, detail in fields.items():
            flattened[(message, field)] = (
                detail.get("units", ""),
                detail.get("description", ""),
            )
    return flattened


PINNED_FIELDS = load_pinned_fields()


def message_class(name: str) -> type:
    return getattr(mavlink, f"MAVLink_{name.lower()}_message")  # type: ignore[no-any-return]


# --- the two sources agree -------------------------------------------------


def test_the_generated_units_match_the_pinned_table() -> None:
    """The cross-check the whole module rests on.

    If these disagree, the installed pymavlink and the pinned definitions have
    drifted, and every factor derived from the former is suspect. Read the
    difference before regenerating: a changed unit is a changed scaling factor.
    """
    mismatches: list[str] = []
    for name in HOT_PATH_MESSAGE_NAMES:
        generated = getattr(message_class(name), "fieldunits_by_name", {})
        for field, unit in generated.items():
            pinned = PINNED_FIELDS.get((name, field), (None, ""))[0]
            if pinned != unit:
                mismatches.append(f"{name}.{field}: python={unit!r} pinned={pinned!r}")

    assert not mismatches, (
        "installed pymavlink disagrees with the pinned table. Read the "
        "difference, then regenerate with tools/refresh_mavlink_units.py: "
        + "; ".join(mismatches)
    )


@pytest.mark.skipif(
    not DEFINITIONS.exists(),
    reason="pymavlink ships no message_definitions here (Linux wheels omit them)",
)
def test_the_pinned_table_still_matches_the_xml() -> None:
    """Where the XML exists, check the pin against it.

    This is what stops the committed table drifting from upstream unnoticed.
    It cannot run on Linux, which is precisely why the table is committed -
    but it runs wherever the definitions are installed, and that is enough for
    the pin to be checked before it is trusted.
    """
    wanted = set(HOT_PATH_MESSAGE_NAMES)
    mismatches: list[str] = []
    for definition in (
        "minimal.xml",
        "standard.xml",
        "common.xml",
        "ardupilotmega.xml",
    ):
        path = DEFINITIONS / definition
        if not path.exists():
            continue
        for message in ElementTree.parse(path).iter("message"):
            name = message.get("name") or ""
            if name not in wanted:
                continue
            for field in message.iter("field"):
                key = (name, field.get("name") or "")
                if key not in PINNED_FIELDS:
                    continue
                units = field.get("units") or ""
                if PINNED_FIELDS[key][0] != units:
                    mismatches.append(
                        f"{key[0]}.{key[1]}: pinned={PINNED_FIELDS[key][0]!r} "
                        f"xml={units!r}"
                    )

    assert not mismatches, (
        "the pinned table has drifted from the XML; regenerate it: "
        + "; ".join(mismatches)
    )


def test_every_hot_path_unit_has_a_rule() -> None:
    """No field on the hot path may fall through to a guessed factor.

    `scale_and_unit` raises on an unknown unit rather than defaulting to 1.0,
    so this is what turns "we have not thought about that unit yet" into a
    failure at test time instead of a silent factor of one in flight.
    """
    unhandled: list[str] = []
    for name in HOT_PATH_MESSAGE_NAMES:
        for field, unit in getattr(
            message_class(name), "fieldunits_by_name", {}
        ).items():
            try:
                scale_and_unit(unit)
            except UnitError:
                unhandled.append(f"{name}.{field} has unit {unit!r}")

    assert not unhandled, "units with no rule:\n  " + "\n  ".join(unhandled)


# --- the factors themselves ------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "factor", "si"),
    [
        ("degE7", 1e-7, "deg"),
        ("degE5", 1e-5, "deg"),
        ("mm", 1e-3, "m"),
        ("cm/s", 1e-2, "m/s"),
        ("mm/s", 1e-3, "m/s"),
        ("cdeg", 1e-2, "deg"),
        ("cdegC", 1e-2, "degC"),
        ("mV", 1e-3, "V"),
        ("cA", 1e-2, "A"),
        ("mAh", 1e-3, "Ah"),
        ("hJ", 1e2, "J"),
        ("d%", 1e-1, "%"),
        ("c%", 1e-2, "%"),
        ("ms", 1e-3, "s"),
        ("us", 1e-6, "s"),
        ("m", 1.0, "m"),
        ("m/s", 1.0, "m/s"),
        ("deg", 1.0, "deg"),
        ("%", 1.0, "%"),
        ("s", 1.0, "s"),
    ],
)
def test_the_rule_produces_the_expected_factor(
    unit: str, factor: float, si: str
) -> None:
    assert scale_and_unit(unit) == pytest.approx((factor, si))


def test_an_unknown_unit_is_an_error_not_a_factor_of_one() -> None:
    """The whole point of raising.

    A silent 1.0 on a field that needed 1e-7 is a coordinate a hundred million
    times too large, and nothing in the pipeline would reject it.
    """
    with pytest.raises(UnitError, match="no rule for MAVLink unit"):
        scale_and_unit("furlong")


def test_a_prefix_on_an_unknown_base_is_still_an_error() -> None:
    with pytest.raises(UnitError):
        scale_and_unit("mFurlong")


def test_latitude_scales_by_1e7_without_anyone_writing_1e7() -> None:
    """Tbilisi, through the derived factor.

    41.7151 N is `417151000` in degE7. The assertion is on the degrees, so a
    wrong factor cannot hide behind a wrong expectation.
    """
    factor, unit = field_scale(message_class("GLOBAL_POSITION_INT"), "lat")

    assert unit == "deg"
    assert 417_151_000 * factor == pytest.approx(41.7151)


def test_millimetres_become_metres() -> None:
    factor, unit = field_scale(message_class("GLOBAL_POSITION_INT"), "alt")
    assert unit == "m"
    assert 450_000 * factor == pytest.approx(450.0)


def test_a_dimensionless_field_has_no_scale() -> None:
    """`fix_type` is an enum, not a measurement.

    Asking for its scale is a mistake worth an error rather than a 1.0, which
    would make an enum look like it had been converted.
    """
    with pytest.raises(UnitError, match="no documented unit"):
        field_scale(message_class("GPS_RAW_INT"), "fix_type")


# --- sentinels -------------------------------------------------------------


def test_every_documented_sentinel_is_in_the_table() -> None:
    """Read from the XML descriptions, where MAVLink documents them.

    There is no rule for which fields have sentinels or what they are - some
    say UINT16_MAX, some say -1 - so the table is written by hand and checked
    here against the dialect. A field that starts documenting one is a failure
    rather than a silent 65535 in the data.
    """
    missing: list[str] = []
    for name in HOT_PATH_MESSAGE_NAMES:
        for field, unit in getattr(
            message_class(name), "fieldunits_by_name", {}
        ).items():
            description = PINNED_FIELDS.get((name, field), ("", ""))[1]
            lowered = description.lower()
            documents_sentinel = (
                "uint16_max" in lowered
                or "uint8_max" in lowered
                or "int16_max" in lowered
                or "-1:" in lowered
                or "-1," in lowered
            )
            if documents_sentinel and (name, field) not in SENTINELS:
                missing.append(f"{name}.{field}: {description[:70]!r} (unit {unit!r})")

    assert not missing, (
        "fields documenting an unknown-value sentinel that units.py does not "
        "know about:\n  " + "\n  ".join(missing)
    )


def test_an_unknown_heading_resolves_to_none_not_655_degrees() -> None:
    """The failure this exists to prevent.

    UINT16_MAX in a cdeg heading scales to 655.35 degrees. No compass produces
    that, but it passes a naive range check and any average or interpolation
    absorbs it without complaint.
    """
    link = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    link.signing.sign_outgoing = False
    message = mavlink.MAVLink_global_position_int_message(
        time_boot_ms=0,
        lat=417151000,
        lon=448271000,
        alt=450000,
        relative_alt=60000,
        vx=0,
        vy=0,
        vz=0,
        hdg=UINT16_MAX,
    )

    assert to_si(message, "hdg") is None
    # And the number it would have been, so the test says what it prevented.
    factor, _ = field_scale(type(message), "hdg")
    assert UINT16_MAX * factor == pytest.approx(655.35)


def test_a_real_heading_still_converts() -> None:
    """The paired presence test.

    A sentinel check that swallowed every heading would pass the test above.
    """
    message = mavlink.MAVLink_global_position_int_message(
        time_boot_ms=0,
        lat=417151000,
        lon=448271000,
        alt=450000,
        relative_alt=60000,
        vx=0,
        vy=0,
        vz=0,
        hdg=9000,
    )
    assert to_si(message, "hdg") == pytest.approx(90.0)


def test_a_sentinel_is_checked_before_scaling_not_after() -> None:
    """Order matters.

    Scaled first, UINT16_MAX is 655.35 and no longer equal to any sentinel, so
    a check afterwards could never match.
    """
    assert is_sentinel(mavlink.MAVLink_global_position_int_message, "hdg", UINT16_MAX)
    assert not is_sentinel(mavlink.MAVLink_global_position_int_message, "hdg", 655.35)


def test_a_field_with_no_sentinel_is_never_treated_as_unknown() -> None:
    """Latitude has no way to say "unknown", so no value of it means unknown.

    Treating 0 as a sentinel would discard a real position in the Gulf of
    Guinea, which is a place an aircraft can be.
    """
    assert not is_sentinel(mavlink.MAVLink_global_position_int_message, "lat", 0)
    assert not is_sentinel(
        mavlink.MAVLink_global_position_int_message, "lat", UINT16_MAX
    )
