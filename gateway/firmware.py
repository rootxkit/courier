"""Flight software per airframe, from AUTOPILOT_VERSION. P1-11.

The Gateway records the version it observes and never asks for it. Stage 0 is
receive-only. QGC requests the version on every connect, so the reply passes
through a relay that was attached before QGC connected.

## Decoding

`flight_sw_version` is four bytes. MAVLink's `common.xml` defines them,
most significant first, as major, minor, patch and `FIRMWARE_VERSION_TYPE`.
The type names come from pymavlink's own enum, not from literals.
`flight_custom_version` is `uint8[8]`, which ArduPilot fills with the first
eight characters of the git hash, in ASCII.

Recording is `gateway/firmware_store.py`, kept apart because it needs the
database and its coverage is measured by the database job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as mavlink

MESSAGE_NAME = "AUTOPILOT_VERSION"

# FIRMWARE_VERSION_TYPE value -> suffix, from pymavlink's enum.
_TYPE_SUFFIX: dict[int, str] = {
    mavlink.FIRMWARE_VERSION_TYPE_DEV: "-dev",
    mavlink.FIRMWARE_VERSION_TYPE_ALPHA: "-alpha",
    mavlink.FIRMWARE_VERSION_TYPE_BETA: "-beta",
    mavlink.FIRMWARE_VERSION_TYPE_RC: "-rc",
    mavlink.FIRMWARE_VERSION_TYPE_OFFICIAL: "",
}


@dataclass(frozen=True, slots=True)
class Firmware:
    """What one AUTOPILOT_VERSION said."""

    flight_sw_version: int
    git_hash: str | None
    os_sw_version: int
    board_version: int
    vendor_id: int
    product_id: int
    uid: int

    @property
    def version(self) -> str:
        return version_text(self.flight_sw_version)

    @property
    def identity(self) -> tuple[int, str | None, int]:
        """What must change for this to count as a different firmware."""
        return (self.flight_sw_version, self.git_hash, self.board_version)

    def summary(self) -> dict[str, str | None]:
        """For the console: what a pilot or technician reads."""
        return {"version": self.version, "git_hash": self.git_hash}


def version_text(flight_sw_version: int) -> str:
    """`4.8.0-dev` from the packed word. An unknown type byte stays visible."""
    major = (flight_sw_version >> 24) & 0xFF
    minor = (flight_sw_version >> 16) & 0xFF
    patch = (flight_sw_version >> 8) & 0xFF
    kind = flight_sw_version & 0xFF
    suffix = _TYPE_SUFFIX.get(kind, f"-type{kind}")
    return f"{major}.{minor}.{patch}{suffix}"


def git_hash_text(custom: Any) -> str | None:
    """The 8 custom-version bytes as text, or None when they carry nothing.

    ASCII when every byte is printable, which is how ArduPilot sends a git
    hash; hexadecimal otherwise, so an autopilot that packs raw bytes is
    still recorded rather than garbled.
    """
    raw = bytes(int(byte) & 0xFF for byte in custom).rstrip(b"\x00")
    if not raw:
        return None
    if all(0x20 <= byte < 0x7F for byte in raw):
        return raw.decode("ascii")
    return raw.hex()


def firmware_from_message(payload: Any) -> Firmware:
    return Firmware(
        flight_sw_version=int(payload.flight_sw_version),
        git_hash=git_hash_text(payload.flight_custom_version),
        os_sw_version=int(payload.os_sw_version),
        board_version=int(payload.board_version),
        vendor_id=int(payload.vendor_id),
        product_id=int(payload.product_id),
        uid=int(payload.uid),
    )
