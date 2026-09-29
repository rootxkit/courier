"""Open Drone ID messages: the bytes a Remote ID broadcast carries. P1-15.

ASTM F3411 and ASD-STAN EN 4709-002 define the same 25-byte messages, sent
over Bluetooth 4 and 5 and Wi-Fi Beacon and NAN. Whatever receives them (an
ESP32, a phone, a commercial receiver) hands us these bytes; decoding them
here, once, is what keeps ingest independent of the receiver.

The layout is not written from memory of the standard (CLAUDE.md). It is
read from the packed structs of the reference library, opendroneid-core-c
(`libopendroneid/opendroneid.h`), and `tests/test_odid.py` decodes bytes that
library encoded and must agree with what the library decodes from them, for
every field. `tools/odid_vectors/` regenerates those vectors.

Multi-byte fields are little-endian. Bit fields are listed least significant
bit first, as in the reference structs.

Values the standard reserves for "unknown" become None here, at the parser
boundary, never deeper in the stack: an unknown altitude is not -1000 m and
an unknown position is not 0, 0.

A broadcast is not authenticated. Nothing decoded here is evidence that the
aircraft is who it says it is, or where it says it is.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

MESSAGE_SIZE = 25
ID_SIZE = 20
PACK_MAX_MESSAGES = 9
# opendroneid.h: ODID_BASIC_ID_MAX_MESSAGES. (Its limit of 16 authentication
# pages cannot be exceeded in a pack of at most 9 messages.)
BASIC_ID_MAX_MESSAGES = 2


class MessageType(IntEnum):
    BASIC_ID = 0
    LOCATION = 1
    AUTH = 2
    SELF_ID = 3
    SYSTEM = 4
    OPERATOR_ID = 5
    PACKED = 0xF


class IdType(IntEnum):
    NONE = 0
    SERIAL_NUMBER = 1
    CAA_REGISTRATION_ID = 2
    UTM_ASSIGNED_UUID = 3
    SPECIFIC_SESSION_ID = 4


class Status(IntEnum):
    UNDECLARED = 0
    GROUND = 1
    AIRBORNE = 2
    EMERGENCY = 3
    REMOTE_ID_SYSTEM_FAILURE = 4


class HeightReference(IntEnum):
    OVER_TAKEOFF = 0
    OVER_GROUND = 1


# Scaling, from opendroneid.c: SPEED_DIV, VSPEED_DIV, LATLON_MULT, ALT_DIV,
# ALT_ADDER. And the values it reserves for "unknown": INV_DIR, INV_SPEED_H,
# INV_SPEED_V, INV_ALT and INV_TIMESTAMP in opendroneid.h.
_SPEED_STEP_MS = (0.25, 0.75)
_VSPEED_STEP_MS = 0.5
_LATLON_SCALE = 10_000_000
_ALT_STEP_M = 0.5
_ALT_OFFSET_M = 1000.0
_UNKNOWN_DIRECTION_DEG = 361.0
_UNKNOWN_SPEED_H_MS = 255.0
_UNKNOWN_SPEED_V_MS = 63.0
_UNKNOWN_ALT_M = -1000.0
_UNKNOWN_TIMESTAMP = 0xFFFF
_AREA_RADIUS_STEP_M = 10


class DecodeError(ValueError):
    """Bytes that are not a message this decoder accepts."""


@dataclass(frozen=True, slots=True)
class BasicId:
    id_type: int
    ua_type: int
    ua_id: str


@dataclass(frozen=True, slots=True)
class Location:
    status: int
    # Track over the ground, degrees true. None: not reported.
    direction_deg: float | None
    speed_horizontal_ms: float | None
    # Up is positive, as in the standard (MAVLink's vz is down-positive).
    speed_vertical_ms: float | None
    lat_deg: float | None
    lon_deg: float | None
    # Pressure altitude, referenced to 1013.25 hPa: not AMSL.
    alt_baro_m: float | None
    # Height above the WGS-84 ellipsoid: not AMSL either.
    alt_hae_m: float | None
    height_reference: int
    height_m: float | None
    horiz_accuracy: int
    vert_accuracy: int
    baro_accuracy: int
    speed_accuracy: int
    ts_accuracy: int
    # Tenths of a second after the full UTC hour. None: not reported.
    seconds_after_hour: float | None


@dataclass(frozen=True, slots=True)
class System:
    operator_location_type: int
    classification_type: int
    operator_lat_deg: float | None
    operator_lon_deg: float | None
    area_count: int
    area_radius_m: int
    area_ceiling_m: float | None
    area_floor_m: float | None
    category_eu: int
    class_eu: int
    operator_alt_hae_m: float | None
    # Seconds since 2019-01-01 00:00:00 UTC.
    timestamp_s: int


@dataclass(frozen=True, slots=True)
class OperatorId:
    operator_id_type: int
    operator_id: str


Message = BasicId | Location | System | OperatorId


def message_type(first_byte: int) -> int:
    return first_byte >> 4


def _text(raw: bytes) -> str:
    # The reference library copies up to the first NUL (strncpy).
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _alt(encoded: int) -> float | None:
    value = encoded * _ALT_STEP_M - _ALT_OFFSET_M
    return None if value == _UNKNOWN_ALT_M else value


def _latlon(lat_enc: int, lon_enc: int) -> tuple[float | None, float | None]:
    # The standard's "unknown" is 0, 0 for both, not either alone.
    if lat_enc == 0 and lon_enc == 0:
        return None, None
    return lat_enc / _LATLON_SCALE, lon_enc / _LATLON_SCALE


def decode_basic_id(raw: bytes) -> BasicId:
    _check(raw, MessageType.BASIC_ID)
    return BasicId(
        id_type=raw[1] >> 4,
        ua_type=raw[1] & 0x0F,
        ua_id=_text(raw[2 : 2 + ID_SIZE]),
    )


def decode_location(raw: bytes) -> Location:
    _check(raw, MessageType.LOCATION)
    flags = raw[1]
    speed_mult = flags & 0x01
    east_west = (flags >> 1) & 0x01
    height_type = (flags >> 2) & 0x01
    status = flags >> 4
    (
        direction_enc,
        speed_enc,
        vspeed_enc,
        lat_enc,
        lon_enc,
        baro_enc,
        geo_enc,
        height_enc,
        accuracy_hv,
        accuracy_sb,
        timestamp_enc,
        accuracy_ts,
    ) = struct.unpack_from("<BBbiiHHHBBHB", raw, 2)

    direction = direction_enc + (180.0 if east_west else 0.0)
    if speed_mult:
        speed = speed_enc * _SPEED_STEP_MS[1] + 255 * _SPEED_STEP_MS[0]
    else:
        speed = speed_enc * _SPEED_STEP_MS[0]
    vspeed = vspeed_enc * _VSPEED_STEP_MS
    lat, lon = _latlon(lat_enc, lon_enc)
    return Location(
        status=status,
        direction_deg=None if direction == _UNKNOWN_DIRECTION_DEG else direction,
        speed_horizontal_ms=None if speed == _UNKNOWN_SPEED_H_MS else speed,
        speed_vertical_ms=None if vspeed == _UNKNOWN_SPEED_V_MS else vspeed,
        lat_deg=lat,
        lon_deg=lon,
        alt_baro_m=_alt(baro_enc),
        alt_hae_m=_alt(geo_enc),
        height_reference=height_type,
        height_m=_alt(height_enc),
        horiz_accuracy=accuracy_hv & 0x0F,
        vert_accuracy=accuracy_hv >> 4,
        speed_accuracy=accuracy_sb & 0x0F,
        baro_accuracy=accuracy_sb >> 4,
        ts_accuracy=accuracy_ts & 0x0F,
        seconds_after_hour=(
            None if timestamp_enc == _UNKNOWN_TIMESTAMP else timestamp_enc / 10
        ),
    )


def decode_system(raw: bytes) -> System:
    _check(raw, MessageType.SYSTEM)
    flags = raw[1]
    (
        lat_enc,
        lon_enc,
        area_count,
        area_radius_enc,
        ceiling_enc,
        floor_enc,
        eu,
        operator_alt_enc,
        timestamp,
    ) = struct.unpack_from("<iiHBHHBHI", raw, 2)
    lat, lon = _latlon(lat_enc, lon_enc)
    return System(
        operator_location_type=flags & 0x03,
        classification_type=(flags >> 2) & 0x07,
        operator_lat_deg=lat,
        operator_lon_deg=lon,
        area_count=area_count,
        area_radius_m=area_radius_enc * _AREA_RADIUS_STEP_M,
        area_ceiling_m=_alt(ceiling_enc),
        area_floor_m=_alt(floor_enc),
        class_eu=eu & 0x0F,
        category_eu=eu >> 4,
        operator_alt_hae_m=_alt(operator_alt_enc),
        timestamp_s=timestamp,
    )


def decode_operator_id(raw: bytes) -> OperatorId:
    _check(raw, MessageType.OPERATOR_ID)
    return OperatorId(operator_id_type=raw[1], operator_id=_text(raw[2 : 2 + ID_SIZE]))


_DECODERS: dict[int, Callable[[bytes], Message]] = {
    MessageType.BASIC_ID: decode_basic_id,
    MessageType.LOCATION: decode_location,
    MessageType.SYSTEM: decode_system,
    MessageType.OPERATOR_ID: decode_operator_id,
}


def decode(raw: bytes) -> list[Message]:
    """One message or a message pack, as the messages it holds.

    Authentication and Self-ID messages are skipped: authentication is not
    verified here (it would take the manufacturer's keys), and Self-ID is free
    text an operator types. A pack that breaks the standard's rules is refused
    whole, as the reference library refuses it.
    """
    if not raw:
        raise DecodeError("empty")
    kind = message_type(raw[0])
    if kind == MessageType.PACKED:
        return [m for m in (_decode_one(r) for r in unpack(raw)) if m is not None]
    single = _decode_one(raw)
    return [] if single is None else [single]


def unpack(raw: bytes) -> list[bytes]:
    """The messages inside a message pack."""
    if len(raw) < 3 or message_type(raw[0]) != MessageType.PACKED:
        raise DecodeError("not a message pack")
    size, count = raw[1], raw[2]
    if size != MESSAGE_SIZE:
        raise DecodeError(f"pack message size {size}, expected {MESSAGE_SIZE}")
    if not 1 <= count <= PACK_MAX_MESSAGES:
        raise DecodeError(f"pack of {count} messages")
    if len(raw) < 3 + count * MESSAGE_SIZE:
        raise DecodeError("pack shorter than it says")
    messages = [
        raw[3 + i * MESSAGE_SIZE : 3 + (i + 1) * MESSAGE_SIZE] for i in range(count)
    ]
    kinds = [message_type(m[0]) for m in messages]
    if any(k > MessageType.OPERATOR_ID for k in kinds):
        raise DecodeError("a pack inside a pack")
    if kinds.count(MessageType.BASIC_ID) > BASIC_ID_MAX_MESSAGES:
        raise DecodeError("too many Basic ID messages in a pack")
    for single in (
        MessageType.LOCATION,
        MessageType.SELF_ID,
        MessageType.SYSTEM,
        MessageType.OPERATOR_ID,
    ):
        if kinds.count(single) > 1:
            raise DecodeError(f"more than one {single.name} message in a pack")
    return messages


def _decode_one(raw: bytes) -> Message | None:
    if len(raw) < MESSAGE_SIZE:
        raise DecodeError(f"{len(raw)} bytes, a message is {MESSAGE_SIZE}")
    decoder = _DECODERS.get(message_type(raw[0]))
    return None if decoder is None else decoder(raw[:MESSAGE_SIZE])


def _check(raw: bytes, expected: MessageType) -> None:
    if len(raw) < MESSAGE_SIZE:
        raise DecodeError(f"{len(raw)} bytes, a message is {MESSAGE_SIZE}")
    if message_type(raw[0]) != expected:
        raise DecodeError(f"not a {expected.name} message")


# --- encoding -------------------------------------------------------------------
#
# For the Remote ID simulator (tools/remote_id_sim.py) and tests: a receiver
# never encodes. Pinned by re-encoding what the reference library encoded and
# getting its bytes back (tests/test_odid.py).

PROTOCOL_VERSION = 2


def _round(value: float) -> int:
    # C's round(): halves away from zero, not Python's round-half-to-even.
    return math.floor(value + 0.5) if value >= 0 else -math.floor(-value + 0.5)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _header(kind: MessageType) -> int:
    return (int(kind) << 4) | PROTOCOL_VERSION


def _enc_alt(alt_m: float | None) -> int:
    value = _UNKNOWN_ALT_M if alt_m is None else alt_m
    return _clamp(_round((value + _ALT_OFFSET_M) / _ALT_STEP_M), 0, 0xFFFF)


def _enc_latlon(value: float | None) -> int:
    return (
        0
        if value is None
        else _clamp(
            _round(value * _LATLON_SCALE), -180 * _LATLON_SCALE, 180 * _LATLON_SCALE
        )
    )


def _enc_text(text: str) -> bytes:
    return text.encode("ascii")[:ID_SIZE].ljust(ID_SIZE, b"\x00")


def encode_basic_id(message: BasicId) -> bytes:
    return (
        bytes([_header(MessageType.BASIC_ID), (message.id_type << 4) | message.ua_type])
        + _enc_text(message.ua_id)
        + bytes(3)
    )


def encode_location(message: Location) -> bytes:
    direction = (
        _UNKNOWN_DIRECTION_DEG
        if message.direction_deg is None
        else message.direction_deg
    )
    direction_int = _round(direction)
    if direction_int == 360:
        direction_int = 0
    east_west = 1 if direction_int >= 180 else 0
    direction_enc = _clamp(direction_int - 180 * east_west, 0, 0xFF)

    speed = (
        _UNKNOWN_SPEED_H_MS
        if message.speed_horizontal_ms is None
        else message.speed_horizontal_ms
    )
    if speed <= 255 * _SPEED_STEP_MS[0]:
        speed_mult, speed_enc = 0, _round(speed / _SPEED_STEP_MS[0])
    else:
        speed_mult = 1
        speed_enc = _clamp(
            _round((speed - 255 * _SPEED_STEP_MS[0]) / _SPEED_STEP_MS[1]), 0, 0xFF
        )
    vspeed = (
        _UNKNOWN_SPEED_V_MS
        if message.speed_vertical_ms is None
        else message.speed_vertical_ms
    )
    timestamp = (
        _UNKNOWN_TIMESTAMP
        if message.seconds_after_hour is None
        else _clamp(_round(message.seconds_after_hour * 10), 0, 3600 * 10)
    )
    flags = (
        speed_mult
        | (east_west << 1)
        | (message.height_reference << 2)
        | (message.status << 4)
    )
    return bytes([_header(MessageType.LOCATION), flags]) + struct.pack(
        "<BBbiiHHHBBHBB",
        direction_enc,
        speed_enc,
        _clamp(_round(vspeed / _VSPEED_STEP_MS), -128, 127),
        _enc_latlon(message.lat_deg),
        _enc_latlon(message.lon_deg),
        _enc_alt(message.alt_baro_m),
        _enc_alt(message.alt_hae_m),
        _enc_alt(message.height_m),
        message.horiz_accuracy | (message.vert_accuracy << 4),
        message.speed_accuracy | (message.baro_accuracy << 4),
        timestamp,
        message.ts_accuracy,
        0,
    )


def encode_operator_id(message: OperatorId) -> bytes:
    return (
        bytes([_header(MessageType.OPERATOR_ID), message.operator_id_type])
        + _enc_text(message.operator_id)
        + bytes(3)
    )


def encode_pack(messages: list[bytes]) -> bytes:
    if not 1 <= len(messages) <= PACK_MAX_MESSAGES:
        raise ValueError(f"a pack holds 1 to {PACK_MAX_MESSAGES} messages")
    if any(len(m) != MESSAGE_SIZE for m in messages):
        raise ValueError(f"every message in a pack is {MESSAGE_SIZE} bytes")
    return bytes([_header(MessageType.PACKED), MESSAGE_SIZE, len(messages)]) + b"".join(
        messages
    )
