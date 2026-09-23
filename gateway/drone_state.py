"""Assembling `drone_state` rows from converted MAVLink messages.

One vehicle's state is spread across several messages arriving at different
rates, so a row is built by *folding* messages into a per-vehicle accumulator
and emitting what it holds. Two rules shape this, both from
`docs/specs/p1-02-gateway-ingest.md` §6.4:

**Nothing may depend on a message arriving at a particular rate.** What arrives
is decided by QGC's `SR*_` settings and by which screen the pilot has open. So
a field nobody has sent is `None`, not a default and not an error, and a row is
emitted on position - the message that defines where the aircraft is - carrying
whatever else is currently known.

**A missing value is a missing value.** `None` reaches the database as `NULL`,
which is a different fact from zero. `battery_remaining = -1` means the
autopilot does not estimate it; storing 0% would feed a failsafe decision a
number nobody measured.

## Timestamps

`ts` is the record's own capture time, taken from the relay's `recv_utc_ns`,
never the moment of ingest. A backlog replayed after an outage belongs where it
happened. That is the same timestamp the binding is resolved at, so a row's
identity and its position in the flight both come from one clock.

relay-v1 §9 warns that clock is the ground station's and may be wrong. Nothing
here corrects it; §12 question 4 owns that, and doing it invisibly at this
layer would make the correction impossible to audit later.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.classify import Source
from gateway.conversion import (
    air_data_from_vfr_hud,
    battery_from_battery_status,
    battery_from_sys_status,
    gps_from_gps_raw_int,
    position_from_global_position_int,
)
from gateway.parsing import ParsedMessage, SourceId

NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class DroneStateRow:
    """One row of `drone_state`, in SI, ready to insert.

    Every measurement is optional because every one of them can legitimately
    be unknown. `drone_id` and `ts` are not: a row with no identity or no time
    is not a degraded observation but an unusable one.
    """

    drone_id: UUID
    ts: datetime
    station_id: str

    lat_deg: float | None = None
    lon_deg: float | None = None
    alt_amsl_m: float | None = None
    # Above HOME, not above ground. There is no alt_agl_m anywhere in this
    # system: nothing in the telemetry carries one, and renaming this field
    # would be the silent error P5-00 exists to remove.
    alt_above_home_m: float | None = None
    heading_deg: float | None = None
    vx_ms: float | None = None
    vy_ms: float | None = None
    vz_ms: float | None = None
    batt_pct: float | None = None
    batt_voltage_v: float | None = None
    batt_consumed_wh: float | None = None
    mode: str | None = None
    armed: bool | None = None
    gps_fix_type: int | None = None
    sat_count: int | None = None
    groundspeed_ms: float | None = None
    climb_ms: float | None = None

    @property
    def has_position(self) -> bool:
        return self.lat_deg is not None and self.lon_deg is not None


@dataclass
class VehicleAccumulator:
    """What is currently known about one vehicle, from any message.

    Held per `(station_id, source)` rather than per drone: §8 accepts two
    stations relaying one vehicle, and their observations are independent. A
    shared accumulator would blend two links into a state neither of them saw.
    """

    station_id: str
    source_id: SourceId

    lat_deg: float | None = None
    lon_deg: float | None = None
    alt_amsl_m: float | None = None
    alt_above_home_m: float | None = None
    heading_deg: float | None = None
    vx_ms: float | None = None
    vy_ms: float | None = None
    vz_ms: float | None = None
    batt_pct: float | None = None
    batt_voltage_v: float | None = None
    batt_consumed_wh: float | None = None
    mode: str | None = None
    armed: bool | None = None
    gps_fix_type: int | None = None
    sat_count: int | None = None
    groundspeed_ms: float | None = None
    climb_ms: float | None = None

    def apply(self, message: ParsedMessage) -> bool:
        """Fold one message in. Returns whether a row should be emitted.

        A row is emitted on GLOBAL_POSITION_INT and nothing else. Every other
        message updates the accumulator and waits: emitting on each would
        produce a row per message type at whatever rate QGC happens to be
        sending, which is the rate dependence §6.4 forbids, and would fill the
        hypertable with rows that differ in one field.
        """
        handler = _HANDLERS.get(message.name)
        if handler is None:
            return False
        return bool(handler(self, message.payload))

    def to_row(self, drone_id: UUID, ts: datetime) -> DroneStateRow:
        return DroneStateRow(
            drone_id=drone_id,
            ts=ts,
            station_id=self.station_id,
            lat_deg=self.lat_deg,
            lon_deg=self.lon_deg,
            alt_amsl_m=self.alt_amsl_m,
            alt_above_home_m=self.alt_above_home_m,
            heading_deg=self.heading_deg,
            vx_ms=self.vx_ms,
            vy_ms=self.vy_ms,
            vz_ms=self.vz_ms,
            batt_pct=self.batt_pct,
            batt_voltage_v=self.batt_voltage_v,
            batt_consumed_wh=self.batt_consumed_wh,
            mode=self.mode,
            armed=self.armed,
            gps_fix_type=self.gps_fix_type,
            sat_count=self.sat_count,
            groundspeed_ms=self.groundspeed_ms,
            climb_ms=self.climb_ms,
        )


def _apply_position(accumulator: VehicleAccumulator, payload: Any) -> bool:
    position = position_from_global_position_int(payload)
    accumulator.lat_deg = position.lat_deg
    accumulator.lon_deg = position.lon_deg
    accumulator.alt_amsl_m = position.alt_amsl_m
    accumulator.alt_above_home_m = position.alt_above_home_m
    # Only overwrite a known heading with another known one. GLOBAL_POSITION_INT
    # sends UINT16_MAX when the heading is unknown, and letting that erase a
    # good heading from VFR_HUD would make the value flicker at whatever rate
    # the two messages happen to interleave.
    if position.heading_deg is not None:
        accumulator.heading_deg = position.heading_deg
    accumulator.vx_ms = position.vx_ms
    accumulator.vy_ms = position.vy_ms
    accumulator.vz_ms = position.vz_ms
    return True


def _apply_sys_status(accumulator: VehicleAccumulator, payload: Any) -> bool:
    battery = battery_from_sys_status(payload)
    # SYS_STATUS carries no energy, so it must not clear one BATTERY_STATUS
    # has already supplied.
    if battery.remaining_pct is not None:
        accumulator.batt_pct = battery.remaining_pct
    if battery.voltage_v is not None:
        accumulator.batt_voltage_v = battery.voltage_v
    return False


def _apply_battery_status(accumulator: VehicleAccumulator, payload: Any) -> bool:
    battery = battery_from_battery_status(payload)
    if battery.remaining_pct is not None:
        accumulator.batt_pct = battery.remaining_pct
    if battery.voltage_v is not None:
        accumulator.batt_voltage_v = battery.voltage_v
    if battery.consumed_wh is not None:
        accumulator.batt_consumed_wh = battery.consumed_wh
    return False


def _apply_gps(accumulator: VehicleAccumulator, payload: Any) -> bool:
    gps = gps_from_gps_raw_int(payload)
    accumulator.gps_fix_type = gps.fix_type
    if gps.satellites_visible is not None:
        accumulator.sat_count = gps.satellites_visible
    return False


def _apply_vfr_hud(accumulator: VehicleAccumulator, payload: Any) -> bool:
    air = air_data_from_vfr_hud(payload)
    if air.groundspeed_ms is not None:
        accumulator.groundspeed_ms = air.groundspeed_ms
    if air.climb_ms is not None:
        accumulator.climb_ms = air.climb_ms
    if air.heading_deg is not None:
        accumulator.heading_deg = air.heading_deg
    return False


def _apply_heartbeat(accumulator: VehicleAccumulator, payload: Any) -> bool:
    accumulator.mode = flight_mode_name(payload)
    accumulator.armed = is_armed(payload)
    return False


class _Handler(Protocol):
    """Folds one message type in, and says whether a row is now due."""

    def __call__(self, accumulator: VehicleAccumulator, payload: Any) -> bool: ...


_HANDLERS: dict[str, _Handler] = {
    "GLOBAL_POSITION_INT": _apply_position,
    "SYS_STATUS": _apply_sys_status,
    "BATTERY_STATUS": _apply_battery_status,
    "GPS_RAW_INT": _apply_gps,
    "VFR_HUD": _apply_vfr_hud,
    "HEARTBEAT": _apply_heartbeat,
}


def is_armed(heartbeat: Any) -> bool:
    """Whether the vehicle reports itself armed.

    The flag is read from pymavlink's own enum rather than written as 128.
    """
    return bool(int(heartbeat.base_mode) & int(mavlink.MAV_MODE_FLAG_SAFETY_ARMED))


def flight_mode_name(heartbeat: Any) -> str | None:
    """The flight mode, as ArduPilot names it.

    `custom_mode` is autopilot-specific, and pymavlink ships the mapping, so it
    is looked up rather than transcribed. An unmapped value returns its number
    as a string rather than None: an unrecognised mode is information, and
    discarding it would hide a firmware the fleet has not seen before.
    """
    if not int(heartbeat.base_mode) & 0x01:  # CUSTOM_MODE_ENABLED
        return None
    custom_mode = int(heartbeat.custom_mode)
    mapping = mavutil.mode_mapping_bynumber(int(heartbeat.type))
    if mapping is None:
        return str(custom_mode)
    return str(mapping.get(custom_mode, custom_mode))


def timestamp_from_recv_utc_ns(recv_utc_ns: int) -> datetime:
    """The record's capture time, as the relay recorded it.

    Not the ingest time. relay-v1 §9 says this clock is the ground station's
    and may be wrong; it is stored as sent, and correcting it is §12 question 4.
    """
    return datetime.fromtimestamp(recv_utc_ns / NANOSECONDS_PER_SECOND, tz=UTC)


@dataclass
class StateAssembler:
    """Folds a station's messages into `drone_state` rows."""

    station_id: str
    accumulators: dict[SourceId, VehicleAccumulator] = field(default_factory=dict)

    def observe(
        self, source: Source, message: ParsedMessage, *, drone_id: UUID, ts: datetime
    ) -> DroneStateRow | None:
        """Fold a message in, returning a row when one is due.

        `drone_id` is resolved by the caller through `BindingResolver` at `ts`,
        never here: an unbound or unclassified source must not reach this
        function at all, and taking the id as an argument makes that the
        caller's visible responsibility rather than a check hidden inside.
        """
        accumulator = self.accumulators.setdefault(
            source.source_id,
            VehicleAccumulator(station_id=self.station_id, source_id=source.source_id),
        )
        if not accumulator.apply(message):
            return None
        row = accumulator.to_row(drone_id, ts)
        if not row.has_position:  # pragma: no cover - position implies a row
            return None
        return row

    def snapshot(
        self, source_id: SourceId, *, drone_id: UUID, ts: datetime
    ) -> DroneStateRow | None:
        """The current accumulated state, without waiting for a position.

        For the console, which wants to show an aircraft that has sent a
        heartbeat and a battery reading but no position yet. Deliberately not
        used for writing rows: a `drone_state` row with no position is a row
        whose only purpose is to say the aircraft existed.
        """
        accumulator = self.accumulators.get(source_id)
        if accumulator is None:
            return None
        return replace(accumulator.to_row(drone_id, ts))
