"""The decoder agrees with the Open Drone ID reference library. P1-15.

`data/odid_vectors.json` holds messages the reference library
(opendroneid-core-c) encoded from pseudo-random values, with what its own
decoder read back. Every field must match, so an offset or a scale that is
wrong in a plausible way fails here instead of placing an aircraft somewhere
plausible and wrong. `tools/odid_vectors/gen_vectors.c` regenerates the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gateway import odid

VECTORS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "data" / "odid_vectors.json").read_text(encoding="utf-8")
)


def of_type(kind: str) -> list[dict[str, Any]]:
    return [v for v in VECTORS if v["type"] == kind]


def unknown_alt(value: float) -> float | None:
    return None if value == -1000 else value


def close(actual: float | None, expected: float | None, tolerance: float) -> bool:
    if expected is None or actual is None:
        return actual is expected
    return abs(actual - expected) <= tolerance


def test_the_vector_file_covers_every_message_kind() -> None:
    kinds = {v["type"] for v in VECTORS}

    assert kinds == {"basic_id", "location", "system", "operator_id", "pack"}
    # Unknown values are in the set, so the None mapping is exercised both ways.
    assert any(v["direction"] == 361 for v in of_type("location"))
    assert any(v["direction"] != 361 for v in of_type("location"))


@pytest.mark.parametrize("vector", of_type("basic_id"), ids=lambda v: v["hex"][:12])
def test_basic_id(vector: dict[str, Any]) -> None:
    decoded = odid.decode_basic_id(bytes.fromhex(vector["hex"]))

    assert (decoded.id_type, decoded.ua_type, decoded.ua_id) == (
        vector["id_type"],
        vector["ua_type"],
        vector["ua_id"],
    )


@pytest.mark.parametrize("vector", of_type("location"), ids=lambda v: v["hex"][:12])
def test_location(vector: dict[str, Any]) -> None:
    got = odid.decode_location(bytes.fromhex(vector["hex"]))

    assert got.status == vector["status"]
    assert close(
        got.direction_deg,
        None if vector["direction"] == 361 else vector["direction"],
        1e-6,
    )
    assert close(
        got.speed_horizontal_ms,
        None if vector["speed_horizontal"] == 255 else vector["speed_horizontal"],
        1e-6,
    )
    assert close(
        got.speed_vertical_ms,
        None if vector["speed_vertical"] == 63 else vector["speed_vertical"],
        1e-6,
    )
    # The library prints ten decimals; the encoding is 1e-7 degrees.
    assert close(got.lat_deg, vector["latitude"], 1e-9)
    assert close(got.lon_deg, vector["longitude"], 1e-9)
    assert close(got.alt_baro_m, unknown_alt(vector["altitude_baro"]), 1e-6)
    assert close(got.alt_hae_m, unknown_alt(vector["altitude_geo"]), 1e-6)
    assert close(got.height_m, unknown_alt(vector["height"]), 1e-6)
    assert got.height_reference == vector["height_type"]
    assert (
        got.horiz_accuracy,
        got.vert_accuracy,
        got.baro_accuracy,
        got.speed_accuracy,
        got.ts_accuracy,
    ) == (
        vector["horiz_accuracy"],
        vector["vert_accuracy"],
        vector["baro_accuracy"],
        vector["speed_accuracy"],
        vector["ts_accuracy"],
    )
    expected_ts = None if vector["timestamp"] == 65535 else vector["timestamp"]
    # The library holds seconds in a float32 (3340.1 prints as 3340.1001); the
    # encoding's resolution is 0.1 s.
    assert close(got.seconds_after_hour, expected_ts, 1e-3)


@pytest.mark.parametrize("vector", of_type("system"), ids=lambda v: v["hex"][:12])
def test_system(vector: dict[str, Any]) -> None:
    got = odid.decode_system(bytes.fromhex(vector["hex"]))

    assert got.operator_location_type == vector["operator_location_type"]
    assert got.classification_type == vector["classification_type"]
    assert close(got.operator_lat_deg, vector["operator_latitude"], 1e-9)
    assert close(got.operator_lon_deg, vector["operator_longitude"], 1e-9)
    assert got.area_count == vector["area_count"]
    assert got.area_radius_m == vector["area_radius"]
    assert close(got.area_ceiling_m, unknown_alt(vector["area_ceiling"]), 1e-6)
    assert close(got.area_floor_m, unknown_alt(vector["area_floor"]), 1e-6)
    assert (got.category_eu, got.class_eu) == (
        vector["category_eu"],
        vector["class_eu"],
    )
    assert close(
        got.operator_alt_hae_m, unknown_alt(vector["operator_altitude_geo"]), 1e-6
    )
    assert got.timestamp_s == vector["timestamp"]


@pytest.mark.parametrize("vector", of_type("operator_id"), ids=lambda v: v["hex"][:12])
def test_operator_id(vector: dict[str, Any]) -> None:
    got = odid.decode_operator_id(bytes.fromhex(vector["hex"]))

    assert (got.operator_id_type, got.operator_id) == (
        vector["operator_id_type"],
        vector["operator_id"],
    )


@pytest.mark.parametrize("vector", of_type("pack"), ids=lambda v: v["hex"][:12])
def test_a_pack_yields_what_the_library_takes_from_it(vector: dict[str, Any]) -> None:
    assert vector["ok"] == 1
    messages = odid.decode(bytes.fromhex(vector["hex"]))

    basic = [m for m in messages if isinstance(m, odid.BasicId)]
    location = [m for m in messages if isinstance(m, odid.Location)]
    system = [m for m in messages if isinstance(m, odid.System)]
    assert [b.ua_id for b in basic] == [vector["ua_id"]]
    assert close(location[0].lat_deg, vector["latitude"], 1e-9)
    assert close(location[0].lon_deg, vector["longitude"], 1e-9)
    assert close(location[0].alt_hae_m, vector["altitude_geo"], 1e-6)
    assert close(system[0].operator_lat_deg, vector["operator_latitude"], 1e-9)


# --- refusals, each paired with the same bytes accepted -------------------------


def a_pack() -> bytes:
    return bytes.fromhex(of_type("pack")[0]["hex"])


def test_a_pack_with_two_locations_is_refused_whole() -> None:
    pack = bytearray(a_pack())
    location = pack[3 + odid.MESSAGE_SIZE : 3 + 2 * odid.MESSAGE_SIZE]
    doubled = bytes(pack[:2]) + bytes([4]) + bytes(pack[3:]) + bytes(location)

    assert len(odid.decode(a_pack())) == 3
    with pytest.raises(odid.DecodeError, match="LOCATION"):
        odid.decode(doubled)


def test_a_pack_shorter_than_it_says_is_refused() -> None:
    pack = a_pack()

    assert odid.unpack(pack)
    with pytest.raises(odid.DecodeError, match="shorter"):
        odid.unpack(pack[:-1])


def test_a_truncated_message_is_refused() -> None:
    location = bytes.fromhex(of_type("location")[0]["hex"])

    assert odid.decode(location)
    with pytest.raises(odid.DecodeError, match="bytes"):
        odid.decode(location[:24])


def test_the_wrong_decoder_refuses_rather_than_misreads() -> None:
    location = bytes.fromhex(of_type("location")[0]["hex"])

    assert odid.decode_location(location)
    with pytest.raises(odid.DecodeError, match="BASIC_ID"):
        odid.decode_basic_id(location)


def test_self_id_and_auth_are_skipped_not_misread() -> None:
    self_id = bytes([0x32]) + bytes(24)
    auth = bytes([0x22]) + bytes(24)
    unknown = bytes([0x92]) + bytes(24)

    assert odid.decode(self_id) == []
    assert odid.decode(auth) == []
    assert odid.decode(unknown) == []


# --- encoding, for the simulator: the reference library's bytes back ------------


@pytest.mark.parametrize("vector", of_type("basic_id"), ids=lambda v: v["hex"][:12])
def test_basic_id_re_encodes_to_the_reference_bytes(vector: dict[str, Any]) -> None:
    raw = bytes.fromhex(vector["hex"])

    assert odid.encode_basic_id(odid.decode_basic_id(raw)) == raw


@pytest.mark.parametrize("vector", of_type("location"), ids=lambda v: v["hex"][:12])
def test_location_re_encodes_to_the_reference_bytes(vector: dict[str, Any]) -> None:
    raw = bytes.fromhex(vector["hex"])

    assert odid.encode_location(odid.decode_location(raw)).hex() == raw.hex()


@pytest.mark.parametrize("vector", of_type("operator_id"), ids=lambda v: v["hex"][:12])
def test_operator_id_re_encodes_to_the_reference_bytes(vector: dict[str, Any]) -> None:
    raw = bytes.fromhex(vector["hex"])

    assert odid.encode_operator_id(odid.decode_operator_id(raw)) == raw


@pytest.mark.parametrize("vector", of_type("pack"), ids=lambda v: v["hex"][:12])
def test_a_pack_re_encodes_to_the_reference_bytes(vector: dict[str, Any]) -> None:
    raw = bytes.fromhex(vector["hex"])

    assert odid.encode_pack(odid.unpack(raw)) == raw


# --- the rest of the refusals, each against bytes that are otherwise accepted ----


def pack_of(*messages: bytes) -> bytes:
    return bytes([0xF2, odid.MESSAGE_SIZE, len(messages)]) + b"".join(messages)


def one(kind: str) -> bytes:
    return bytes.fromhex(of_type(kind)[0]["hex"])


@pytest.mark.parametrize(
    ("raw", "complaint"),
    [
        (b"", "empty"),
        (bytes([0xF2, 24, 1]) + one("basic_id"), "message size 24"),
        (bytes([0xF2, odid.MESSAGE_SIZE, 0]), "pack of 0"),
        (bytes([0xF2, odid.MESSAGE_SIZE, 10]) + one("basic_id") * 10, "pack of 10"),
        (pack_of(one("basic_id"), bytes([0xF2]) + bytes(24)), "pack inside a pack"),
        (pack_of(one("basic_id"), one("basic_id"), one("basic_id")), "Basic ID"),
    ],
    ids=["empty", "size", "none", "too-many", "nested", "three-ids"],
)
def test_malformed_packs_are_refused(raw: bytes, complaint: str) -> None:
    assert odid.decode(pack_of(one("basic_id"), one("location")))
    with pytest.raises(odid.DecodeError, match=complaint):
        odid.decode(raw)


def test_unpack_refuses_a_single_message() -> None:
    with pytest.raises(odid.DecodeError, match="not a message pack"):
        odid.unpack(one("location"))


def test_a_short_message_after_a_valid_type_byte_is_refused() -> None:
    with pytest.raises(odid.DecodeError, match="bytes, a message is"):
        odid.decode(one("system")[:10])


def test_position_zero_zero_is_unknown_but_one_zero_is_not() -> None:
    base = odid.decode_location(one("location"))
    unknown = odid.encode_location(
        odid.Location(**{**_fields(base), "lat_deg": None, "lon_deg": None})
    )
    on_equator = odid.encode_location(
        odid.Location(**{**_fields(base), "lat_deg": 0.0, "lon_deg": 10.0})
    )

    assert odid.decode_location(unknown).lat_deg is None
    assert odid.decode_location(on_equator).lat_deg == 0.0


def test_a_direction_that_rounds_to_360_is_north() -> None:
    base = odid.decode_location(one("location"))
    raw = odid.encode_location(
        odid.Location(**{**_fields(base), "direction_deg": 359.6})
    )

    assert odid.decode_location(raw).direction_deg == 0.0


def test_encode_pack_refuses_what_decode_would() -> None:
    assert odid.encode_pack([one("basic_id")])
    with pytest.raises(ValueError, match="holds 1 to"):
        odid.encode_pack([])
    with pytest.raises(ValueError, match="bytes"):
        odid.encode_pack([one("basic_id")[:24]])


def _fields(location: odid.Location) -> dict[str, Any]:
    return {name: getattr(location, name) for name in odid.Location.__slots__}
