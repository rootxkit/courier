"""P1-04's batching policy for `drone_state` inserts.

Kept out of `state_writer.py`, which owns the SQL and is measured by the
database-backed coverage gate. This owns only when to call it, needs no
database to test, and is covered by the unit run.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Protocol

from common import get_logger
from gateway.drone_state import DroneStateRow
from gateway.stage_timing import StageTimings, shared_timings

_log = get_logger(__name__)


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
