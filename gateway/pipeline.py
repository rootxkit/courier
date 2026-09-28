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
from datetime import datetime
from uuid import UUID

from common import get_logger
from gateway.binding import BindingResolver, Resolution
from gateway.classify import Source, SourceKind, SourceRegistry
from gateway.drone_state import (
    DroneStateRow,
    StateAssembler,
    timestamp_from_recv_utc_ns,
)
from gateway.live_state import LiveState
from gateway.parsing import ParsedMessage, SourceId, parse_datagram
from gateway.publisher import TelemetryPublisher
from gateway.rate_limit import RateLimiter
from gateway.relay_records import Record
from gateway.stage_timing import StageTimings, shared_timings
from gateway.state_writer import DroneStateWriter

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Observed:
    """One parsed message, with its record's seq and capture time, and its
    source as classified at that message."""

    seq: int
    ts: datetime
    message: ParsedMessage
    source: Source


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
    timings: StageTimings = field(default_factory=shared_timings)
    # P1-07: sources refused by station policy are reported at most once per
    # address per interval, with a count of what was suppressed in between.
    rejections: RateLimiter = field(default_factory=RateLimiter)
    # P1-05. Optional so the pipeline runs, and is tested, without Redis.
    live_state: LiveState | None = None

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
        """Convert a batch of stored records. Never raises into the transport.

        Three passes, so bindings are read once per batch rather than once per
        message (P1-13). Per message, the resolver cost one database query:
        about 3.3 ms, which capped the Gateway at about 300 records/s at any
        fleet size and was 96% of the time spent storing a batch (ADR-002).

        Nothing about *which* binding applies changes. Each message is still
        resolved against its own record's capture time, with the source as it
        was classified at that message - `Source` is a frozen snapshot, so
        classifying the whole batch first cannot leak a later HEARTBEAT back
        into an earlier message's resolution.
        """
        observed = self._observe(epoch, records)
        if not observed:
            return []

        try:
            with self.timings.measure("process.resolve"):
                resolutions = await self.resolver.resolve_batch(
                    self.station_id,
                    [(item.source, item.ts) for item in observed],
                )
        except Exception as error:
            # The flight record is already durable (obligation 9); what is lost
            # is this batch's derived view. Logged per batch, because a failed
            # binding read fails every message in it the same way.
            _log.error(
                "could not resolve a batch",
                extra={
                    "station_id": self.station_id,
                    "epoch": epoch,
                    "first_seq": observed[0].seq,
                    "last_seq": observed[-1].seq,
                    "error": repr(error),
                },
            )
            return []

        rows: list[DroneStateRow] = []
        for item, resolution in zip(observed, resolutions, strict=True):
            try:
                row = await self._assemble(epoch, item, resolution)
            except Exception as error:
                _log.error(
                    "could not convert a record",
                    extra={
                        "station_id": self.station_id,
                        "epoch": epoch,
                        "seq": item.seq,
                        "error": repr(error),
                    },
                )
                continue
            if row is not None:
                rows.append(row)

        if rows:
            labels: dict[UUID, str] = {}
            try:
                with self.timings.measure("process.write"):
                    await self.writer.write(rows)
                with self.timings.measure("process.labels"):
                    labels = await self._labels(rows)
                with self.timings.measure("process.publish"):
                    await self.publisher.publish_rows(rows, labels)
            except Exception as error:
                _log.error(
                    "could not write drone_state",
                    extra={"station_id": self.station_id, "error": repr(error)},
                )
            # Its own handler, after the write rather than inside it. Live
            # state is derived from the same rows but is not downstream of the
            # hypertable: a failed insert must not also make every drone look
            # link-lost, and a Redis failure must not cost the insert.
            if self.live_state is not None:
                try:
                    with self.timings.measure("process.live_state"):
                        await self.live_state.update(rows, labels)
                except Exception as error:
                    _log.error(
                        "could not update live state",
                        extra={"station_id": self.station_id, "error": repr(error)},
                    )
        return rows

    async def _labels(self, rows: list[DroneStateRow]) -> dict[UUID, str]:
        """Registry names for the console, and never a reason to lose a row.

        Isolated in its own handler rather than sharing the one around the
        write and the publish. A label is decoration on a position; if looking
        one up fails, the position must still be written and still reach the
        console unnamed. Sharing the handler would have made a registry
        hiccup cost live telemetry, which is the wrong trade by a wide margin.
        """
        try:
            return await self.resolver.labels_for({row.drone_id for row in rows})
        except Exception as error:
            _log.warning(
                "could not read drone labels",
                extra={"station_id": self.station_id, "error": repr(error)},
            )
            return {}

    def _observe(self, epoch: str, records: list[Record]) -> list[_Observed]:
        """Parse and classify every message, in order, with its capture time."""
        observed: list[_Observed] = []
        for record in records:
            try:
                with self.timings.measure("process.parse"):
                    parsed = parse_datagram(record.datagram)
                self._bad_frames += parsed.bad_frame_count

                # The record's own capture time. Used for the binding lookup
                # and for the row, so a row's identity and its place in the
                # flight come from one clock.
                ts = timestamp_from_recv_utc_ns(record.recv_utc_ns)
                for message in parsed.messages:
                    source = self.registry.observe(message)
                    observed.append(_Observed(record.seq, ts, message, source))
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
        return observed

    async def _assemble(
        self, epoch: str, item: _Observed, resolution: Resolution
    ) -> DroneStateRow | None:
        if resolution.is_rejected:
            await self._report_rejected(epoch, resolution)
            return None

        if resolution.drone_id is None:
            await self._announce_unclaimed(epoch, item.source, resolution)
            # Archived already, and deliberately not written to drone_state:
            # §7 forbids auto-registration, so an unbound aircraft stays
            # visible and unwritten until someone binds it.
            return None

        return self.assembler.observe(
            item.source, item.message, drone_id=resolution.drone_id, ts=item.ts
        )

    async def _announce_unclaimed(
        self, epoch: str, source: Source, resolution: Resolution
    ) -> None:
        # A GCS or a gimbal is not an unclaimed aircraft, it is a source that
        # was never going to be one. Announcing those would bury the case that
        # matters under QGroundControl's own heartbeat.
        #
        # Judged on the source as it was at this message, not as the registry
        # holds it now. The registry has already seen the whole batch, so a
        # HEARTBEAT later in it would otherwise decide for an earlier message.
        if source.kind in {SourceKind.GCS, SourceKind.COMPONENT}:
            return
        source_id = source.source_id

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

    async def _report_rejected(self, epoch: str, resolution: Resolution) -> None:
        """A station presented an address bound on another station (P1-07).

        Not announced once like an unclaimed source: a refusal that goes quiet
        after its first report reads as a refusal that stopped. Reported at
        most once per address per interval instead, carrying the count
        suppressed since the last report, so it neither floods nor falls
        silent. The records themselves are already archived.
        """
        source_id = resolution.source_id
        suppressed = self.rejections.admit(source_id)
        if suppressed is None:
            return
        _log.warning(
            "rejected source: not assigned to this station",
            extra={
                "station_id": self.station_id,
                "sysid": source_id.sysid,
                "compid": source_id.compid,
                "suppressed": suppressed,
            },
        )
        await self.resolver.record_unclaimed(
            self.station_id, epoch, resolution, suppressed=suppressed
        )
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
    live_state: LiveState | None = None

    pipelines: dict[str, IngestPipeline] = field(default_factory=dict)

    def for_station(self, station_id: str) -> IngestPipeline:
        pipeline = self.pipelines.get(station_id)
        if pipeline is None:
            pipeline = IngestPipeline(
                station_id=station_id,
                resolver=self.resolver,
                writer=self.writer,
                publisher=self.publisher,
                live_state=self.live_state,
            )
            self.pipelines[station_id] = pipeline
        return pipeline

    async def process(self, station_id: str, epoch: str, records: list[Record]) -> None:
        await self.for_station(station_id).process(epoch, records)
