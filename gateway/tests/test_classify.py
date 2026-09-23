"""Source classification and the hot-path split.

Every constant is derived from pymavlink here as well, and by a *different*
route than the code uses where one exists: the code reads
`MAVLink_heartbeat_message.id`, and these tests cross-check against
`mavlink_map`, which is pymavlink's id-to-class table. A test that recomputed
the value the same way would only prove the expression was copied correctly.

The rule this enforces is CLAUDE.md's: never write a wire-format number from
memory. A wrong-but-plausible message id would route a message the pipeline is
waiting for into the archive, and nothing would error.
"""

from __future__ import annotations

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.classify import (
    HEARTBEAT_ID,
    HOT_PATH_MESSAGE_IDS,
    HOT_PATH_MESSAGE_NAMES,
    NON_VEHICLE_MAV_TYPES,
    Source,
    SourceKind,
    SourceRegistry,
    classify_heartbeat,
    is_hot_path,
)
from gateway.parsing import ParsedMessage, SourceId, parse_datagram


def encode(message: object) -> bytes:
    link = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    link.signing.sign_outgoing = False
    return bytes(message.pack(link))  # type: ignore[attr-defined]


def heartbeat(
    *,
    mav_type: int,
    autopilot: int = mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
    sysid: int = 1,
    compid: int = 1,
) -> ParsedMessage:
    link = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    link.signing.sign_outgoing = False
    raw = link.heartbeat_encode(
        mav_type, autopilot, 0, 0, mavlink.MAV_STATE_ACTIVE
    ).pack(link)
    parsed = parse_datagram(bytes(raw))
    assert len(parsed) == 1
    return parsed.messages[0]


def attitude(*, sysid: int = 1, compid: int = 1) -> ParsedMessage:
    link = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    link.signing.sign_outgoing = False
    raw = link.attitude_encode(0, 0.1, 0.2, 1.5, 0.0, 0.0, 0.0).pack(link)
    return parse_datagram(bytes(raw)).messages[0]


# --- the ids are pymavlink's, by a second route ----------------------------


def test_every_hot_path_id_matches_pymavlinks_own_table() -> None:
    """Cross-checked against `mavlink_map`, not recomputed the same way."""
    by_name = {cls.name: message_id for message_id, cls in mavlink.mavlink_map.items()}
    expected = {by_name[name] for name in HOT_PATH_MESSAGE_NAMES}

    assert expected == HOT_PATH_MESSAGE_IDS


def test_every_hot_path_name_exists_in_the_dialect() -> None:
    """A typo'd name would silently drop a message from the hot path."""
    known = {cls.name for cls in mavlink.mavlink_map.values()}
    assert set(HOT_PATH_MESSAGE_NAMES) <= known


def test_the_heartbeat_id_is_pymavlinks() -> None:
    by_name = {cls.name: message_id for message_id, cls in mavlink.mavlink_map.items()}
    assert by_name["HEARTBEAT"] == HEARTBEAT_ID


def test_the_hot_path_is_the_adr_001_list() -> None:
    """Pinned so the split cannot drift from the decision that set it."""
    assert set(HOT_PATH_MESSAGE_NAMES) == {
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
    }


# --- the split -------------------------------------------------------------


@pytest.mark.parametrize("name", HOT_PATH_MESSAGE_NAMES)
def test_hot_path_messages_are_on_the_hot_path(name: str) -> None:
    by_name = {cls.name: message_id for message_id, cls in mavlink.mavlink_map.items()}
    assert is_hot_path(by_name[name])


@pytest.mark.parametrize(
    "name", ["ATTITUDE", "VIBRATION", "RAW_IMU", "SCALED_PRESSURE", "TIMESYNC"]
)
def test_everything_else_is_archived_not_on_the_hot_path(name: str) -> None:
    """Archived, never discarded.

    These are exactly what an incident investigation reads, and P10-03 cannot
    replay what was never stored. `is_hot_path` being False means "not
    converted to live state", not "thrown away" - the archive already holds
    the datagram either way.
    """
    by_name = {cls.name: message_id for message_id, cls in mavlink.mavlink_map.items()}
    assert not is_hot_path(by_name[name])


def test_mission_item_reached_is_on_the_hot_path_though_never_observed() -> None:
    """ADR-001 saw no MISSION_ITEM_REACHED, because the aircraft was parked.

    An event that did not happen is not a missing stream. It stays on the hot
    path, and §6.4 forbids treating its absence as a fault.
    """
    by_name = {cls.name: message_id for message_id, cls in mavlink.mavlink_map.items()}
    assert is_hot_path(by_name["MISSION_ITEM_REACHED"])


# --- classification --------------------------------------------------------


def test_an_autopilot_heartbeat_is_a_vehicle() -> None:
    assert (
        classify_heartbeat(
            mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA
        )
        is SourceKind.VEHICLE
    )


def test_a_ground_station_is_not_a_vehicle() -> None:
    assert (
        classify_heartbeat(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID)
        is SourceKind.GCS
    )


@pytest.mark.parametrize("mav_type", sorted(NON_VEHICLE_MAV_TYPES))
def test_peripherals_are_components(mav_type: int) -> None:
    if mav_type == mavlink.MAV_TYPE_GCS:
        pytest.skip("GCS has its own kind")
    assert (
        classify_heartbeat(mav_type, mavlink.MAV_AUTOPILOT_INVALID)
        is SourceKind.COMPONENT
    )


def test_an_invalid_autopilot_is_a_component_whatever_its_type() -> None:
    """A companion computer may declare a vehicle type and no autopilot."""
    assert (
        classify_heartbeat(mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_AUTOPILOT_INVALID)
        is SourceKind.COMPONENT
    )


def test_an_unknown_type_with_a_real_autopilot_resolves_to_vehicle() -> None:
    """Ambiguity resolves to vehicle.

    Registering a ground station as an aircraft is a nuisance; failing to
    register an aircraft is the direction that gets someone hurt.
    """
    unknown_but_plausible = 250
    assert unknown_but_plausible not in NON_VEHICLE_MAV_TYPES
    assert (
        classify_heartbeat(unknown_but_plausible, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA)
        is SourceKind.VEHICLE
    )


def test_classification_ignores_the_sysid() -> None:
    """255 is a convention and GCS_SYSTEM_ID is a user setting.

    An aircraft configured on SYSID 255 is still an aircraft, and a ground
    station on SYSID 1 is still a ground station.
    """
    registry = SourceRegistry(station_id="tbilisi-base-1")

    aircraft_on_255 = registry.observe(
        heartbeat(mav_type=mavlink.MAV_TYPE_QUADROTOR, sysid=255, compid=1)
    )
    gcs_on_1 = registry.observe(
        heartbeat(
            mav_type=mavlink.MAV_TYPE_GCS,
            autopilot=mavlink.MAV_AUTOPILOT_INVALID,
            sysid=1,
            compid=190,
        )
    )

    assert aircraft_on_255.kind is SourceKind.VEHICLE
    assert gcs_on_1.kind is SourceKind.GCS


def test_one_heartbeat_is_enough() -> None:
    """A just-booted aircraft has sent one HEARTBEAT and nothing else.

    That is exactly when it must be visible, so volume is never an input.
    """
    registry = SourceRegistry(station_id="tbilisi-base-1")
    source = registry.observe(heartbeat(mav_type=mavlink.MAV_TYPE_QUADROTOR))

    assert source.kind is SourceKind.VEHICLE
    assert source.heartbeat_count == 1
    assert registry.vehicles() == [source]


# --- the registry, per (sysid, compid) -------------------------------------


def test_the_adr_001_shape_is_separated_correctly() -> None:
    """ADR-001 observed the autopilot at 1/1 and QGC at 255/190.

    Both are on one link, and the Gateway must not register the second.
    """
    registry = SourceRegistry(station_id="tbilisi-base-1")
    registry.observe(heartbeat(mav_type=mavlink.MAV_TYPE_QUADROTOR, sysid=1, compid=1))
    registry.observe(
        heartbeat(
            mav_type=mavlink.MAV_TYPE_GCS,
            autopilot=mavlink.MAV_AUTOPILOT_INVALID,
            sysid=255,
            compid=190,
        )
    )

    assert [source.source_id for source in registry.vehicles()] == [
        SourceId(sysid=1, compid=1)
    ]
    assert [source.source_id for source in registry.of_kind(SourceKind.GCS)] == [
        SourceId(sysid=255, compid=190)
    ]


def test_a_gimbal_under_the_vehicles_sysid_is_a_separate_source() -> None:
    """The reason the key is (sysid, compid) and not sysid alone.

    A gimbal heartbeating under the aircraft's own SYSID must not make the
    aircraft look like a gimbal, nor the reverse.
    """
    registry = SourceRegistry(station_id="tbilisi-base-1")
    registry.observe(heartbeat(mav_type=mavlink.MAV_TYPE_QUADROTOR, sysid=1, compid=1))
    registry.observe(
        heartbeat(
            mav_type=mavlink.MAV_TYPE_GIMBAL,
            autopilot=mavlink.MAV_AUTOPILOT_INVALID,
            sysid=1,
            compid=154,
        )
    )

    assert len(registry.sources) == 2
    assert [source.source_id for source in registry.vehicles()] == [
        SourceId(sysid=1, compid=1)
    ]
    assert registry.sources[SourceId(1, 154)].kind is SourceKind.COMPONENT


def test_a_source_with_no_heartbeat_is_unclassified_not_a_vehicle() -> None:
    """The absence of a judgement is not a judgement.

    §7 archives an unknown source and surfaces it as an unclaimed source
    rather than letting it walk into the fleet.
    """
    registry = SourceRegistry(station_id="tbilisi-base-1")
    source = registry.observe(attitude(sysid=42, compid=1))

    assert source.kind is SourceKind.UNCLASSIFIED
    assert registry.vehicles() == []
    assert source.message_count == 1


def test_a_heartbeat_after_other_traffic_classifies_retroactively() -> None:
    registry = SourceRegistry(station_id="tbilisi-base-1")
    registry.observe(attitude(sysid=7, compid=1))
    source = registry.observe(
        heartbeat(mav_type=mavlink.MAV_TYPE_QUADROTOR, sysid=7, compid=1)
    )

    assert source.kind is SourceKind.VEHICLE
    assert source.message_count == 2
    assert source.heartbeat_count == 1


def test_a_source_that_changes_what_it_declares_is_reclassified() -> None:
    """Not latched.

    A source whose declared type changes has been reconfigured or replaced.
    Continuing to treat it as what it used to be is how a companion computer
    inherits an aircraft's identity.
    """
    registry = SourceRegistry(station_id="tbilisi-base-1")
    registry.observe(heartbeat(mav_type=mavlink.MAV_TYPE_QUADROTOR, sysid=3, compid=1))
    changed = registry.observe(
        heartbeat(
            mav_type=mavlink.MAV_TYPE_GIMBAL,
            autopilot=mavlink.MAV_AUTOPILOT_INVALID,
            sysid=3,
            compid=1,
        )
    )

    assert changed.kind is SourceKind.COMPONENT
    assert registry.vehicles() == []


def test_non_heartbeat_traffic_does_not_change_a_classification() -> None:
    """Volume is not evidence, in either direction."""
    registry = SourceRegistry(station_id="tbilisi-base-1")
    registry.observe(heartbeat(mav_type=mavlink.MAV_TYPE_QUADROTOR, sysid=9))
    for _ in range(50):
        registry.observe(attitude(sysid=9))

    source = registry.sources[SourceId(9, 1)]
    assert source.kind is SourceKind.VEHICLE
    assert source.heartbeat_count == 1
    assert source.message_count == 51


def test_a_source_is_a_dataclass_not_a_drone() -> None:
    """A SYSID is an address, never an identity.

    Nothing here produces a `drone_id`; §7's binding does, at the record's
    timestamp, and this type deliberately has no field for one.
    """
    source = Source(source_id=SourceId(1, 1), kind=SourceKind.VEHICLE)
    assert not hasattr(source, "drone_id")
    assert str(source.source_id) == "1/1"
