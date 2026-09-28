"""Binding a MAVLink address to a fleet identity, as of a record's timestamp.

`docs/specs/p1-02-gateway-ingest.md` §7. Two rules do all the work:

**`drone_state` stores a `drone_id`, never a SYSID.** An address is set by a
parameter and reused across airframes; two aircraft that never fly together may
share one for years.

**Resolution is at the record's timestamp, never at ingest time.** This is not
a refinement, it is the whole reason the binding has validity at all. A relay
that was offline for two hours replays its backlog afterwards, and resolving
that backlog against the binding in force *now* would attribute an hour of one
airframe's flight to whichever drone holds the address today. The relay makes
that case ordinary rather than exotic - it is the design working.

## Where the guarantees live

Non-overlap is a database exclusion constraint, not a check in this module.
Two concurrent writers would both look, both find nothing, and both insert; the
constraint refuses the second one regardless. A binding to a drone that does
not exist is a foreign-key violation for the same reason.

## Bounds

`tstzrange` defaults to `[lower, upper)`: inclusive lower, exclusive upper. A
record at the exact instant a binding ends belongs to the **new** binding. The
choice is arbitrary; making it once and pinning it is not, because the
alternatives are one record attributed to two airframes, or to none.

Resolution is done here in Python against ranges read from the database, rather
than one `@>` query per record - a replayed backlog is tens of thousands of
records and each would otherwise be a round trip. `test_binding.py` pins the
Python arithmetic against PostgreSQL's own `@>` operator at the boundary
instant, so the duplication cannot drift.

## Bindings are also the station policy (P1-07)

A station authenticates as itself and relays whatever its radio hears, so
which aircraft it may carry is server policy (relay-v1 §3). That policy *is*
`source_bindings`: a station may carry exactly the addresses bound on it, and
a handover between stations is the same drone bound on both (spec §8).

That gives an address three outcomes at a record's timestamp, not two:

- bound on this station: resolved;
- bound on no station: **unclaimed**, the normal state of an aircraft being
  set up, announced once;
- bound on another station but not this one: **not assigned**, a rejection. It
  is what a spoofed SYSID or a misrouted station looks like, and unlike an
  unclaimed source it is reported for as long as it continues.

Either way nothing is discarded: the records are already archived, and only
attribution to a drone is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway.classify import Source, SourceKind
from gateway.ingest_store import StoreError
from gateway.parsing import SourceId

_log = get_logger(__name__)

_drones = sa.table(
    "known_drones",
    sa.column("drone_id"),
    sa.column("label", sa.Text),
    sa.column("registered_at", sa.DateTime(timezone=True)),
    sa.column("retired_at", sa.DateTime(timezone=True)),
)

_bindings = sa.table(
    "source_bindings",
    sa.column("id", sa.BigInteger),
    sa.column("station_id", sa.Text),
    sa.column("sysid", sa.Integer),
    sa.column("compid", sa.Integer),
    sa.column("drone_id"),
    sa.column("valid"),
    sa.column("created_by", sa.Text),
    sa.column("note", sa.Text),
)

_events = sa.table(
    "ingest_events",
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("event_type", sa.Text),
    sa.column("payload", sa.JSON),
)


class BindingConflictError(StoreError):
    """The database refused a binding.

    Either it overlaps an existing one for the same address, or it names a
    drone the telemetry database has never been told about. Both are refusals
    by constraint, which is the point: neither can be a silent skip.
    """


@dataclass(frozen=True, slots=True)
class Binding:
    """One address bound to one drone, over one interval."""

    station_id: str
    source_id: SourceId
    drone_id: UUID
    bound_from: datetime
    # None means open-ended: still in force.
    bound_until: datetime | None

    def covers(self, moment: datetime) -> bool:
        """Whether this binding is the one in force at `moment`.

        `[bound_from, bound_until)` - inclusive lower, exclusive upper, matching
        the `tstzrange` this was read from. Pinned against PostgreSQL's own
        containment operator by test, because reimplementing a range check is
        exactly the kind of off-by-one that returns a plausible answer.
        """
        if moment < self.bound_from:
            return False
        return self.bound_until is None or moment < self.bound_until


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a source resolved to, and why it did not if it did not."""

    source_id: SourceId
    drone_id: UUID | None
    # Set when `drone_id` is None. The console renders these as unclaimed
    # sources, which §7 requires be visible rather than silently dropped.
    unclaimed_reason: str = ""

    @property
    def is_claimed(self) -> bool:
        return self.drone_id is not None

    @property
    def is_rejected(self) -> bool:
        """Refused by station policy, as opposed to merely unclaimed."""
        return self.unclaimed_reason == NOT_ASSIGNED


UNBOUND: str = "no binding covers this address at the record's timestamp"
NOT_ASSIGNED: str = (
    "the address is bound on another station, not on the station presenting it"
)
NOT_A_VEHICLE: str = "the source is not classified as a vehicle"
UNCLASSIFIED: str = "the source has never sent a HEARTBEAT"


@dataclass
class BindingResolver:
    """Resolves addresses to drones, at the timestamp of each record."""

    engine: AsyncEngine

    def __post_init__(self) -> None:
        # Registry labels, read once per drone. See `labels_for`.
        self._labels: dict[UUID, str] = {}

    async def register_drone(
        self, drone_id: UUID, label: str, *, retired_at: datetime | None = None
    ) -> None:
        """Record that the telemetry database may attribute telemetry here.

        A projection of the relational fleet registry, which the Gateway
        cannot reach. Keeping the two in step is the API's job.
        """
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    pg_insert(_drones)
                    .values(drone_id=drone_id, label=label, retired_at=retired_at)
                    .on_conflict_do_update(
                        index_elements=["drone_id"],
                        set_={"label": label, "retired_at": retired_at},
                    )
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not register drone: {error}") from error

    async def labels_for(self, drone_ids: set[UUID]) -> dict[UUID, str]:
        """The registry label for each drone, for whatever has to name one.

        Nothing else in this module needs a label: a `drone_id` is the identity
        and a label is a convenience for people. But a console that can only
        show `0b63df96` cannot tell a pilot which aircraft that is, and the
        pilot is who the console exists for.

        Cached in process and never invalidated, deliberately. A label is
        registry data that changes when someone renames an airframe, not
        telemetry, and looking it up per row at 4 Hz per drone would put a
        query on the hot path to save a restart after a rename. Keeping the
        projection in step with the relational registry is P2-05's job and is
        already recorded as a gap; this inherits that limitation rather than
        inventing a second, worse answer to it.
        """
        missing = drone_ids - self._labels.keys()
        if missing:
            try:
                async with self.engine.connect() as connection:
                    result = await connection.execute(
                        sa.select(_drones.c.drone_id, _drones.c.label).where(
                            _drones.c.drone_id.in_(missing)
                        )
                    )
                    for drone_id, label in result:
                        self._labels[drone_id] = label
            except SQLAlchemyError as error:
                # A missing label is a cosmetic failure. It must never stop a
                # row being published, because the position is what matters
                # and the name is what makes it readable.
                _log.warning("could not read drone labels", extra={"error": str(error)})
        return {
            drone_id: self._labels[drone_id]
            for drone_id in drone_ids
            if drone_id in self._labels
        }

    async def bind(
        self,
        station_id: str,
        source_id: SourceId,
        drone_id: UUID,
        *,
        bound_from: datetime,
        bound_until: datetime | None = None,
        created_by: str,
        note: str = "",
    ) -> None:
        """Bind an address to a drone over an interval.

        Overlap and unknown drones are refused by the database. This method
        does not check for either first: a check would be a race, and a race
        that usually wins is worse than no check because it looks like one.
        """
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "INSERT INTO source_bindings "
                        "(station_id, sysid, compid, drone_id, valid, "
                        " created_by, note) "
                        "VALUES (:station_id, :sysid, :compid, :drone_id, "
                        " tstzrange(:lower, :upper, '[)'), :created_by, :note)"
                    ),
                    {
                        "station_id": station_id,
                        "sysid": source_id.sysid,
                        "compid": source_id.compid,
                        "drone_id": str(drone_id),
                        "lower": bound_from,
                        "upper": bound_until,
                        "created_by": created_by,
                        "note": note,
                    },
                )
        except IntegrityError as error:
            raise BindingConflictError(
                f"refused binding {source_id} -> {drone_id} on {station_id}: "
                f"{_explain(error)}"
            ) from error
        except SQLAlchemyError as error:
            raise StoreError(f"could not bind: {error}") from error

    async def close_binding(
        self, station_id: str, source_id: SourceId, *, at: datetime
    ) -> int:
        """End the open binding for an address.

        §7: reassigning a SYSID closes one binding and opens another. Closing
        first is what makes the next `bind` legal - the exclusion constraint
        refuses an overlap, so an open-ended binding blocks every successor
        until it is closed.
        """
        try:
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    sa.text(
                        "UPDATE source_bindings "
                        "SET valid = tstzrange(lower(valid), :at, '[)') "
                        "WHERE station_id = :station_id AND sysid = :sysid "
                        "  AND compid = :compid AND upper(valid) IS NULL "
                        "  AND lower(valid) < :at"
                    ),
                    {
                        "station_id": station_id,
                        "sysid": source_id.sysid,
                        "compid": source_id.compid,
                        "at": at,
                    },
                )
                return int(result.rowcount)
        except SQLAlchemyError as error:
            raise StoreError(f"could not close binding: {error}") from error

    async def close_bindings_for_drone(self, drone_id: UUID, *, at: datetime) -> int:
        """End every open binding that attributes telemetry to `drone_id`.

        P2-05: retiring an airframe. Its SYSID must stop being attributed to
        it from `at`, on every station, or the next aircraft to transmit as
        that address would fly under a retired identity.
        """
        try:
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    sa.text(
                        "UPDATE source_bindings "
                        "SET valid = tstzrange(lower(valid), :at, '[)') "
                        "WHERE drone_id = :drone_id AND upper(valid) IS NULL "
                        "  AND lower(valid) < :at"
                    ),
                    {"drone_id": str(drone_id), "at": at},
                )
                return int(result.rowcount)
        except SQLAlchemyError as error:
            raise StoreError(f"could not close bindings: {error}") from error

    async def bindings_for(self, station_id: str, source_id: SourceId) -> list[Binding]:
        """Every binding this address has ever had, oldest first."""
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        sa.text(
                            "SELECT drone_id, lower(valid) AS bound_from, "
                            "       upper(valid) AS bound_until "
                            "FROM source_bindings "
                            "WHERE station_id = :station_id AND sysid = :sysid "
                            "  AND compid = :compid "
                            "ORDER BY lower(valid)"
                        ),
                        {
                            "station_id": station_id,
                            "sysid": source_id.sysid,
                            "compid": source_id.compid,
                        },
                    )
                ).all()
        except SQLAlchemyError as error:
            raise StoreError(f"could not read bindings: {error}") from error

        return [
            Binding(
                station_id=station_id,
                source_id=source_id,
                drone_id=row.drone_id,
                bound_from=row.bound_from,
                bound_until=row.bound_until,
            )
            for row in rows
        ]

    async def _bindings_on_any_station(self, source_id: SourceId) -> list[Binding]:
        """Every binding this address has on any station, oldest first."""
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        sa.text(
                            "SELECT station_id, drone_id, lower(valid) AS bound_from, "
                            "       upper(valid) AS bound_until "
                            "FROM source_bindings "
                            "WHERE sysid = :sysid AND compid = :compid "
                            "ORDER BY lower(valid)"
                        ),
                        {"sysid": source_id.sysid, "compid": source_id.compid},
                    )
                ).all()
        except SQLAlchemyError as error:
            raise StoreError(f"could not read bindings: {error}") from error

        return [
            Binding(
                station_id=row.station_id,
                source_id=source_id,
                drone_id=row.drone_id,
                bound_from=row.bound_from,
                bound_until=row.bound_until,
            )
            for row in rows
        ]

    async def resolve(
        self, station_id: str, source: Source, *, at: datetime
    ) -> Resolution:
        """Resolve one source at one timestamp."""
        results = await self.resolve_batch(station_id, [(source, at)])
        return results[0]

    async def resolve_batch(
        self, station_id: str, records: list[tuple[Source, datetime]]
    ) -> list[Resolution]:
        """Resolve a batch, each record against its own timestamp.

        The batch is the case that matters. A backlog replayed after an outage
        can straddle a binding change, and every record in it must resolve
        against what was in force when it was *captured* - so a single batch
        can legitimately produce two different `drone_id`s for one address.
        Resolving the batch at one timestamp, any timestamp, would be wrong for
        at least half of it.
        """
        addresses = {
            source.source_id
            for source, _ in records
            if source.kind is SourceKind.VEHICLE
        }
        # One query per address, across every station. The other stations'
        # bindings are what distinguish "not assigned here" from "unclaimed",
        # and reading them separately would double the queries per batch.
        known: dict[SourceId, list[Binding]] = {}
        for address in addresses:
            known[address] = await self._bindings_on_any_station(address)

        resolved: list[Resolution] = []
        for source, moment in records:
            resolved.append(self._resolve_one(station_id, source, moment, known))
        return resolved

    def _resolve_one(
        self,
        station_id: str,
        source: Source,
        moment: datetime,
        known: dict[SourceId, list[Binding]],
    ) -> Resolution:
        if source.kind is SourceKind.UNCLASSIFIED:
            # No HEARTBEAT has arrived, so nothing has said what this is. §7:
            # archived and surfaced, never written to drone_state, and never
            # auto-registered - a misconfigured aircraft must not walk itself
            # into the fleet.
            return Resolution(source.source_id, None, UNCLASSIFIED)

        if source.kind is not SourceKind.VEHICLE:
            return Resolution(source.source_id, None, NOT_A_VEHICLE)

        in_force = [
            binding
            for binding in known.get(source.source_id, [])
            if binding.covers(moment)
        ]
        for binding in in_force:
            if binding.station_id == station_id:
                return Resolution(source.source_id, binding.drone_id)

        # Bound, but somewhere else, at this instant. Judged at the record's
        # timestamp like everything here: an address that was bound elsewhere
        # last week and is bound nowhere now is unclaimed, not rejected.
        if in_force:
            return Resolution(source.source_id, None, NOT_ASSIGNED)
        return Resolution(source.source_id, None, UNBOUND)

    async def record_unclaimed(
        self,
        station_id: str,
        epoch: str | None,
        resolution: Resolution,
        *,
        suppressed: int = 0,
    ) -> None:
        """Surface an unclaimed or rejected source as an event.

        §7: normal during setup and serious in flight, so it is an event and a
        console state rather than a log line nobody reads. A source refused by
        station policy (P1-07) is a different event type, because it is a
        different finding: not "nobody has said what this is" but "this
        station was never assigned it". `suppressed` is how many rejections
        were counted, not reported, since the previous event for the address.
        """
        payload: dict[str, object] = {
            "sysid": resolution.source_id.sysid,
            "compid": resolution.source_id.compid,
            "reason": resolution.unclaimed_reason,
        }
        if resolution.is_rejected:
            payload["suppressed"] = suppressed
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    sa.insert(_events).values(
                        station_id=station_id,
                        epoch=epoch,
                        event_type=(
                            "rejected_source"
                            if resolution.is_rejected
                            else "unclaimed_source"
                        ),
                        payload=payload,
                    )
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not record unclaimed source: {error}") from error


def _explain(error: IntegrityError) -> str:
    """Turn a constraint name into something readable at 3am."""
    detail = str(error.orig)
    if "source_bindings_no_overlap" in detail:
        return (
            "it overlaps an existing binding for the same address. Close the "
            "open binding first (§7: reassigning a SYSID closes one and opens "
            "another)"
        )
    if "source_bindings_drone_id_fkey" in detail:
        return (
            "the drone_id is not in known_drones. Register it first; the "
            "Gateway cannot reach the relational fleet registry"
        )
    return detail


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
