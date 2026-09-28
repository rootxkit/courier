"""Live state against a real Redis. P1-05.

Real Redis rather than a fake, because the behaviour under test is the Lua
compare-and-set and `PEXPIREAT`. A fake would test a re-implementation of
both, and agree with itself.

Every rule in `gateway/live_state.py` is exercised in both directions. A fresh
row is written and an old one is refused. A newer row replaces an older one
and never the reverse. The key exists, and then, after the timeout, it does
not: the flip that P1-05 defines as link lost.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis.asyncio

from gateway.drone_state import DroneStateRow
from gateway.live_state import LiveState, state_key

pytestmark = pytest.mark.redis


def redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is not set; `make up` starts Redis")
    return url


@pytest.fixture
async def client() -> AsyncIterator[Any]:
    connection = redis.asyncio.from_url(redis_url())
    try:
        yield connection
    finally:
        await connection.aclose()


@pytest.fixture
async def drones(client: Any) -> AsyncIterator[list[UUID]]:
    """Drone ids a test used, so their keys are deleted however it ends."""
    created: list[UUID] = []
    try:
        yield created
    finally:
        for drone_id in created:
            await client.delete(state_key(drone_id))


def a_drone(drones: list[UUID]) -> UUID:
    drone_id = uuid4()
    drones.append(drone_id)
    return drone_id


def row(drone_id: UUID, ts: datetime, lat_deg: float = 41.7151) -> DroneStateRow:
    return DroneStateRow(
        drone_id=drone_id,
        ts=ts,
        station_id="live-state-test",
        lat_deg=lat_deg,
        lon_deg=44.8271,
    )


def now() -> datetime:
    return datetime.now(tz=UTC)


async def test_a_fresh_row_makes_the_drone_live(
    client: Any, drones: list[UUID]
) -> None:
    live = LiveState(redis=client, link_timeout_s=15.0)
    drone = a_drone(drones)

    verdicts = await live.update([row(drone, now())])

    assert verdicts == {drone: 1}
    state = await live.get(drone)
    assert state is not None
    assert state["lat_deg"] == pytest.approx(41.7151)
    assert await live.is_live(drone)


async def test_a_replayed_row_older_than_the_timeout_never_makes_it_live(
    client: Any, drones: list[UUID]
) -> None:
    """A backlog replayed after an outage must not resurrect a lost link."""
    live = LiveState(redis=client, link_timeout_s=15.0)
    drone = a_drone(drones)

    verdicts = await live.update([row(drone, now() - timedelta(seconds=60))])

    assert verdicts == {drone: -1}
    assert not await live.is_live(drone)


async def test_an_older_row_never_overwrites_a_newer_one(
    client: Any, drones: list[UUID]
) -> None:
    """Two stations relaying one aircraft, or a backlog interleaving with the
    live stream. Last write must not win; newest capture must."""
    live = LiveState(redis=client, link_timeout_s=15.0)
    drone = a_drone(drones)
    fresh = now()

    await live.update([row(drone, fresh, lat_deg=41.0)])
    verdicts = await live.update([row(drone, fresh - timedelta(seconds=2), 40.0)])

    assert verdicts == {drone: 0}
    state = await live.get(drone)
    assert state is not None
    assert state["lat_deg"] == pytest.approx(41.0)


async def test_a_newer_row_replaces_an_older_one(
    client: Any, drones: list[UUID]
) -> None:
    """The presence half of the rule above."""
    live = LiveState(redis=client, link_timeout_s=15.0)
    drone = a_drone(drones)
    fresh = now()

    await live.update([row(drone, fresh - timedelta(seconds=2), lat_deg=40.0)])
    verdicts = await live.update([row(drone, fresh, lat_deg=41.0)])

    assert verdicts == {drone: 1}
    state = await live.get(drone)
    assert state is not None
    assert state["lat_deg"] == pytest.approx(41.0)


async def test_only_the_newest_row_in_a_batch_is_offered(
    client: Any, drones: list[UUID]
) -> None:
    live = LiveState(redis=client, link_timeout_s=15.0)
    drone = a_drone(drones)
    fresh = now()

    await live.update(
        [
            row(drone, fresh, lat_deg=41.0),
            row(drone, fresh - timedelta(seconds=1), lat_deg=40.0),
        ]
    )

    state = await live.get(drone)
    assert state is not None
    assert state["lat_deg"] == pytest.approx(41.0)


async def test_the_link_is_lost_when_the_timeout_passes(
    client: Any, drones: list[UUID]
) -> None:
    """P1-05's definition, exercised: live, then - with nothing new - not."""
    live = LiveState(redis=client, link_timeout_s=1.0)
    drone = a_drone(drones)

    await live.update([row(drone, now())])
    assert await live.is_live(drone)

    deadline = time.monotonic() + 3.0
    while await live.is_live(drone) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    assert not await live.is_live(drone)


async def test_a_station_clock_running_ahead_cannot_extend_the_timeout(
    client: Any, drones: list[UUID]
) -> None:
    """A capture time an hour in the future still expires one timeout from
    now, so a fast clock cannot keep a dead link looking alive."""
    live = LiveState(redis=client, link_timeout_s=15.0)
    drone = a_drone(drones)

    await live.update([row(drone, now() + timedelta(hours=1))])

    remaining_ms = await client.pttl(state_key(drone))
    assert 0 < remaining_ms <= 15_000
