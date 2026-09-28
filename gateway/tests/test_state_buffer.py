"""P1-04's batching policy: 100 rows or 500 ms, whichever comes first.

Each trigger is tested on its own and against the other: a burst flushes on
the row count without waiting for the clock, and a trickle flushes on the clock
without waiting for a hundred rows. A policy that only ever used one of them
passes half of these and fails the other half.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from gateway.drone_state import DroneStateRow
from gateway.stage_timing import StageTimings
from gateway.state_writer import BufferedStateWriter

NOON = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
DRONE = uuid4()


class RecordingWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.inserts: list[list[DroneStateRow]] = []
        self.fail = fail

    async def write(self, rows: list[DroneStateRow]) -> int:
        if self.fail:
            raise ConnectionError("the database is gone")
        self.inserts.append(list(rows))
        return len(rows)


def rows(count: int, start: int = 0) -> list[DroneStateRow]:
    return [
        DroneStateRow(
            drone_id=DRONE,
            ts=NOON + timedelta(milliseconds=250 * (start + n)),
            station_id="buffer-test",
        )
        for n in range(count)
    ]


def buffered(
    inner: RecordingWriter, *, flush_rows: int = 100, flush_interval_s: float = 0.2
) -> BufferedStateWriter:
    return BufferedStateWriter(
        inner=inner,
        flush_rows=flush_rows,
        flush_interval_s=flush_interval_s,
        timings=StageTimings(),
    )


async def test_a_burst_flushes_on_the_row_count_without_waiting() -> None:
    inner = RecordingWriter()
    writer = buffered(inner, flush_interval_s=60.0)

    await writer.write(rows(60))
    assert inner.inserts == []
    await writer.write(rows(40, start=60))

    assert [len(batch) for batch in inner.inserts] == [100]


async def test_a_trickle_flushes_on_the_interval() -> None:
    """Ten drones at 4 Hz: forty rows a second never reaches a hundred."""
    inner = RecordingWriter()
    writer = buffered(inner, flush_interval_s=0.1)

    await writer.write(rows(5))
    await writer.write(rows(5, start=5))
    assert inner.inserts == [], "flushed before the interval"

    await asyncio.sleep(0.25)

    assert [len(batch) for batch in inner.inserts] == [10]


async def test_rows_are_inserted_once_and_in_order() -> None:
    inner = RecordingWriter()
    writer = buffered(inner, flush_rows=7, flush_interval_s=0.05)

    for start in range(0, 30, 3):
        await writer.write(rows(3, start=start))
    await asyncio.sleep(0.15)

    inserted = [row.ts for batch in inner.inserts for row in batch]
    assert inserted == [row.ts for row in rows(30)]


async def test_close_flushes_what_is_left() -> None:
    """Shutdown must not drop the last half second of rows."""
    inner = RecordingWriter()
    writer = buffered(inner, flush_interval_s=60.0)
    await writer.write(rows(12))

    await writer.close()

    assert [len(batch) for batch in inner.inserts] == [12]


async def test_a_failed_flush_is_logged_and_does_not_raise() -> None:
    inner = RecordingWriter(fail=True)
    writer = buffered(inner, flush_rows=10)

    assert await writer.write(rows(10)) == 10
    assert await writer.flush() == 0


async def test_the_next_flush_after_a_failure_writes_normally() -> None:
    """The presence half: a failure drops that batch, not the writer."""
    inner = RecordingWriter(fail=True)
    writer = buffered(inner, flush_rows=10, flush_interval_s=60.0)
    await writer.write(rows(10))

    inner.fail = False
    await writer.write(rows(10, start=10))

    assert [len(batch) for batch in inner.inserts] == [10]
    assert inner.inserts[0][0].ts == rows(1, start=10)[0].ts


async def test_each_insert_is_timed_for_the_p99() -> None:
    timings = StageTimings()
    inner = RecordingWriter()
    writer = BufferedStateWriter(inner=inner, flush_rows=5, timings=timings)

    await writer.write(rows(5))
    await writer.write(rows(5, start=5))

    assert len(timings.samples("state.insert")) == 2
    assert timings.snapshot()["state_rows"] == 10


@pytest.mark.parametrize(
    ("values", "rank", "expected"),
    [
        ([1.0], 99, 1.0),
        ([float(n) for n in range(1, 101)], 99, 99.0),
        ([float(n) for n in range(1, 101)], 50, 50.0),
        ([float(n) for n in range(1, 21)], 99, 20.0),
    ],
)
def test_percentile_is_nearest_rank(
    values: list[float], rank: float, expected: float
) -> None:
    from gateway.stage_timing import percentile

    assert percentile(sorted(values), rank) == expected


def test_only_sampled_stages_keep_their_calls() -> None:
    """Keeping every call of a per-message stage would be a cost of its own."""
    timings = StageTimings(sampled=frozenset({"state.insert"}))
    for _ in range(3):
        timings.add("process.parse", 0.001)
        timings.add("state.insert", 0.010)

    snapshot = timings.snapshot()
    assert timings.samples("process.parse") == []
    assert len(timings.samples("state.insert")) == 3
    assert snapshot["state_insert_p99_ms"] == pytest.approx(10.0)
    assert "process_parse_p99_ms" not in snapshot
