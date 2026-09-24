"""What happens to a record after the transport has stored it.

`docs/specs/p1-02-gateway-ingest.md` §4 obligation 9: **parse MAVLink only
after all of the above.** By the time anything here runs, the datagram is in
the archive, the index is written and the relay has been told it can delete its
copy. So a failure in this module loses a derived view, never the flight
record — and that asymmetry is why it catches its own errors rather than
letting them propagate back into the transport and stall a station.

The order is fixed and each step depends on the one before:

    parse      bytes -> MAVLink messages          (gateway/parsing.py)
    classify   is this a vehicle?                 (gateway/classify.py)
    resolve    which drone, at the record's time? (gateway/binding.py)
    assemble   fold into a drone_state row        (gateway/drone_state.py)
    write      into the hypertable                (gateway/state_writer.py)
    publish    onto the bus for the console       (gateway/publisher.py)

`resolve` uses the **record's** timestamp, not now. A replayed backlog crossing
a SYSID reassignment resolves each record against the binding in force when it
was captured, which is the whole reason bindings have validity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common import get_logger
from gateway.binding import BindingResolver, Resolution
from gateway.classify import SourceKind, SourceRegistry
from gateway.drone_state import (
    DroneStateRow,
    StateAssembler,
    timestamp_from_recv_utc_ns,
)
from gateway.parsing import SourceId, parse_datagram
from gateway.publisher import TelemetryPublisher
from gateway.relay_records import Record
from gateway.state_writer import DroneStateWriter

_log = get_logger(__name__)


@dataclass
class IngestPipeline:
    """Turns stored records into rows, per station.

    One instance per station: the registry and the assembler are both
    per-station state, and §8 accepts two stations relaying one vehicle as
    independent observations.
    """

    station_id: str
    resolver: BindingResolver
    writer: DroneStateWriter
    publisher: TelemetryPublisher

    registry: SourceRegistry = field(init=False)
    assembler: StateAssembler = field(init=False)

    # Unclaimed sources are announced once per address, not once per datagram.
    # An aircraft transmitting at 84 Hz with no binding would otherwise emit
    # 84 identical events a second, which is how a genuinely useful signal
    # becomes something operators filter out.
    _announced_unclaimed: set[SourceId] = field(default_factory=set, init=False)
    _bad_frames: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.registry = SourceRegistry(station_id=self.station_id)
        self.assembler = StateAssembler(station_id=self.station_id)

    async def process(self, epoch: str, records: list[Record]) -> list[DroneStateRow]:
        """Convert a batch of stored records. Never raises into the transport."""
        rows: list[DroneStateRow] = []
        for record in records:
            try:
                rows.extend(await self._process_record(epoch, record))
            except Exception as error:
                _log.error(
                    "could not convert a record",
                    extra={
                        "station_id": self.station_id,
                        "epoch": epoch,
                        "seq": record.seq,
                        "error": repr(error),
                    },
                )

        if rows:
            try:
                await self.writer.write(rows)
                await self.publisher.publish_rows(rows)
            except Exception as error:
                _log.error(
                    "could not write drone_state",
                    extra={"station_id": self.station_id, "error": repr(error)},
                )
        return rows

    async def _process_record(self, epoch: str, record: Record) -> list[DroneStateRow]:
        parsed = parse_datagram(record.datagram)
        self._bad_frames += parsed.bad_frame_count

        # The record's own capture time. Used for the binding lookup and for
        # the row, so a row's identity and its place in the flight come from
        # one clock.
        ts = timestamp_from_recv_utc_ns(record.recv_utc_ns)

        rows: list[DroneStateRow] = []
        for message in parsed.messages:
            source = self.registry.observe(message)
            resolution = await self.resolver.resolve(self.station_id, source, at=ts)

            if resolution.drone_id is None:
                await self._announce_unclaimed(epoch, source.source_id, resolution)
                # Archived already, and deliberately not written to
                # drone_state: §7 forbids auto-registration, so an unbound
                # aircraft stays visible and unwritten until someone binds it.
                continue

            row = self.assembler.observe(
                source, message, drone_id=resolution.drone_id, ts=ts
            )
            if row is not None:
                rows.append(row)
        return rows

    async def _announce_unclaimed(
        self, epoch: str, source_id: SourceId, resolution: Resolution
    ) -> None:
        # A GCS or a gimbal is not an unclaimed aircraft, it is a source that
        # was never going to be one. Announcing those would bury the case that
        # matters under QGroundControl's own heartbeat.
        source = self.registry.sources.get(source_id)
        if source is not None and source.kind in {
            SourceKind.GCS,
            SourceKind.COMPONENT,
        }:
            return

        if source_id in self._announced_unclaimed:
            return
        self._announced_unclaimed.add(source_id)

        _log.warning(
            "unclaimed source",
            extra={
                "station_id": self.station_id,
                "sysid": source_id.sysid,
                "compid": source_id.compid,
                "reason": resolution.unclaimed_reason,
            },
        )
        await self.resolver.record_unclaimed(self.station_id, epoch, resolution)
        await self.publisher.publish_unclaimed(self.station_id, resolution, source_id)

    def forget_unclaimed(self, source_id: SourceId) -> None:
        """Allow an address to be announced again.

        Called when a binding appears, so that if it is later removed the
        operator is told again rather than the silence being mistaken for
        everything being fine.
        """
        self._announced_unclaimed.discard(source_id)


@dataclass
class StationPipelines:
    """One `IngestPipeline` per station, created on first sight.

    This is what the relay-v1 server calls. Pipelines are per station because
    the source registry and the state accumulator both are: spec §8 accepts
    two stations relaying one vehicle, and they are independent observations,
    not a single stream to be merged.
    """

    resolver: BindingResolver
    writer: DroneStateWriter
    publisher: TelemetryPublisher

    pipelines: dict[str, IngestPipeline] = field(default_factory=dict)

    def for_station(self, station_id: str) -> IngestPipeline:
        pipeline = self.pipelines.get(station_id)
        if pipeline is None:
            pipeline = IngestPipeline(
                station_id=station_id,
                resolver=self.resolver,
                writer=self.writer,
                publisher=self.publisher,
            )
            self.pipelines[station_id] = pipeline
        return pipeline

    async def process(self, station_id: str, epoch: str, records: list[Record]) -> None:
        await self.for_station(station_id).process(epoch, records)
