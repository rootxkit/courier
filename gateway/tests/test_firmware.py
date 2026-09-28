"""Flight software per airframe. P1-11.

Decoding is tested from frames pymavlink packs and parses, not from hand-built
payloads, so a field name or an encoding assumption that disagrees with the
dialect fails here rather than in the air.

The registry is tested against a real database, in both directions: a version
is recorded, the same version is not recorded again, and a changed version is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from pymavlink.dialects.v20 import ardupilotmega as mavlink
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.binding import BindingResolver
from gateway.firmware import (
    MESSAGE_NAME,
    Firmware,
    firmware_from_message,
    git_hash_text,
    version_text,
)
from gateway.firmware_store import FirmwareRegistry
from gateway.ingest_store import StoreError
from gateway.parsing import parse_datagram

NOON = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def packed(major: int, minor: int, patch: int, kind: int) -> int:
    return (major << 24) | (minor << 16) | (patch << 8) | kind


def autopilot_version_frame(
    *,
    flight_sw_version: int,
    custom: bytes = b"66c89850",
    board_version: int = 0,
    uid: int = 0,
) -> bytes:
    sender = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    sender.signing.sign_outgoing = False
    return bytes(
        sender.autopilot_version_encode(
            capabilities=0,
            flight_sw_version=flight_sw_version,
            middleware_sw_version=0,
            os_sw_version=0,
            board_version=board_version,
            flight_custom_version=list(custom.ljust(8, b"\x00")),
            middleware_custom_version=[0] * 8,
            os_custom_version=[0] * 8,
            vendor_id=0x1209,
            product_id=0x5740,
            uid=uid,
        ).pack(sender)
    )


def parsed(frame: bytes) -> Any:
    messages = list(parse_datagram(frame).messages)
    assert len(messages) == 1
    assert messages[0].name == MESSAGE_NAME
    return messages[0].payload


# --- decoding ----------------------------------------------------------------


def test_a_dev_build_reads_with_its_suffix() -> None:
    assert (
        version_text(packed(4, 8, 0, mavlink.FIRMWARE_VERSION_TYPE_DEV)) == "4.8.0-dev"
    )


def test_an_official_build_has_no_suffix() -> None:
    assert (
        version_text(packed(4, 5, 7, mavlink.FIRMWARE_VERSION_TYPE_OFFICIAL)) == "4.5.7"
    )


def test_every_named_type_is_distinguishable() -> None:
    kinds = [
        mavlink.FIRMWARE_VERSION_TYPE_DEV,
        mavlink.FIRMWARE_VERSION_TYPE_ALPHA,
        mavlink.FIRMWARE_VERSION_TYPE_BETA,
        mavlink.FIRMWARE_VERSION_TYPE_RC,
        mavlink.FIRMWARE_VERSION_TYPE_OFFICIAL,
    ]
    texts = {version_text(packed(4, 6, 0, kind)) for kind in kinds}
    assert len(texts) == len(kinds)


def test_an_unknown_type_byte_stays_visible() -> None:
    """Not rendered as official: an unrecognised build must not look released."""
    assert version_text(packed(4, 6, 0, 7)) == "4.6.0-type7"


def test_the_git_hash_is_read_as_ascii() -> None:
    assert git_hash_text(list(b"66c89850")) == "66c89850"


def test_empty_custom_bytes_mean_no_hash() -> None:
    assert git_hash_text([0] * 8) is None


def test_non_printable_custom_bytes_are_kept_as_hex() -> None:
    assert git_hash_text([0xDE, 0xAD, 0xBE, 0xEF, 0, 0, 0, 0]) == "deadbeef"


def test_a_packed_frame_decodes_to_the_values_it_was_built_from() -> None:
    """Through pymavlink's own pack and parse, so the field names and the
    uint8[8] representation are the dialect's, not an assumption."""
    frame = autopilot_version_frame(
        flight_sw_version=packed(4, 8, 0, mavlink.FIRMWARE_VERSION_TYPE_DEV),
        board_version=0x00320000,
        uid=2**64 - 1,
    )

    firmware = firmware_from_message(parsed(frame))

    assert firmware.version == "4.8.0-dev"
    assert firmware.git_hash == "66c89850"
    assert firmware.board_version == 0x00320000
    assert firmware.vendor_id == 0x1209
    assert firmware.product_id == 0x5740
    assert firmware.uid == 2**64 - 1


def test_the_summary_is_what_the_console_needs() -> None:
    firmware = firmware_from_message(
        parsed(
            autopilot_version_frame(
                flight_sw_version=packed(
                    4, 5, 7, mavlink.FIRMWARE_VERSION_TYPE_OFFICIAL
                )
            )
        )
    )
    assert firmware.summary() == {"version": "4.5.7", "git_hash": "66c89850"}


# --- the registry, against the database ---------------------------------------


def firmware(version: int, git_hash: str | None = "66c89850") -> Firmware:
    return Firmware(
        flight_sw_version=version,
        git_hash=git_hash,
        os_sw_version=0,
        board_version=0,
        vendor_id=0x1209,
        product_id=0x5740,
        uid=2**64 - 1,
    )


DEV_480 = packed(4, 8, 0, mavlink.FIRMWARE_VERSION_TYPE_DEV)
OFFICIAL_457 = packed(4, 5, 7, mavlink.FIRMWARE_VERSION_TYPE_OFFICIAL)


@pytest.fixture
async def drone(
    engine: AsyncEngine, request: pytest.FixtureRequest
) -> AsyncIterator[UUID]:
    """A known drone for this test, removed with its history afterwards."""
    drone_id = uuid4()
    label = f"fw-{abs(hash(request.node.nodeid)) % 10**12}"
    await BindingResolver(engine=engine).register_drone(drone_id, label)
    try:
        yield drone_id
    finally:
        async with engine.begin() as connection:
            # drone_firmware references known_drones with RESTRICT.
            await connection.execute(
                sa.text("DELETE FROM drone_firmware WHERE drone_id = :d"),
                {"d": str(drone_id)},
            )
            await connection.execute(
                sa.text("DELETE FROM known_drones WHERE drone_id = :d"),
                {"d": str(drone_id)},
            )


async def history(engine: AsyncEngine, drone_id: UUID) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT version, uid FROM drone_firmware "
                "WHERE drone_id = :d ORDER BY observed_at"
            ),
            {"d": str(drone_id)},
        )
        return [(row.version, row.uid) for row in rows]


@pytest.mark.postgres
async def test_a_drone_that_never_reported_has_no_firmware(
    engine: AsyncEngine, drone: UUID
) -> None:
    assert await FirmwareRegistry(engine=engine).latest(drone) is None


@pytest.mark.postgres
async def test_the_first_report_is_recorded(engine: AsyncEngine, drone: UUID) -> None:
    registry = FirmwareRegistry(engine=engine)

    recorded = await registry.observe(drone, "fw-station", NOON, firmware(DEV_480))

    assert recorded
    # uid as text: a uint64 does not fit a signed bigint.
    assert await history(engine, drone) == [("4.8.0-dev", str(2**64 - 1))]
    latest = await registry.latest(drone)
    assert latest is not None and latest.version == "4.8.0-dev"


@pytest.mark.postgres
async def test_the_same_version_is_not_recorded_twice(
    engine: AsyncEngine, drone: UUID
) -> None:
    """QGC asks on every connect. The table is a history of changes, not of
    connects."""
    registry = FirmwareRegistry(engine=engine)
    await registry.observe(drone, "fw-station", NOON, firmware(DEV_480))

    again = await registry.observe(
        drone, "fw-station", NOON + timedelta(minutes=5), firmware(DEV_480)
    )

    assert not again
    assert len(await history(engine, drone)) == 1


@pytest.mark.postgres
async def test_a_changed_version_is_recorded(engine: AsyncEngine, drone: UUID) -> None:
    registry = FirmwareRegistry(engine=engine)
    await registry.observe(drone, "fw-station", NOON, firmware(OFFICIAL_457))

    changed = await registry.observe(
        drone, "fw-station", NOON + timedelta(days=1), firmware(DEV_480)
    )

    assert changed
    assert [version for version, _ in await history(engine, drone)] == [
        "4.5.7",
        "4.8.0-dev",
    ]


@pytest.mark.postgres
async def test_a_rebuilt_binary_of_the_same_version_is_a_change(
    engine: AsyncEngine, drone: UUID
) -> None:
    registry = FirmwareRegistry(engine=engine)
    await registry.observe(drone, "fw-station", NOON, firmware(DEV_480, "66c89850"))

    changed = await registry.observe(
        drone, "fw-station", NOON + timedelta(hours=1), firmware(DEV_480, "0a1b2c3d")
    )

    assert changed
    assert len(await history(engine, drone)) == 2


@pytest.mark.postgres
async def test_a_restarted_gateway_knows_what_was_recorded(
    engine: AsyncEngine, drone: UUID
) -> None:
    """A new registry reloads from the table: a known airframe is not shown
    as unknown after a restart, and its unchanged version is not re-recorded."""
    await FirmwareRegistry(engine=engine).observe(
        drone, "fw-station", NOON, firmware(DEV_480)
    )

    restarted = FirmwareRegistry(engine=engine)
    latest = await restarted.latest(drone)
    again = await restarted.observe(
        drone, "fw-station", NOON + timedelta(minutes=1), firmware(DEV_480)
    )

    assert latest is not None
    assert latest.identity == firmware(DEV_480).identity
    assert not again
    assert len(await history(engine, drone)) == 1


@pytest.mark.postgres
async def test_a_version_for_an_unknown_airframe_is_refused(
    engine: AsyncEngine,
) -> None:
    with pytest.raises(StoreError, match="could not record firmware"):
        await FirmwareRegistry(engine=engine).observe(
            uuid4(), "fw-station", NOON, firmware(DEV_480)
        )
