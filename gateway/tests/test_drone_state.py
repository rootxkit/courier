"""Assembling drone_state rows, and writing the first of them.

The end-to-end test goes datagram -> parse -> classify -> convert -> resolve ->
row -> hypertable, against the real database. Each of those steps has its own
tests; this one exists because the seams between them are where a field ends up
in the wrong column, and no unit test looks at a seam.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from pymavlink.dialects.v20 import ardupilotmega as mavlink
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.binding import BindingResolver
from gateway.classify import Source, SourceKind, SourceRegistry
from gateway.drone_state import (
    StateAssembler,
    flight_mode_name,
    is_armed,
    timestamp_from_recv_utc_ns,
)
from gateway.parsing import ParsedMessage, SourceId, parse_datagram
from gateway.state_writer import DroneStateWriter

# NOT a module-level `postgres` mark. The assembler tests need no database,
# and marking the whole file would exclude them from the default run - which
# showed up as drone_state.py sitting at 44% coverage in a suite that was in
# fact testing it thoroughly, just not where the gate was looking.
ADDRESS = SourceId(sysid=1, compid=1)
NOON = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
TBILISI_LAT_E7 = 417_151_000
TBILISI_LON_E7 = 448_271_000


@pytest.fixture
async def station(
    engine: AsyncEngine, request: pytest.FixtureRequest
) -> AsyncIterator[str]:
    name = f"ds-{abs(hash(request.node.nodeid)) % 10**12}"
    try:
        yield name
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM drone_state WHERE station_id = :s"), {"s": name}
            )
            await connection.execute(
                sa.text("DELETE FROM source_bindings WHERE station_id = :s"),
                {"s": name},
            )
            await connection.execute(
                sa.text("DELETE FROM known_drones WHERE label LIKE :p"),
                {"p": f"{name}%"},
            )


@pytest.fixture
def writer(engine: AsyncEngine) -> DroneStateWriter:
    return DroneStateWriter(engine=engine)


@pytest.fixture
def resolver(engine: AsyncEngine) -> BindingResolver:
    return BindingResolver(engine=engine)


def link(sysid: int = 1, compid: int = 1) -> mavlink.MAVLink:
    built = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    built.signing.sign_outgoing = False
    return built


def heartbeat_bytes(*, armed: bool = True, custom_mode: int = 4) -> bytes:
    sender = link()
    base_mode = int(mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
    if armed:
        base_mode |= int(mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
    return bytes(
        sender.heartbeat_encode(
            mavlink.MAV_TYPE_QUADROTOR,
            mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            base_mode,
            custom_mode,
            mavlink.MAV_STATE_ACTIVE,
        ).pack(sender)
    )


def position_bytes(
    *,
    alt_mm: int = 450_000,
    rel_alt_mm: int = 60_000,
    lat: int = TBILISI_LAT_E7,
    lon: int = TBILISI_LON_E7,
) -> bytes:
    sender = link()
    return bytes(
        sender.global_position_int_encode(
            0, lat, lon, alt_mm, rel_alt_mm, 1000, -250, 150, 9000
        ).pack(sender)
    )


def ekf_status_bytes(*, has_position: bool) -> bytes:
    """EKF_STATUS_REPORT with or without absolute horizontal position.

    The "without" value is the one a real aircraft sent indoors on
    2026-09-24: attitude, both velocities and vertical position valid, and
    CONST_POS_MODE set because there is no horizontal source.
    """
    sender = link()
    flags = 0xA7
    if has_position:
        flags |= int(mavlink.EKF_POS_HORIZ_ABS)
    return bytes(
        sender.ekf_status_report_encode(flags, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1).pack(
            sender
        )
    )


def sys_status_bytes(*, remaining: int = 87) -> bytes:
    sender = link()
    return bytes(
        sender.sys_status_encode(
            0, 0, 0, 500, 12_600, 350, remaining, 0, 0, 0, 0, 0, 0
        ).pack(sender)
    )


def gps_bytes(*, sats: int = 14) -> bytes:
    sender = link()
    return bytes(
        sender.gps_raw_int_encode(
            0, 3, TBILISI_LAT_E7, TBILISI_LON_E7, 450_000, 120, 130, 1234, 4500, sats
        ).pack(sender)
    )


def vfr_bytes() -> bytes:
    sender = link()
    return bytes(sender.vfr_hud_encode(12.0, 12.5, 90, 45, 450.0, 1.5).pack(sender))


def first(datagram: bytes) -> ParsedMessage:
    return parse_datagram(datagram).messages[0]


def vehicle() -> Source:
    return Source(source_id=ADDRESS, kind=SourceKind.VEHICLE, heartbeat_count=1)


async def bound_drone(
    resolver: BindingResolver, station: str, *, bound_from: datetime = NOON
) -> UUID:
    drone_id = uuid4()
    await resolver.register_drone(drone_id, f"{station}-aircraft")
    await resolver.bind(
        station, ADDRESS, drone_id, bound_from=bound_from, created_by="test"
    )
    return drone_id


# --- assembling ------------------------------------------------------------


def test_a_row_is_emitted_on_position_and_not_on_other_messages() -> None:
    """Emitting per message would make the row rate QGC's rate.

    §6.4 forbids depending on any message arriving at any rate, and a row per
    message type would also fill the hypertable with rows differing in one
    field.
    """
    assembler = StateAssembler(station_id="s")
    drone_id = uuid4()

    assert (
        assembler.observe(
            vehicle(), first(heartbeat_bytes()), drone_id=drone_id, ts=NOON
        )
        is None
    )
    assert (
        assembler.observe(
            vehicle(), first(sys_status_bytes()), drone_id=drone_id, ts=NOON
        )
        is None
    )
    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )

    assert row is not None
    assert row.has_position


def test_the_row_carries_everything_accumulated_so_far() -> None:
    """A row is the vehicle's state, not one message's contents."""
    assembler = StateAssembler(station_id="s")
    drone_id = uuid4()

    for datagram in (heartbeat_bytes(), sys_status_bytes(), gps_bytes(), vfr_bytes()):
        assembler.observe(vehicle(), first(datagram), drone_id=drone_id, ts=NOON)
    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )

    assert row is not None
    assert row.lat_deg == pytest.approx(41.7151)
    assert row.alt_amsl_m == pytest.approx(450.0)
    assert row.alt_above_home_m == pytest.approx(60.0)
    assert row.batt_pct == pytest.approx(87.0)
    assert row.batt_voltage_v == pytest.approx(12.6)
    assert row.gps_fix_type == 3
    assert row.sat_count == 14
    assert row.groundspeed_ms == pytest.approx(12.5)
    assert row.climb_ms == pytest.approx(1.5)
    assert row.armed is True


def test_a_field_nobody_sent_is_none_not_zero() -> None:
    """A missing value is a missing value.

    Storing 0% for a battery the autopilot does not estimate would feed a
    failsafe decision a number nobody measured.
    """
    assembler = StateAssembler(station_id="s")
    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=uuid4(), ts=NOON
    )

    assert row is not None
    assert row.batt_pct is None
    assert row.batt_consumed_wh is None
    assert row.mode is None
    assert row.sat_count is None


def test_an_unknown_battery_does_not_erase_a_known_one() -> None:
    """SYS_STATUS sends -1 when it does not estimate.

    Letting that clear a value BATTERY_STATUS supplied would make the reading
    flicker at whatever rate the two messages interleave.
    """
    assembler = StateAssembler(station_id="s")
    drone_id = uuid4()

    assembler.observe(
        vehicle(), first(sys_status_bytes(remaining=87)), drone_id=drone_id, ts=NOON
    )
    assembler.observe(
        vehicle(), first(sys_status_bytes(remaining=-1)), drone_id=drone_id, ts=NOON
    )
    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )

    assert row is not None
    assert row.batt_pct == pytest.approx(87.0)


def test_two_stations_keep_separate_accumulators() -> None:
    """§8: two stations relaying one vehicle are independent observations.

    A shared accumulator would blend two links into a state neither saw.
    """
    one = StateAssembler(station_id="alpha")
    two = StateAssembler(station_id="bravo")
    drone_id = uuid4()

    one.observe(
        vehicle(), first(sys_status_bytes(remaining=87)), drone_id=drone_id, ts=NOON
    )
    row_two = two.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )

    assert row_two is not None
    assert row_two.station_id == "bravo"
    assert row_two.batt_pct is None


def test_the_row_has_no_agl_field() -> None:
    """P5-00 owns AGL. Until then the field does not exist to be filled."""
    assembler = StateAssembler(station_id="s")
    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=uuid4(), ts=NOON
    )
    assert row is not None
    assert not hasattr(row, "alt_agl_m")


# --- heartbeat decoding ----------------------------------------------------


def test_armed_is_read_from_pymavlinks_flag_not_from_128() -> None:
    assert is_armed(first(heartbeat_bytes(armed=True)).payload) is True
    assert is_armed(first(heartbeat_bytes(armed=False)).payload) is False


def test_the_flight_mode_is_looked_up_not_transcribed() -> None:
    """custom_mode 4 is GUIDED on a copter, per pymavlink's own mapping."""
    assert flight_mode_name(first(heartbeat_bytes(custom_mode=4)).payload) == "GUIDED"


def test_an_unmapped_mode_keeps_its_number() -> None:
    """An unrecognised mode is information; discarding it hides new firmware."""
    assert flight_mode_name(first(heartbeat_bytes(custom_mode=250)).payload) == "250"


# --- timestamps ------------------------------------------------------------


def test_the_timestamp_comes_from_the_records_capture_time() -> None:
    """Not the ingest time. A replayed backlog belongs where it happened."""
    recv_utc_ns = int(NOON.timestamp()) * 1_000_000_000
    assert timestamp_from_recv_utc_ns(recv_utc_ns) == NOON


# --- writing ---------------------------------------------------------------


@pytest.mark.postgres
async def test_the_first_rows_reach_the_hypertable(
    writer: DroneStateWriter,
    resolver: BindingResolver,
    engine: AsyncEngine,
    station: str,
) -> None:
    """End to end: datagram in, row out, through every layer.

    Each layer has its own tests. This one exists for the seams between them,
    which is where a value lands in the wrong column and nothing complains.
    """
    drone_id = await bound_drone(resolver, station)
    registry = SourceRegistry(station_id=station)
    assembler = StateAssembler(station_id=station)

    rows = []
    for offset, datagram in enumerate(
        (
            heartbeat_bytes(),
            sys_status_bytes(),
            gps_bytes(),
            vfr_bytes(),
            position_bytes(),
        )
    ):
        message = parse_datagram(datagram).messages[0]
        source = registry.observe(message)
        ts = NOON + timedelta(milliseconds=offset * 100)
        resolution = await resolver.resolve(station, source, at=ts)
        assert resolution.drone_id == drone_id, resolution.unclaimed_reason
        row = assembler.observe(source, message, drone_id=resolution.drone_id, ts=ts)
        if row is not None:
            rows.append(row)

    written = await writer.write(rows)

    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                sa.text(
                    "SELECT drone_id, ts, alt_amsl_m, alt_above_home_m, batt_pct, "
                    "       sat_count, mode, armed, "
                    "       ST_Y(geom) AS lat, ST_X(geom) AS lon, ST_SRID(geom) AS srid "
                    "FROM drone_state WHERE station_id = :s ORDER BY ts"
                ),
                {"s": station},
            )
        ).all()

    assert written == 1
    assert len(stored) == 1
    # A distinct name from the assembled `row` above: the database row and the
    # DroneStateRow are different shapes and conflating them hid a type error.
    persisted = stored[0]
    assert persisted.drone_id == drone_id
    # Latitude is Y and longitude is X. Swapping them produces a point that is
    # valid, plots somewhere real, and is wrong.
    assert persisted.lat == pytest.approx(41.7151)
    assert persisted.lon == pytest.approx(44.8271)
    assert persisted.srid == 4326
    assert persisted.alt_amsl_m == pytest.approx(450.0)
    assert persisted.alt_above_home_m == pytest.approx(60.0)
    assert persisted.batt_pct == pytest.approx(87.0)
    assert persisted.sat_count == 14
    assert persisted.mode == "GUIDED"
    assert persisted.armed is True


@pytest.mark.postgres
async def test_drone_state_has_no_agl_column(engine: AsyncEngine) -> None:
    """The column is absent, not nullable.

    A nullable alt_agl_m that is always null invites someone to fill it from
    relative_alt, and the error is smooth, plausible and silent.
    """
    async with engine.connect() as connection:
        columns = {
            row.column_name
            for row in (
                await connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'drone_state'"
                    )
                )
            ).all()
        }

    assert "alt_agl_m" not in columns
    assert {"alt_amsl_m", "alt_above_home_m"} <= columns


@pytest.mark.postgres
async def test_a_replayed_row_is_ignored_rather_than_duplicated(
    writer: DroneStateWriter,
    resolver: BindingResolver,
    engine: AsyncEngine,
    station: str,
) -> None:
    """A relay retransmitting after a lost ack replays converted records."""
    drone_id = await bound_drone(resolver, station)
    assembler = StateAssembler(station_id=station)
    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )
    assert row is not None

    await writer.write([row])
    await writer.write([row])

    async with engine.connect() as connection:
        count = await connection.scalar(
            sa.text("SELECT count(*) FROM drone_state WHERE station_id = :s"),
            {"s": station},
        )
    assert count == 1


@pytest.mark.postgres
async def test_two_stations_observing_one_vehicle_produce_two_rows(
    writer: DroneStateWriter,
    resolver: BindingResolver,
    engine: AsyncEngine,
    station: str,
) -> None:
    """§8: not duplicates. Two independent observations over different links.

    The paired presence test for the one above: dedupe must not collapse
    these, and after an incident the difference between them is evidence
    about the links.
    """
    drone_id = await bound_drone(resolver, station)
    other_station = f"{station}-b"
    await resolver.bind(
        other_station, ADDRESS, drone_id, bound_from=NOON, created_by="test"
    )

    rows = []
    for name in (station, other_station):
        assembler = StateAssembler(station_id=name)
        row = assembler.observe(
            vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
        )
        assert row is not None
        rows.append(row)

    try:
        await writer.write(rows)
        async with engine.connect() as connection:
            count = await connection.scalar(
                sa.text("SELECT count(*) FROM drone_state WHERE drone_id = :d"),
                {"d": str(drone_id)},
            )
        assert count == 2
    finally:
        async with engine.begin() as connection:
            for name in (station, other_station):
                await connection.execute(
                    sa.text("DELETE FROM drone_state WHERE station_id = :s"),
                    {"s": name},
                )
            await connection.execute(
                sa.text("DELETE FROM source_bindings WHERE station_id = :s"),
                {"s": other_station},
            )


@pytest.mark.postgres
async def test_the_database_rejects_a_heading_that_is_not_a_bearing(
    writer: DroneStateWriter,
    resolver: BindingResolver,
    station: str,
) -> None:
    """The last line against a sentinel that slipped through conversion.

    UINT16_MAX in cdeg is 655.35 degrees. The converter resolves it to None,
    and if it ever stopped doing so the constraint refuses the row rather than
    storing a heading no compass produces.
    """
    from dataclasses import replace

    from gateway.ingest_store import StoreError

    drone_id = await bound_drone(resolver, station)
    assembler = StateAssembler(station_id=station)
    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )
    assert row is not None

    with pytest.raises(StoreError):
        await writer.write([replace(row, heading_deg=655.35)])


@pytest.mark.postgres
async def test_an_empty_batch_writes_nothing(writer: DroneStateWriter) -> None:
    assert await writer.write([]) == 0


# --- a row with no position ------------------------------------------------


def test_a_row_is_still_emitted_without_a_position() -> None:
    """Unknown is not a value, and it is not a reason to drop the row.

    An aircraft with no EKF origin is present. Its heading, battery and mode
    are real measurements; only the coordinate is unknown.
    """
    assembler = StateAssembler(station_id="s")
    drone_id = uuid4()
    assembler.observe(vehicle(), first(heartbeat_bytes()), drone_id=drone_id, ts=NOON)
    assembler.observe(
        vehicle(),
        first(ekf_status_bytes(has_position=False)),
        drone_id=drone_id,
        ts=NOON,
    )

    row = assembler.observe(
        vehicle(), first(position_bytes(lat=0, lon=0)), drone_id=drone_id, ts=NOON
    )

    assert row is not None
    assert row.lat_deg is None
    assert row.lon_deg is None
    assert row.has_position is False
    # Everything the aircraft does know is still there.
    assert row.heading_deg == pytest.approx(90.0)
    assert row.mode == "GUIDED"
    assert row.alt_above_home_m == pytest.approx(60.0)


def test_an_ekf_that_regains_position_starts_writing_one() -> None:
    """The paired presence test.

    Without it, an assembler that never stored a position would pass the one
    above.
    """
    assembler = StateAssembler(station_id="s")
    drone_id = uuid4()
    assembler.observe(
        vehicle(),
        first(ekf_status_bytes(has_position=True)),
        drone_id=drone_id,
        ts=NOON,
    )

    row = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )

    assert row is not None
    assert row.lat_deg == pytest.approx(41.7151)
    assert row.has_position is True


def test_losing_the_position_clears_it_rather_than_keeping_the_last_one() -> None:
    """A stale coordinate presented as current is the same failure as 0,0.

    It is worse, in fact: it is plausible, and it is exactly where the
    aircraft was a moment ago.
    """
    assembler = StateAssembler(station_id="s")
    drone_id = uuid4()
    assembler.observe(
        vehicle(),
        first(ekf_status_bytes(has_position=True)),
        drone_id=drone_id,
        ts=NOON,
    )
    good = assembler.observe(
        vehicle(), first(position_bytes()), drone_id=drone_id, ts=NOON
    )
    assert good is not None and good.lat_deg is not None

    assembler.observe(
        vehicle(),
        first(ekf_status_bytes(has_position=False)),
        drone_id=drone_id,
        ts=NOON,
    )
    lost = assembler.observe(
        vehicle(), first(position_bytes(lat=0, lon=0)), drone_id=drone_id, ts=NOON
    )

    assert lost is not None
    assert lost.lat_deg is None


@pytest.mark.postgres
async def test_a_row_without_a_position_stores_geom_as_null(
    writer: DroneStateWriter,
    resolver: BindingResolver,
    engine: AsyncEngine,
    station: str,
) -> None:
    """NULL in the database, not a point at 0,0.

    This is the whole ruling, checked where it matters: a spatial query must
    find nothing rather than find a drone in the Gulf of Guinea.
    """
    drone_id = await bound_drone(resolver, station)
    assembler = StateAssembler(station_id=station)
    assembler.observe(
        vehicle(),
        first(ekf_status_bytes(has_position=False)),
        drone_id=drone_id,
        ts=NOON,
    )
    row = assembler.observe(
        vehicle(), first(position_bytes(lat=0, lon=0)), drone_id=drone_id, ts=NOON
    )
    assert row is not None

    await writer.write([row])

    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                sa.text(
                    "SELECT geom IS NULL AS no_geom, heading_deg, alt_above_home_m "
                    "FROM drone_state WHERE station_id = :s"
                ),
                {"s": station},
            )
        ).one()

    assert stored.no_geom is True
    # The rest of the row is real and present.
    assert stored.heading_deg == pytest.approx(90.0)
    assert stored.alt_above_home_m == pytest.approx(60.0)
