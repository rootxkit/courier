"""From telemetry messages to alerts: raised once, cleared with hysteresis,
and never for aircraft on the ground."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from airspace.cpa import SeparationPolicy, local_offset_m
from airspace.monitor import AirspaceMonitor, AlertKind, Severity, conflict_key
from airspace.zones import zone_from_geojson

A = UUID(int=1)
B = UUID(int=2)
LAT0 = 41.7151
LON0 = 44.8271
POLICY = SeparationPolicy(
    t_cpa_max_s=60, d_horizontal_min_m=60, d_vertical_min_m=20, neighbour_radius_m=800
)


def message(
    drone_id: UUID,
    north_m: float,
    *,
    vn: float = 0.0,
    armed: bool | None = True,
    alt_amsl_m: float = 550.0,
    label: str | None = None,
) -> dict[str, Any]:
    n1, _ = local_offset_m(LAT0, LON0, LAT0 + 0.001, LON0)
    return {
        "drone_id": str(drone_id),
        "label": label or f"D{drone_id.int}",
        "lat_deg": LAT0 + 0.001 * north_m / n1,
        "lon_deg": LON0,
        "alt_amsl_m": alt_amsl_m,
        "vx_ms": vn,
        "vy_ms": 0.0,
        "vz_ms": 0.0,
        "armed": armed,
    }


def head_on(monitor: AirspaceMonitor, *, now_s: float) -> list[Any]:
    """A at 0 heading north, B 500 m north heading south: CPA in 25 s."""
    monitor.observe(message(A, 0, vn=10), now_s=now_s)
    return monitor.observe(message(B, 500, vn=-10), now_s=now_s).raised


def test_a_head_on_pair_raises_one_critical_conflict_naming_both() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    raised = head_on(monitor, now_s=0.0)

    assert len(raised) == 1
    alert = raised[0]
    assert alert.kind is AlertKind.CONFLICT
    assert alert.severity is Severity.CRITICAL
    assert set(alert.drone_ids) == {A, B}
    assert set(alert.labels) == {"D1", "D2"}
    assert alert.detail["t_cpa_s"] == 25.0


def test_the_same_conflict_is_not_raised_again_every_tick() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    head_on(monitor, now_s=0.0)
    again = head_on(monitor, now_s=1.0)
    assert again == []
    assert len(monitor.active) == 1


def test_the_alert_is_the_same_whichever_aircraft_reported_last() -> None:
    """Both aircraft's messages describe the pair identically."""
    monitor = AirspaceMonitor(policy=POLICY)
    head_on(monitor, now_s=0.0)
    first = monitor.active[0]
    monitor.observe(message(A, 0, vn=10), now_s=1.0)
    second = monitor.active[0]
    assert first.key == second.key == conflict_key(A, B)
    assert first.drone_ids == second.drone_ids


def test_a_resolved_conflict_clears_only_after_the_hysteresis() -> None:
    monitor = AirspaceMonitor(policy=POLICY, clear_after_s=3.0)
    head_on(monitor, now_s=0.0)

    # B turns away: diverging and 500 m apart, no longer a conflict.
    at_1 = monitor.observe(message(B, 500, vn=10), now_s=1.0)
    at_3 = monitor.observe(message(B, 510, vn=10), now_s=3.0)
    at_4 = monitor.observe(message(B, 520, vn=10), now_s=3.5)

    assert at_1.cleared == [] and at_3.cleared == []
    assert [alert.key for alert in at_4.cleared] == [conflict_key(A, B)]
    assert monitor.active == []


def test_silence_does_not_clear_a_conflict_before_the_aircraft_are_stale() -> None:
    """The paired case: no message shows the pair apart, so it stays raised."""
    monitor = AirspaceMonitor(policy=POLICY, clear_after_s=3.0, stale_after_s=15.0)
    head_on(monitor, now_s=0.0)
    assert monitor.tick(now_s=10.0).cleared == []
    assert len(monitor.active) == 1


def test_a_pair_on_the_ground_raises_nothing() -> None:
    """The paired absence: the same geometry, disarmed."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, vn=10, armed=False), now_s=0.0)
    change = monitor.observe(message(B, 20, vn=-10, armed=False), now_s=0.0)
    assert change.raised == []


def test_unknown_armed_state_is_not_treated_as_flying() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, armed=None), now_s=0.0)
    change = monitor.observe(message(B, 20, armed=True), now_s=0.0)
    assert change.raised == []


def test_disarming_clears_the_conflict() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    head_on(monitor, now_s=0.0)
    change = monitor.observe(message(B, 500, armed=False), now_s=1.0)
    assert [alert.key for alert in change.cleared] == [conflict_key(A, B)]


def test_an_aircraft_that_goes_silent_is_dropped_and_its_alerts_cleared() -> None:
    """The end of a condition can be the absence of telemetry; tick() sees it."""
    monitor = AirspaceMonitor(policy=POLICY, stale_after_s=15.0)
    head_on(monitor, now_s=0.0)

    assert monitor.tick(now_s=10.0).cleared == []
    cleared = monitor.tick(now_s=16.0).cleared
    assert [alert.key for alert in cleared] == [conflict_key(A, B)]
    assert len(monitor.index) == 0


def test_a_message_without_velocity_places_nothing() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    incomplete = message(A, 0)
    incomplete["vx_ms"] = None
    monitor.observe(incomplete, now_s=0.0)
    assert len(monitor.index) == 0


def zone(kind: str) -> Any:
    square = [
        [LON0 - 0.01, LAT0 - 0.01],
        [LON0 + 0.01, LAT0 - 0.01],
        [LON0 + 0.01, LAT0 + 0.01],
        [LON0 - 0.01, LAT0 + 0.01],
        [LON0 - 0.01, LAT0 - 0.01],
    ]
    return zone_from_geojson(
        zone_id=UUID(int=77),
        name="Parliament",
        zone_type=kind,
        geojson=json.dumps({"type": "Polygon", "coordinates": [square]}),
        min_alt_amsl_m=None,
        max_alt_amsl_m=None,
    )


def test_entering_a_no_fly_zone_is_critical_and_names_the_zone() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone("no_fly")])
    raised = monitor.observe(message(A, 0), now_s=0.0).raised
    assert len(raised) == 1
    assert raised[0].kind is AlertKind.ZONE
    assert raised[0].severity is Severity.CRITICAL
    assert raised[0].detail["zone_name"] == "Parliament"


def test_a_restricted_zone_is_a_warning() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone("restricted")])
    raised = monitor.observe(message(A, 0), now_s=0.0).raised
    assert raised[0].severity is Severity.WARNING


def test_outside_the_zone_raises_nothing_and_leaving_clears() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone("no_fly")], clear_after_s=3.0)
    far = 5_000.0
    assert monitor.observe(message(A, far), now_s=0.0).raised == []

    monitor.observe(message(A, 0), now_s=1.0)
    monitor.observe(message(A, far), now_s=2.0)
    cleared = monitor.observe(message(A, far), now_s=5.0).cleared
    assert len(cleared) == 1


def test_the_alert_serialises_for_the_bus() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    alert = head_on(monitor, now_s=0.0)[0]
    encoded = json.loads(json.dumps(alert.as_dict()))
    assert encoded["kind"] == "conflict"
    assert encoded["severity"] == "critical"
    assert sorted(encoded["drone_ids"]) == sorted([str(A), str(B)])
