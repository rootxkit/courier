"""The ingest pipeline: stored records in, drone_state rows out.

The behaviour worth testing here is not the conversion - every step has its own
tests - but the pipeline's obligations to the transport above it:

- it never raises, because a conversion fault is not a transport fault and must
  not stall a station;
- an unbound source produces no row and is announced once, not once per
  datagram;
- a GCS is never announced at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.binding import UNBOUND, UNCLASSIFIED, Resolution
from gateway.drone_state import DroneStateRow
from gateway.parsing import SourceId
from gateway.pipeline import IngestPipeline, StationPipelines
from gateway.relay_records import Record

STATION = "tbilisi-base-1"
EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"
DRONE = UUID("0b63df96-30ba-41ba-b3fe-7647edb2b0ee")
NOON_NS = int(datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp()) * 1_000_000_000


class FakeResolver:
    """Resolves everything to one drone, or to nothing."""

    def __init__(
        self,
        *,
        drone_id: UUID | None = DRONE,
        labels: dict[UUID, str] | None = None,
        labels_fail: bool = False,
    ) -> None:
        self.drone_id = drone_id
        self.unclaimed: list[Resolution] = []
        self.labels = labels if labels is not None else {DRONE: "SITL-01"}
        self.labels_fail = labels_fail

    async def labels_for(self, drone_ids: set[UUID]) -> dict[UUID, str]:
        if self.labels_fail:
            raise RuntimeError("known_drones is unreachable")
        return {
            drone_id: self.labels[drone_id]
            for drone_id in drone_ids
            if drone_id in self.labels
        }

    async def resolve(
        self, station_id: str, source: Any, *, at: datetime
    ) -> Resolution:
        if self.drone_id is None:
            reason = UNCLASSIFIED if source.kind.value == "unclassified" else UNBOUND
            return Resolution(source.source_id, None, reason)
        return Resolution(source.source_id, self.drone_id)

    async def record_unclaimed(
        self, station_id: str, epoch: str | None, resolution: Resolution
    ) -> None:
        self.unclaimed.append(resolution)


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.written: list[DroneStateRow] = []
        self.fail = fail

    async def write(self, rows: list[DroneStateRow]) -> int:
        if self.fail:
            raise RuntimeError("the database is gone")
        self.written.extend(rows)
        return len(rows)


class FakePublisher:
    def __init__(self) -> None:
        self.rows: list[DroneStateRow] = []
        self.labels: dict[UUID, str] = {}
        self.unclaimed: list[SourceId] = []

    async def publish_rows(
        self, rows: list[DroneStateRow], labels: dict[UUID, str] | None = None
    ) -> None:
        self.rows.extend(rows)
        self.labels = labels or {}

    async def publish_unclaimed(
        self, station_id: str, resolution: Resolution, source_id: SourceId
    ) -> None:
        self.unclaimed.append(source_id)


def build(
    resolver: FakeResolver | None = None,
    writer: FakeWriter | None = None,
    publisher: FakePublisher | None = None,
) -> tuple[IngestPipeline, FakeResolver, FakeWriter, FakePublisher]:
    resolver = resolver or FakeResolver()
    writer = writer or FakeWriter()
    publisher = publisher or FakePublisher()
    pipeline = IngestPipeline(
        station_id=STATION,
        resolver=cast(Any, resolver),
        writer=cast(Any, writer),
        publisher=cast(Any, publisher),
    )
    return pipeline, resolver, writer, publisher


def link(sysid: int = 1, compid: int = 1) -> mavlink.MAVLink:
    built = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    built.signing.sign_outgoing = False
    return built


def record(seq: int, datagram: bytes, *, offset_ns: int = 0) -> Record:
    return Record(seq=seq, recv_utc_ns=NOON_NS + offset_ns, datagram=datagram)


def heartbeat(sysid: int = 1, compid: int = 1, *, gcs: bool = False) -> bytes:
    sender = link(sysid, compid)
    return bytes(
        sender.heartbeat_encode(
            mavlink.MAV_TYPE_GCS if gcs else mavlink.MAV_TYPE_QUADROTOR,
            mavlink.MAV_AUTOPILOT_INVALID
            if gcs
            else mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            1,
            4,
            mavlink.MAV_STATE_ACTIVE,
        ).pack(sender)
    )


def position(sysid: int = 1, compid: int = 1) -> bytes:
    sender = link(sysid, compid)
    return bytes(
        sender.global_position_int_encode(
            0, 417151000, 448271000, 450000, 60000, 1000, -250, 150, 9000
        ).pack(sender)
    )


# --- the happy path --------------------------------------------------------


async def test_a_heartbeat_then_a_position_produces_one_row() -> None:
    pipeline, _, writer, publisher = build()

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert len(rows) == 1
    assert rows[0].drone_id == DRONE
    assert rows[0].station_id == STATION
    assert rows[0].lat_deg == pytest.approx(41.7151)
    assert writer.written == rows
    assert publisher.rows == rows


async def test_the_row_timestamp_is_the_records_capture_time() -> None:
    """Not ingest time. A replayed backlog belongs where it happened."""
    pipeline, _, _, _ = build()

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert rows[0].ts == datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


async def test_messages_that_are_not_on_the_hot_path_emit_no_row() -> None:
    """A row is emitted on position and nothing else."""
    pipeline, _, writer, _ = build()

    rows = await pipeline.process(EPOCH, [record(0, heartbeat())])

    assert rows == []
    assert writer.written == []


# --- unbound sources -------------------------------------------------------


async def test_an_unbound_source_produces_no_row() -> None:
    """§7: archived, never written, never auto-registered."""
    pipeline, _, writer, _ = build(resolver=FakeResolver(drone_id=None))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert rows == []
    assert writer.written == []


async def test_an_unbound_source_is_announced_once_not_once_per_datagram() -> None:
    """An unbound aircraft at 84 Hz would otherwise emit 84 events a second.

    That is how a genuinely useful signal becomes something operators filter
    out, so it is announced per address.
    """
    pipeline, resolver, _, publisher = build(resolver=FakeResolver(drone_id=None))

    for seq in range(20):
        await pipeline.process(EPOCH, [record(seq, position())])

    assert len(publisher.unclaimed) == 1
    assert len(resolver.unclaimed) == 1
    assert publisher.unclaimed[0] == SourceId(sysid=1, compid=1)


async def test_two_unbound_addresses_are_announced_separately() -> None:
    """Per address, so a second aircraft is not hidden by the first."""
    pipeline, _, _, publisher = build(resolver=FakeResolver(drone_id=None))

    await pipeline.process(
        EPOCH,
        [record(0, position(sysid=1)), record(1, position(sysid=2))],
    )

    assert set(publisher.unclaimed) == {SourceId(1, 1), SourceId(2, 1)}


async def test_a_ground_station_is_never_announced_as_unclaimed() -> None:
    """QGroundControl's own heartbeat is not an aircraft nobody bound.

    Announcing it would bury the case that matters, which arrives at the same
    rate on the same link.
    """
    pipeline, _, _, publisher = build(resolver=FakeResolver(drone_id=None))

    await pipeline.process(
        EPOCH, [record(0, heartbeat(sysid=255, compid=190, gcs=True))]
    )

    assert publisher.unclaimed == []


async def test_forgetting_an_address_allows_it_to_be_announced_again() -> None:
    """A binding that is later removed must be reported again.

    Silence after a binding disappears would read as everything being fine.
    """
    pipeline, _, _, publisher = build(resolver=FakeResolver(drone_id=None))
    await pipeline.process(EPOCH, [record(0, position())])

    pipeline.forget_unclaimed(SourceId(1, 1))
    await pipeline.process(EPOCH, [record(1, position())])

    assert len(publisher.unclaimed) == 2


# --- the pipeline never raises into the transport --------------------------


async def test_a_datagram_that_is_not_mavlink_does_not_raise() -> None:
    """The transport already stored it; the conversion is what fails.

    A datagram that is not MAVLink is exactly what relay-v1 §6 promises to
    forward unchanged, so it must reach here and be survivable.
    """
    pipeline, _, writer, _ = build()

    rows = await pipeline.process(EPOCH, [record(0, b"GET / HTTP/1.1\r\n\r\n")])

    assert rows == []
    assert writer.written == []


async def test_a_writer_failure_does_not_raise() -> None:
    """A failure here loses a derived view, not the flight record.

    The archive and the index are already written, so this must not propagate
    back into the transport and stall a station.
    """
    pipeline, _, _, publisher = build(writer=FakeWriter(fail=True))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    # The rows were assembled and returned; only the write failed.
    assert len(rows) == 1
    assert publisher.rows == []


async def test_a_resolver_failure_does_not_raise() -> None:
    class ExplodingResolver(FakeResolver):
        async def resolve(
            self, station_id: str, source: Any, *, at: datetime
        ) -> Resolution:
            raise RuntimeError("the database is gone")

    pipeline, _, writer, _ = build(resolver=ExplodingResolver())

    rows = await pipeline.process(EPOCH, [record(0, position())])

    assert rows == []
    assert writer.written == []


async def test_bad_frames_are_counted() -> None:
    """Corruption reaches somewhere it can be seen rather than vanishing."""
    pipeline, _, _, _ = build()
    damaged = bytearray(heartbeat())
    damaged[6] ^= 0xFF

    await pipeline.process(EPOCH, [record(0, bytes(damaged))])

    assert pipeline._bad_frames >= 1


# --- per-station isolation -------------------------------------------------


async def test_each_station_gets_its_own_pipeline() -> None:
    """§8: two stations relaying one vehicle are independent observations.

    A shared accumulator would blend two links into a state neither saw.
    """
    resolver, writer, publisher = FakeResolver(), FakeWriter(), FakePublisher()
    pipelines = StationPipelines(
        resolver=cast(Any, resolver),
        writer=cast(Any, writer),
        publisher=cast(Any, publisher),
    )

    await pipelines.process("alpha", EPOCH, [record(0, heartbeat())])
    await pipelines.process("bravo", EPOCH, [record(0, position())])

    assert set(pipelines.pipelines) == {"alpha", "bravo"}
    # bravo never saw the heartbeat, so its row carries no mode.
    assert len(writer.written) == 1
    assert writer.written[0].station_id == "bravo"
    assert writer.written[0].mode is None


async def test_the_same_station_reuses_its_pipeline() -> None:
    """State accumulates across batches; a new pipeline per batch would lose
    everything learned from the messages before it."""
    resolver, writer, publisher = FakeResolver(), FakeWriter(), FakePublisher()
    pipelines = StationPipelines(
        resolver=cast(Any, resolver),
        writer=cast(Any, writer),
        publisher=cast(Any, publisher),
    )

    await pipelines.process(STATION, EPOCH, [record(0, heartbeat())])
    await pipelines.process(STATION, EPOCH, [record(1, position())])

    assert len(pipelines.pipelines) == 1
    assert writer.written[0].mode == "GUIDED"


# --- the registry label travels with the row -------------------------------
#
# The console listed ten aircraft as `9e1e607e`, `ba7f9168`, `685def77` and so
# on. Every one of them was correct and none of them was usable: with ten SITL
# vehicles in a row on the map there was no way to tell which entry was which
# airframe, which is also how a heading was compared against the wrong vehicle.


async def test_the_label_is_published_with_the_row() -> None:
    pipeline, _, _, publisher = build()

    await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert publisher.labels == {DRONE: "SITL-01"}


async def test_a_row_is_still_published_when_the_label_is_unknown() -> None:
    """An unregistered label must cost the name, not the position."""
    pipeline, _, _, publisher = build(resolver=FakeResolver(labels={}))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert publisher.rows == rows
    assert publisher.labels == {}


async def test_a_row_is_still_published_when_the_label_lookup_fails() -> None:
    """The paired failure test: the registry falling over is cosmetic.

    Written because the first version of this change put the lookup inside the
    handler that already wrapped the write and the publish, so a resolver
    without the method took the telemetry down with it - live positions lost to
    a missing display name.
    """
    pipeline, _, writer, publisher = build(resolver=FakeResolver(labels_fail=True))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert rows
    assert writer.written == rows
    assert publisher.rows == rows
    assert publisher.labels == {}
