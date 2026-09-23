"""Binding a MAVLink address to a drone, resolved at the record's timestamp.

The test that carries the weight is `test_a_replayed_backlog_crossing_a_binding
_change_splits_correctly`: one batch, one address, two drones, because the
records were captured either side of a reassignment. That is the case the relay
makes ordinary, and resolving at ingest time would get every record before the
change wrong while looking entirely healthy.

The overlap and foreign-key guarantees are tested by *attempting the insert*.
Asserting that application code checks first would prove nothing about two
concurrent writers; asserting the database refuses it proves the property.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gateway.binding import (
    UNBOUND,
    UNCLASSIFIED,
    Binding,
    BindingConflictError,
    BindingResolver,
)
from gateway.classify import Source, SourceKind
from gateway.parsing import SourceId

pytestmark = pytest.mark.postgres

ADDRESS = SourceId(sysid=1, compid=1)
NOON = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

# The reassignment instant used throughout: the old airframe's binding ends
# here and the new one begins at the same moment.
HANDOVER = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)


def database_url() -> str:
    url = os.environ.get("TELEMETRY_DATABASE_URL")
    if not url:
        pytest.skip("TELEMETRY_DATABASE_URL is not set; `make up` starts the stack")
    return url


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(database_url())
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
async def station(
    engine: AsyncEngine, request: pytest.FixtureRequest
) -> AsyncIterator[str]:
    name = f"bind-{abs(hash(request.node.nodeid)) % 10**12}"
    try:
        yield name
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM source_bindings WHERE station_id = :s"),
                {"s": name},
            )
            await connection.execute(
                sa.text("DELETE FROM ingest_events WHERE station_id = :s"),
                {"s": name},
            )
            # Drones are referenced by bindings, so they go last.
            await connection.execute(
                sa.text("DELETE FROM known_drones WHERE label LIKE :p"),
                {"p": f"{name}%"},
            )


@pytest.fixture
def resolver(engine: AsyncEngine) -> BindingResolver:
    return BindingResolver(engine=engine)


def vehicle(source_id: SourceId = ADDRESS) -> Source:
    return Source(source_id=source_id, kind=SourceKind.VEHICLE, heartbeat_count=1)


def unclassified(source_id: SourceId = ADDRESS) -> Source:
    return Source(source_id=source_id, kind=SourceKind.UNCLASSIFIED)


async def a_drone(resolver: BindingResolver, station: str, name: str) -> UUID:
    drone_id = uuid4()
    await resolver.register_drone(drone_id, f"{station}-{name}")
    return drone_id


# --- resolution at the record's timestamp ----------------------------------


async def test_a_replayed_backlog_crossing_a_binding_change_splits_correctly(
    resolver: BindingResolver, station: str
) -> None:
    """The case the relay makes real, and the reason validity exists.

    A station is offline for hours, the SYSID is reassigned during the outage,
    and the whole backlog arrives in one batch afterwards. Every record must
    resolve against the binding in force when it was *captured*. Resolving the
    batch at ingest time - at any single timestamp - would attribute the older
    half of the flight to the wrong airframe, and nothing about the result
    would look wrong.
    """
    old_airframe = await a_drone(resolver, station, "old")
    new_airframe = await a_drone(resolver, station, "new")

    await resolver.bind(
        station,
        ADDRESS,
        old_airframe,
        bound_from=NOON,
        bound_until=HANDOVER,
        created_by="test",
    )
    await resolver.bind(
        station, ADDRESS, new_airframe, bound_from=HANDOVER, created_by="test"
    )

    # One batch, timestamps either side of the reassignment.
    batch = [
        (vehicle(), HANDOVER - timedelta(hours=1)),
        (vehicle(), HANDOVER - timedelta(minutes=1)),
        (vehicle(), HANDOVER),
        (vehicle(), HANDOVER + timedelta(minutes=1)),
    ]

    resolved = await resolver.resolve_batch(station, batch)

    assert [r.drone_id for r in resolved] == [
        old_airframe,
        old_airframe,
        # The boundary instant belongs to the NEW binding: tstzrange is
        # [lower, upper), so the old binding's upper bound is exclusive.
        new_airframe,
        new_airframe,
    ]


async def test_resolution_uses_the_record_timestamp_not_the_time_of_asking(
    resolver: BindingResolver, station: str
) -> None:
    """The paired test, stated the other way round.

    The binding in force *now* is the new one. A record captured before the
    handover must still resolve to the old airframe, however long afterwards
    the question is asked.
    """
    old_airframe = await a_drone(resolver, station, "old")
    new_airframe = await a_drone(resolver, station, "new")
    await resolver.bind(
        station,
        ADDRESS,
        old_airframe,
        bound_from=NOON,
        bound_until=HANDOVER,
        created_by="test",
    )
    await resolver.bind(
        station, ADDRESS, new_airframe, bound_from=HANDOVER, created_by="test"
    )

    long_after = await resolver.resolve(
        station, vehicle(), at=HANDOVER - timedelta(hours=1)
    )
    right_now = await resolver.resolve(
        station, vehicle(), at=HANDOVER + timedelta(days=30)
    )

    assert long_after.drone_id == old_airframe
    assert right_now.drone_id == new_airframe


async def test_the_python_range_check_agrees_with_postgresql(
    resolver: BindingResolver, engine: AsyncEngine, station: str
) -> None:
    """Pins the duplication.

    Resolution is done in Python so a backlog is not one round trip per
    record, which means the `[lower, upper)` arithmetic exists twice. This
    compares it against PostgreSQL's own containment operator at the instants
    where an off-by-one lives: one microsecond either side of each bound, and
    the bounds themselves.
    """
    drone_id = await a_drone(resolver, station, "one")
    await resolver.bind(
        station,
        ADDRESS,
        drone_id,
        bound_from=NOON,
        bound_until=HANDOVER,
        created_by="test",
    )
    binding = (await resolver.bindings_for(station, ADDRESS))[0]

    tick = timedelta(microseconds=1)
    moments = [
        NOON - tick,
        NOON,
        NOON + tick,
        HANDOVER - tick,
        HANDOVER,
        HANDOVER + tick,
    ]

    async with engine.connect() as connection:
        for moment in moments:
            database_says = await connection.scalar(
                sa.text(
                    "SELECT valid @> CAST(:moment AS timestamptz) "
                    "FROM source_bindings "
                    "WHERE station_id = :s AND sysid = :sysid AND compid = :compid"
                ),
                {
                    "moment": moment,
                    "s": station,
                    "sysid": ADDRESS.sysid,
                    "compid": ADDRESS.compid,
                },
            )
            assert binding.covers(moment) is bool(database_says), (
                f"disagreement at {moment.isoformat()}: "
                f"python={binding.covers(moment)} postgres={database_says}"
            )


async def test_an_open_ended_binding_covers_everything_after_it(
    resolver: BindingResolver, station: str
) -> None:
    drone_id = await a_drone(resolver, station, "one")
    await resolver.bind(station, ADDRESS, drone_id, bound_from=NOON, created_by="t")

    far_future = await resolver.resolve(
        station, vehicle(), at=NOON + timedelta(days=3650)
    )
    assert far_future.drone_id == drone_id


async def test_a_record_before_any_binding_is_unbound(
    resolver: BindingResolver, station: str
) -> None:
    """A binding is not retroactive.

    Telemetry captured before anyone said what this address was belongs to no
    drone, and must not be back-attributed by a binding created later.
    """
    drone_id = await a_drone(resolver, station, "one")
    await resolver.bind(station, ADDRESS, drone_id, bound_from=NOON, created_by="t")

    earlier = await resolver.resolve(station, vehicle(), at=NOON - timedelta(seconds=1))

    assert earlier.drone_id is None
    assert earlier.unclaimed_reason == UNBOUND


# --- the database is what guarantees non-overlap ---------------------------


async def test_two_overlapping_bindings_are_refused_by_the_database(
    resolver: BindingResolver, station: str
) -> None:
    """Attempted, not assumed.

    Two concurrent writers would both check first, both find nothing and both
    insert. Only the constraint makes the overlap impossible.
    """
    first = await a_drone(resolver, station, "first")
    second = await a_drone(resolver, station, "second")
    await resolver.bind(
        station,
        ADDRESS,
        first,
        bound_from=NOON,
        bound_until=HANDOVER,
        created_by="t",
    )

    with pytest.raises(BindingConflictError, match="overlaps an existing binding"):
        await resolver.bind(
            station,
            ADDRESS,
            second,
            bound_from=NOON + timedelta(minutes=30),
            bound_until=HANDOVER + timedelta(hours=1),
            created_by="t",
        )


async def test_an_open_binding_blocks_a_successor_until_it_is_closed(
    resolver: BindingResolver, station: str
) -> None:
    """§7: reassigning a SYSID closes one binding and opens another.

    The open-ended binding overlaps everything after it, so the close is not
    bookkeeping - it is what makes the next bind legal.
    """
    first = await a_drone(resolver, station, "first")
    second = await a_drone(resolver, station, "second")
    await resolver.bind(station, ADDRESS, first, bound_from=NOON, created_by="t")

    with pytest.raises(BindingConflictError):
        await resolver.bind(
            station, ADDRESS, second, bound_from=HANDOVER, created_by="t"
        )

    closed = await resolver.close_binding(station, ADDRESS, at=HANDOVER)
    await resolver.bind(station, ADDRESS, second, bound_from=HANDOVER, created_by="t")

    assert closed == 1
    assert [b.drone_id for b in await resolver.bindings_for(station, ADDRESS)] == [
        first,
        second,
    ]


async def test_adjacent_bindings_do_not_overlap(
    resolver: BindingResolver, station: str
) -> None:
    """The paired presence test for the constraint.

    A constraint that refused everything would pass the tests above. Touching
    bindings - one ending exactly where the next begins - must be accepted,
    which is precisely what `[lower, upper)` bounds are for.
    """
    first = await a_drone(resolver, station, "first")
    second = await a_drone(resolver, station, "second")

    await resolver.bind(
        station,
        ADDRESS,
        first,
        bound_from=NOON,
        bound_until=HANDOVER,
        created_by="t",
    )
    await resolver.bind(station, ADDRESS, second, bound_from=HANDOVER, created_by="t")

    assert len(await resolver.bindings_for(station, ADDRESS)) == 2


async def test_the_same_address_on_another_station_is_independent(
    resolver: BindingResolver, station: str, engine: AsyncEngine
) -> None:
    """The key is (station_id, sysid, compid).

    Two stations may each carry a SYSID 1, and they are not the same aircraft.
    """
    here = await a_drone(resolver, station, "here")
    there = await a_drone(resolver, station, "there")
    other_station = f"{station}-other"

    await resolver.bind(station, ADDRESS, here, bound_from=NOON, created_by="t")
    await resolver.bind(other_station, ADDRESS, there, bound_from=NOON, created_by="t")

    try:
        assert (
            await resolver.resolve(station, vehicle(), at=HANDOVER)
        ).drone_id == here
        assert (
            await resolver.resolve(other_station, vehicle(), at=HANDOVER)
        ).drone_id == there
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM source_bindings WHERE station_id = :s"),
                {"s": other_station},
            )


async def test_a_different_component_on_one_sysid_is_a_different_binding(
    resolver: BindingResolver, station: str
) -> None:
    """A gimbal at 1/154 is not the aircraft at 1/1."""
    aircraft = await a_drone(resolver, station, "aircraft")
    await resolver.bind(station, ADDRESS, aircraft, bound_from=NOON, created_by="t")

    gimbal_address = SourceId(sysid=1, compid=154)
    resolution = await resolver.resolve(station, vehicle(gimbal_address), at=HANDOVER)

    assert resolution.drone_id is None
    assert resolution.unclaimed_reason == UNBOUND


# --- a drone that does not exist -------------------------------------------


async def test_binding_to_an_unregistered_drone_is_refused(
    resolver: BindingResolver, station: str
) -> None:
    """A constraint violation, never a silent skip.

    The Gateway cannot reach the relational fleet registry, so known_drones is
    the projection a foreign key can point at.
    """
    with pytest.raises(BindingConflictError, match="not in known_drones"):
        await resolver.bind(station, ADDRESS, uuid4(), bound_from=NOON, created_by="t")


async def test_a_drone_with_bindings_cannot_be_deleted(
    resolver: BindingResolver, engine: AsyncEngine, station: str
) -> None:
    """An aircraft that has flown is not erased from under its own history."""
    drone_id = await a_drone(resolver, station, "one")
    await resolver.bind(station, ADDRESS, drone_id, bound_from=NOON, created_by="t")

    with pytest.raises(Exception, match="source_bindings_drone_id_fkey"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM known_drones WHERE drone_id = :d"),
                {"d": str(drone_id)},
            )


# --- never auto-registered -------------------------------------------------


async def test_an_unclassified_source_never_resolves(
    resolver: BindingResolver, station: str
) -> None:
    """No HEARTBEAT has arrived, so nothing has said what this is.

    §7: no auto-registration. A misconfigured aircraft must not walk itself
    into the fleet.
    """
    resolution = await resolver.resolve(station, unclassified(), at=HANDOVER)

    assert resolution.drone_id is None
    assert resolution.unclaimed_reason == UNCLASSIFIED


async def test_a_ground_station_never_resolves_even_if_bound(
    resolver: BindingResolver, station: str
) -> None:
    """Classification gates resolution, not the other way round.

    A binding row for a GCS address would be a configuration error; it must
    not be able to turn a ground station into a drone.
    """
    drone_id = await a_drone(resolver, station, "one")
    await resolver.bind(station, ADDRESS, drone_id, bound_from=NOON, created_by="t")

    gcs = Source(source_id=ADDRESS, kind=SourceKind.GCS, heartbeat_count=1)
    resolution = await resolver.resolve(station, gcs, at=HANDOVER)

    assert resolution.drone_id is None


async def test_a_vehicle_with_the_same_binding_does_resolve(
    resolver: BindingResolver, station: str
) -> None:
    """The paired presence test for the two above.

    Identical address, identical binding, and the only difference is the
    classification - so the refusals above are about the classification and
    not about something else being broken.
    """
    drone_id = await a_drone(resolver, station, "one")
    await resolver.bind(station, ADDRESS, drone_id, bound_from=NOON, created_by="t")

    assert (
        await resolver.resolve(station, vehicle(), at=HANDOVER)
    ).drone_id == drone_id


async def test_an_unclaimed_source_is_recorded_as_an_event(
    resolver: BindingResolver, engine: AsyncEngine, station: str
) -> None:
    """§7: normal during setup, serious in flight.

    So it is an event and a console state, not a log line nobody reads.
    """
    resolution = await resolver.resolve(station, vehicle(), at=HANDOVER)
    await resolver.record_unclaimed(station, None, resolution)

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT event_type, payload FROM ingest_events "
                    "WHERE station_id = :s"
                ),
                {"s": station},
            )
        ).all()

    assert [row.event_type for row in rows] == ["unclaimed_source"]
    assert rows[0].payload["sysid"] == ADDRESS.sysid
    assert rows[0].payload["reason"] == UNBOUND


# --- the range arithmetic, without a database ------------------------------


def test_covers_is_inclusive_below_and_exclusive_above() -> None:
    """Stated once, in the smallest possible test.

    The database comparison above is the authority; this one makes the
    intended rule legible without reading a range literal.
    """
    binding = Binding(
        station_id="s",
        source_id=ADDRESS,
        drone_id=uuid4(),
        bound_from=NOON,
        bound_until=HANDOVER,
    )

    assert not binding.covers(NOON - timedelta(microseconds=1))
    assert binding.covers(NOON)
    assert binding.covers(HANDOVER - timedelta(microseconds=1))
    assert not binding.covers(HANDOVER)
