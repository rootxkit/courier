"""Remote ID observations to rows, and rows kept until they are written. P1-15.

The SQL is in `test_remote_id_store_pg.py`; this is everything that needs no
database.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from gateway import odid, remote_id_store
from gateway.remote_id import RemoteIdTracker, aircraft_id
from gateway.remote_id_store import PendingRows, RemoteIdRow, row_from_observation
from gateway.tests.rid_frames import NOW, FlatGeoid, basic, frame, location, pack


def observation(geoid: bool = True, **located: object) -> dict[str, object]:
    tracker = RemoteIdTracker(geoid=FlatGeoid() if geoid else None)
    found = tracker.take(frame(pack(basic(), location(**located))), now_s=0.0)  # type: ignore[arg-type]
    assert found is not None
    return found


def row(n: int = 0) -> RemoteIdRow:
    return row_from_observation(
        observation(), ts=NOW, payload=bytes([n]), geoid_model="EGM2008"
    )


def test_an_observation_becomes_a_row_with_both_heights_and_the_raw_frame() -> None:
    payload = pack(basic(), location())

    r = row_from_observation(
        observation(), ts=NOW, payload=payload, geoid_model="WGS84 EGM2008"
    )

    expected_id = aircraft_id(
        odid.BasicId(id_type=odid.IdType.SERIAL_NUMBER, ua_type=2, ua_id="SN-RID-0001")
    )
    assert r.aircraft_id == expected_id
    assert (r.ts, r.receiver_id, r.transmitter) == (NOW, "rx-1", "AA:BB:CC:00:00:01")
    assert (r.ua_id, r.id_type, r.ua_type) == ("SN-RID-0001", 1, 2)
    assert r.status == odid.Status.AIRBORNE
    assert (r.alt_hae_m, r.alt_amsl_m, r.geoid_model) == (520.0, 500.0, "WGS84 EGM2008")
    assert r.alt_above_takeoff_m == 30.0
    assert r.track_deg == 90.0
    assert r.vy_ms == pytest.approx(10.0)
    assert r.rssi_dbm == -71.0
    assert r.payload == payload


def test_without_an_amsl_height_no_geoid_is_named() -> None:
    r = row_from_observation(
        observation(geoid=False), ts=NOW, payload=b"x", geoid_model="WGS84 EGM2008"
    )
    assert (r.alt_hae_m, r.alt_amsl_m, r.geoid_model) == (520.0, None, None)


class Writer:
    def __init__(self) -> None:
        self.batches: list[list[RemoteIdRow]] = []
        self.fail = False

    async def write(self, rows: list[RemoteIdRow]) -> None:
        if self.fail:
            raise OperationalError("INSERT", {}, ConnectionRefusedError())
        self.batches.append(rows)


async def test_pending_rows_are_written_in_one_batch() -> None:
    writer = Writer()
    pending = PendingRows(writer=writer)
    for n in range(3):
        pending.add(row(n))

    await pending.flush()

    assert [[r.payload for r in b] for b in writer.batches] == [[b"\0", b"\1", b"\2"]]
    assert (pending.pending, pending.written, pending.dropped) == (0, 3, 0)


async def test_a_failed_write_keeps_the_rows_for_the_next_one() -> None:
    writer = Writer()
    pending = PendingRows(writer=writer)
    pending.add(row(0))
    writer.fail = True

    await pending.flush()
    pending.add(row(1))
    assert pending.pending == 2
    writer.fail = False
    await pending.flush()

    assert [[r.payload for r in b] for b in writer.batches] == [[b"\0", b"\1"]]
    assert pending.written == 2


async def test_past_the_limit_the_oldest_are_dropped_and_counted() -> None:
    writer = Writer()
    writer.fail = True
    pending = PendingRows(writer=writer, max_pending=2)
    for n in range(4):
        pending.add(row(n))
    await pending.flush()

    assert pending.dropped == 2
    writer.fail = False
    await pending.flush()
    assert [r.payload for r in writer.batches[0]] == [b"\2", b"\3"]


async def test_nothing_pending_writes_nothing() -> None:
    writer = Writer()
    await PendingRows(writer=writer).flush()
    assert writer.batches == []


async def test_a_database_down_for_minutes_is_logged_now_and_then_not_every_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    said: list[tuple[str, dict[str, Any]]] = []
    for level in ("error", "warning"):
        monkeypatch.setattr(
            remote_id_store._log,
            level,
            lambda message, *a, extra=None, **k: said.append((message, extra or {})),
        )
    now = [0.0]
    writer = Writer()
    writer.fail = True
    pending = PendingRows(writer=writer, clock_s=lambda: now[0])
    pending.add(row(0))

    for now[0] in (0.0, 0.5, 30.0, 59.5):
        await pending.flush()
    assert [m for m, _ in said] == [
        "could not store remote id observations; will retry"
    ]

    now[0] = 61.0
    await pending.flush()
    assert said[-1][1]["failing_for_s"] == 61.0

    writer.fail = False
    now[0] = 90.0
    await pending.flush()
    assert said[-1][0] == "remote id observations stored again"
    assert said[-1][1]["failing_for_s"] == 90.0
    assert pending.written == 1
