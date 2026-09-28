"""Drone status is derived, never set. P2-05. No database needed."""

from __future__ import annotations

import pytest

from api.registry import DroneStatus, derive_status


@pytest.mark.parametrize(
    ("in_maintenance", "retired", "live", "expected"),
    [
        (False, False, None, DroneStatus.OFFLINE),
        (False, False, {"armed": False}, DroneStatus.IDLE),
        (False, False, {"armed": None}, DroneStatus.IDLE),
        (False, False, {"armed": True}, DroneStatus.IN_FLIGHT),
        (False, True, {"armed": True}, DroneStatus.OFFLINE),
        (True, False, {"armed": True}, DroneStatus.MAINTENANCE),
        (True, False, None, DroneStatus.MAINTENANCE),
    ],
)
def test_status_is_derived(
    in_maintenance: bool,
    retired: bool,
    live: dict[str, object] | None,
    expected: DroneStatus,
) -> None:
    assert (
        derive_status(in_maintenance=in_maintenance, retired=retired, live=live)
        is expected
    )


def test_statuses_that_need_missing_signals_are_never_produced() -> None:
    """ASSIGNED needs missions, CHARGING a charging signal. Neither exists, so
    neither may be guessed."""
    produced = {
        derive_status(in_maintenance=m, retired=r, live=live)
        for m in (True, False)
        for r in (True, False)
        for live in (None, {"armed": True}, {"armed": False})
    }
    assert DroneStatus.ASSIGNED not in produced
    assert DroneStatus.CHARGING not in produced
    assert produced == {
        DroneStatus.MAINTENANCE,
        DroneStatus.OFFLINE,
        DroneStatus.IN_FLIGHT,
        DroneStatus.IDLE,
    }
