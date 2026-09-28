"""Writing `drone_state` rows into the TimescaleDB hypertable.

P1-04's batching policy - flush on 100 rows or 500 ms, to sustain ten drones
at 4 Hz under a p99 insert latency of 50 ms - is `BufferedStateWriter`, kept
apart from `DroneStateWriter` so the policy can be tuned without touching the
SQL.

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

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway.drone_state import DroneStateRow
from gateway.ingest_store import StoreError
from gateway.stage_timing import StageTimings, shared_timings

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


DEFAULT_FLUSH_ROWS = 100
DEFAULT_FLUSH_INTERVAL_S = 0.5


class RowWriter(Protocol):
    async def write(self, rows: list[DroneStateRow]) -> int: ...


@dataclass
class BufferedStateWriter:
    """P1-04's batching policy: flush on `flush_rows` rows or `flush_interval_s`.

    Whichever comes first. At ten drones at 4 Hz the interval governs - forty
    rows a second never reaches a hundred in half a second - so each insert
    carries about twenty rows instead of the handful in one relay batch, and
    the database sees two inserts a second rather than one per batch. The row
    count is the bound for bursts: a replayed backlog flushes every hundred
    rows instead of waiting on the clock.

    Delay costs nothing a pilot sees. The console is fed from the bus, which
    is published before this buffer; only the hypertable is up to
    `flush_interval_s` behind.

    A failed flush is logged with its row count and those rows are dropped,
    not retried: `drone_state` is derived, the archive holds every byte, and
    P10-03 rebuilds it. Retrying would hold rows in memory for as long as the
    database is down, which is the one moment memory should not grow.
    """

    inner: RowWriter
    flush_rows: int = DEFAULT_FLUSH_ROWS
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S
    timings: StageTimings = field(default_factory=shared_timings)

    _buffer: list[DroneStateRow] = field(default_factory=list, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _timer: asyncio.Task[None] | None = field(default=None, init=False)

    async def write(self, rows: list[DroneStateRow]) -> int:
        """Buffer rows; flush now if the row limit is reached. Never raises."""
        if not rows:
            return 0
        self._buffer.extend(rows)
        if len(self._buffer) >= self.flush_rows:
            await self.flush()
        elif self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_after_interval())
        return len(rows)

    async def flush(self) -> int:
        """Insert everything buffered, in one statement. Returns rows written."""
        async with self._lock:
            if not self._buffer:
                return 0
            batch, self._buffer = self._buffer, []
            try:
                with self.timings.measure("state.insert"):
                    written = await self.inner.write(batch)
            except Exception as error:
                _log.error(
                    "could not flush drone_state",
                    extra={"rows": len(batch), "error": repr(error)},
                )
                return 0
            self.timings.count("state_rows", written)
            return written

    async def close(self) -> None:
        """Flush what is left and stop the timer. Called on shutdown."""
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer
        await self.flush()

    async def _flush_after_interval(self) -> None:
        await asyncio.sleep(self.flush_interval_s)
        await self.flush()
