"""Recording flight software per airframe in `drone_firmware`. P1-11.

A row is written only when a drone's reported version changes, so the table
is a history. The latest version per drone is cached. After a restart the
cache is reloaded from the table the first time a drone is seen, so a
restarted Gateway does not show a known airframe as unknown.

Decoding is `gateway/firmware.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.firmware import Firmware
from gateway.ingest_store import StoreError

_INSERT = sa.text(
    """
    INSERT INTO drone_firmware (
        drone_id, observed_at, station_id, flight_sw_version, version,
        git_hash, os_sw_version, board_version, vendor_id, product_id, uid
    ) VALUES (
        :drone_id, :observed_at, :station_id, :flight_sw_version, :version,
        :git_hash, :os_sw_version, :board_version, :vendor_id, :product_id, :uid
    )
    ON CONFLICT (drone_id, observed_at) DO NOTHING
    """
)

_LATEST = sa.text(
    """
    SELECT flight_sw_version, git_hash, os_sw_version, board_version,
           vendor_id, product_id, uid
    FROM drone_firmware
    WHERE drone_id = :drone_id
    ORDER BY observed_at DESC
    LIMIT 1
    """
)


@dataclass
class FirmwareRegistry:
    """Records changes and answers "what does this drone run", from a cache."""

    engine: AsyncEngine

    _known: dict[UUID, Firmware | None] = field(default_factory=dict, init=False)

    async def latest(self, drone_id: UUID) -> Firmware | None:
        """The drone's last recorded firmware, or None if it never reported one."""
        if drone_id not in self._known:
            self._known[drone_id] = await self._read_latest(drone_id)
        return self._known[drone_id]

    async def observe(
        self, drone_id: UUID, station_id: str, observed_at: datetime, firmware: Firmware
    ) -> bool:
        """Record `firmware` if it differs from the latest. Returns whether it did."""
        current = await self.latest(drone_id)
        if current is not None and current.identity == firmware.identity:
            return False
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    _INSERT,
                    {
                        "drone_id": str(drone_id),
                        "observed_at": observed_at,
                        "station_id": station_id,
                        "flight_sw_version": firmware.flight_sw_version,
                        "version": firmware.version,
                        "git_hash": firmware.git_hash,
                        "os_sw_version": firmware.os_sw_version,
                        "board_version": firmware.board_version,
                        "vendor_id": firmware.vendor_id,
                        "product_id": firmware.product_id,
                        "uid": str(firmware.uid),
                    },
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not record firmware: {error}") from error
        self._known[drone_id] = firmware
        return True

    async def _read_latest(self, drone_id: UUID) -> Firmware | None:
        try:
            async with self.engine.connect() as connection:
                row = (
                    await connection.execute(_LATEST, {"drone_id": str(drone_id)})
                ).first()
        except SQLAlchemyError as error:
            raise StoreError(f"could not read firmware: {error}") from error
        if row is None:
            return None
        return Firmware(
            flight_sw_version=int(row.flight_sw_version),
            git_hash=row.git_hash,
            os_sw_version=int(row.os_sw_version or 0),
            board_version=int(row.board_version or 0),
            vendor_id=int(row.vendor_id or 0),
            product_id=int(row.product_id or 0),
            uid=int(row.uid or 0),
        )
