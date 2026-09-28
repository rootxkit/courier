"""Writing `drone_state` rows into the TimescaleDB hypertable.

P1-04's batching policy - flush on 100 rows or 500 ms, to sustain ten drones
at 4 Hz under a p99 insert latency of 50 ms - is `gateway/state_buffer.py`,
kept apart so the policy can be tuned without touching the SQL, and so this
module stays what the database-backed coverage gate measures.

**This is not on the acknowledgement path.** relay-v1 §7's ack promises the
*datagram* is durable, and that promise is kept by the archive and the ingest
index before a row is ever assembled. A failure here loses a derived view, not
the flight record: the archive still holds every byte and P10-03 can rebuild
`drone_state` from it. That asymmetry is why this raises and lets the caller
decide, rather than blocking ingest.

Geometry is built from the coordinates with `ST_SetSRID(ST_MakePoint(...))`
rather than by formatting WKT, so a row cannot carry a point whose SRID nobody
set. CLAUDE.md: all geometry SRID 4326, distance math on `geography`.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway.drone_state import DroneStateRow
from gateway.ingest_store import StoreError

_log = get_logger(__name__)

# ST_MakePoint takes longitude first. Writing it the other way round produces a
# point that is valid, plots somewhere real, and is wrong - Tbilisi at
# 41.7 N 44.8 E becomes 44.8 N 41.7 E, which is in Georgia's neighbour. The
# column order here is the only place that can go wrong, so it is stated once.
_INSERT = sa.text(
    """
    INSERT INTO drone_state (
        drone_id, ts, station_id, geom,
        alt_amsl_m, alt_above_home_m, heading_deg,
        vx_ms, vy_ms, vz_ms,
        batt_pct, batt_voltage_v, batt_consumed_wh,
        mode, armed, gps_fix_type, sat_count,
        groundspeed_ms, climb_ms
    ) VALUES (
        :drone_id, :ts, :station_id,
        ST_SetSRID(ST_MakePoint(:lon_deg, :lat_deg), 4326),
        :alt_amsl_m, :alt_above_home_m, :heading_deg,
        :vx_ms, :vy_ms, :vz_ms,
        :batt_pct, :batt_voltage_v, :batt_consumed_wh,
        :mode, :armed, :gps_fix_type, :sat_count,
        :groundspeed_ms, :climb_ms
    )
    ON CONFLICT (drone_id, ts, station_id) DO NOTHING
    """
)


@dataclass
class DroneStateWriter:
    """Inserts assembled rows. Batching policy belongs to the caller."""

    engine: AsyncEngine

    async def write(self, rows: list[DroneStateRow]) -> int:
        """Insert a batch, returning how many rows it contained.

        Duplicates are ignored rather than failing the batch. A relay
        retransmitting after a lost ack replays records the Gateway already
        converted, and the unique key is `(drone_id, ts, station_id)` - so a
        replay is a no-op while two *stations* observing one vehicle at the
        same instant still produce two rows, which spec §8 requires: they are
        independent observations over different links, not duplicates.
        """
        if not rows:
            return 0

        try:
            async with self.engine.begin() as connection:
                await connection.execute(_INSERT, [_parameters(row) for row in rows])
        except SQLAlchemyError as error:
            raise StoreError(f"could not write drone_state: {error}") from error
        return len(rows)


def _parameters(row: DroneStateRow) -> dict[str, object]:
    return {
        "drone_id": str(row.drone_id),
        "ts": row.ts,
        "station_id": row.station_id,
        "lat_deg": row.lat_deg,
        "lon_deg": row.lon_deg,
        "alt_amsl_m": row.alt_amsl_m,
        "alt_above_home_m": row.alt_above_home_m,
        "heading_deg": row.heading_deg,
        "vx_ms": row.vx_ms,
        "vy_ms": row.vy_ms,
        "vz_ms": row.vz_ms,
        "batt_pct": row.batt_pct,
        "batt_voltage_v": row.batt_voltage_v,
        "batt_consumed_wh": row.batt_consumed_wh,
        "mode": row.mode,
        "armed": row.armed,
        "gps_fix_type": row.gps_fix_type,
        "sat_count": row.sat_count,
        "groundspeed_ms": row.groundspeed_ms,
        "climb_ms": row.climb_ms,
    }
