"""What a heartbeating source is, and which of its messages feed live state.

Two decisions, both from `docs/specs/p1-02-gateway-ingest.md` §6 and both made
from what a source *says it is* rather than from anything circumstantial.

## Classification, per `(sysid, compid)`

The relay forwards everything it hears, which includes QGroundControl's own
heartbeat and any component sharing the link. Three rules, each from a
specific failure:

- **Never classify on the SYSID number.** 255 for a ground station is a
  convention, and `GCS_SYSTEM_ID` is a user setting in `QGroundControl.ini`.
- **Never classify on message volume.** An aircraft that has just booted has
  sent one HEARTBEAT and nothing else, which is exactly the moment it must
  stay visible.
- **Ambiguity resolves to vehicle.** Registering a ground station as an
  aircraft is a nuisance. Failing to register an aircraft is the direction
  that gets someone hurt.

## Hot path versus archive

Per ADR-001. Roughly 80% of a real link's traffic is not on the hot path, and
**none of it is discarded**: `ATTITUDE`, `VIBRATION`, `ESC_TELEMETRY_1_TO_4`
and the rest are what an incident investigation reads, and P10-03 replay cannot
reconstruct what was never stored. The split is between *live state* and
*archive*, never between keep and drop.

Nothing here depends on a message arriving at any particular rate. What arrives
is decided by QGC's `SR*_` settings and by which screen the pilot has open
(§6.4). `MISSION_ITEM_REACHED` is event-driven and was not observed at all in
ADR-001's capture, because the aircraft was parked - so its absence is never a
fault.

**Every constant here is read from pymavlink by name.** No message id and no
enum value is written as a literal; CLAUDE.md's rule about offsets applies to
these too, and a wrong-but-plausible id would silently route a message to the
archive that the pipeline was waiting for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.parsing import ParsedMessage, SourceId


class SourceKind(StrEnum):
    """What a `(sysid, compid)` is. Only `VEHICLE` may become a drone."""

    VEHICLE = "vehicle"
    GCS = "gcs"
    COMPONENT = "component"
    # Seen, but has never sent a HEARTBEAT, so it has not said what it is.
    # Distinct from the three above: it is not a judgement, it is the absence
    # of one, and it must not be silently treated as a vehicle.
    UNCLASSIFIED = "unclassified"


def _mav_type(name: str) -> int:
    """Read a MAV_TYPE from pymavlink, failing loudly if it is not there."""
    return int(getattr(mavlink, f"MAV_TYPE_{name}"))


MAV_TYPE_GCS: Final = _mav_type("GCS")
MAV_AUTOPILOT_INVALID: Final = int(mavlink.MAV_AUTOPILOT_INVALID)

# Ground equipment and peripherals. Each emits its own HEARTBEAT, often under
# the vehicle's SYSID with a different component id.
NON_VEHICLE_TYPE_NAMES: Final = (
    "ANTENNA_TRACKER",
    "GCS",
    "ONBOARD_CONTROLLER",
    "GIMBAL",
    "ADSB",
    "CAMERA",
    "CHARGING_STATION",
    "FLARM",
    "SERVO",
    "ODID",
    "BATTERY",
    "PARACHUTE",
    "LOG",
    "OSD",
    "IMU",
    "GPS",
    "WINCH",
)

NON_VEHICLE_MAV_TYPES: Final = frozenset(
    _mav_type(name) for name in NON_VEHICLE_TYPE_NAMES
)


def _message_id(name: str) -> int:
    """Read a message id from pymavlink's own class, never from memory."""
    return int(getattr(mavlink, f"MAVLink_{name.lower()}_message").id)


# ADR-001's hot path: the messages that become `drone_state`.
HOT_PATH_MESSAGE_NAMES: Final = (
    "HEARTBEAT",
    "GLOBAL_POSITION_INT",
    "SYS_STATUS",
    "BATTERY_STATUS",
    "GPS_RAW_INT",
    "VFR_HUD",
    "EKF_STATUS_REPORT",
    "MISSION_CURRENT",
    "MISSION_ITEM_REACHED",
    "STATUSTEXT",
)

HOT_PATH_MESSAGE_IDS: Final = frozenset(
    _message_id(name) for name in HOT_PATH_MESSAGE_NAMES
)

HEARTBEAT_ID: Final = _message_id("HEARTBEAT")


def classify_heartbeat(mav_type: int, autopilot: int) -> SourceKind:
    """Decide what a source is, from the HEARTBEAT it sent.

    Lifted from `tools/mavlink_probe.py`, which has been run against a real
    link, with the constants derived from pymavlink rather than pinned as
    integers - the probe hardcodes them because its listen mode must run on a
    ground-station Python with nothing but the standard library, and the
    Gateway has no such constraint.
    """
    if mav_type == MAV_TYPE_GCS:
        return SourceKind.GCS
    # A component that is not an autopilot declares MAV_AUTOPILOT_INVALID.
    if mav_type in NON_VEHICLE_MAV_TYPES or autopilot == MAV_AUTOPILOT_INVALID:
        return SourceKind.COMPONENT
    return SourceKind.VEHICLE


def is_hot_path(message_id: int) -> bool:
    """Whether a message feeds live state.

    False means archive, never discard. The archive already holds every
    datagram either way; this decides what is additionally converted into
    `drone_state`.
    """
    return message_id in HOT_PATH_MESSAGE_IDS


@dataclass(frozen=True, slots=True)
class Source:
    """A `(sysid, compid)` and what it has told us about itself."""

    source_id: SourceId
    kind: SourceKind
    mav_type: int | None = None
    autopilot: int | None = None
    heartbeat_count: int = 0
    message_count: int = 0

    @property
    def is_vehicle(self) -> bool:
        return self.kind is SourceKind.VEHICLE


@dataclass
class SourceRegistry:
    """Everything one station has been heard to carry.

    Per station, because §5 evaluates policy on `(station_id, sysid)` and a
    station is never trusted to assert what it is carrying. Classification is
    what this produces; whether the station is *permitted* to carry that
    vehicle is a separate check, and binding a source to a `drone_id` is §7.
    """

    station_id: str
    sources: dict[SourceId, Source] = field(default_factory=dict)

    def observe(self, message: ParsedMessage) -> Source:
        """Fold one message in, and return the source's current state."""
        existing = self.sources.get(message.source)

        if message.message_id != HEARTBEAT_ID:
            updated = (
                Source(
                    source_id=message.source,
                    kind=SourceKind.UNCLASSIFIED,
                    message_count=1,
                )
                if existing is None
                else Source(
                    source_id=existing.source_id,
                    kind=existing.kind,
                    mav_type=existing.mav_type,
                    autopilot=existing.autopilot,
                    heartbeat_count=existing.heartbeat_count,
                    message_count=existing.message_count + 1,
                )
            )
            self.sources[message.source] = updated
            return updated

        mav_type = int(message.field("type"))
        autopilot = int(message.field("autopilot"))
        updated = Source(
            source_id=message.source,
            # Re-evaluated on every heartbeat rather than latched. A source
            # whose declared type changes has been reconfigured or replaced,
            # and continuing to treat it as what it used to be is how a
            # companion computer inherits an aircraft's identity.
            kind=classify_heartbeat(mav_type, autopilot),
            mav_type=mav_type,
            autopilot=autopilot,
            heartbeat_count=(existing.heartbeat_count + 1) if existing else 1,
            message_count=(existing.message_count + 1) if existing else 1,
        )
        self.sources[message.source] = updated
        return updated

    def vehicles(self) -> list[Source]:
        return [source for source in self.sources.values() if source.is_vehicle]

    def of_kind(self, kind: SourceKind) -> list[Source]:
        return [source for source in self.sources.values() if source.kind is kind]
