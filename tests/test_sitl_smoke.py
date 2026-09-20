"""Integration smoke test: real SITL vehicles reach us on the expected ports.

Marked `sitl`, so `make test` skips it. It runs in CI against vehicles that
`sim/run_sitl.sh` has already started, and it is what proves the launcher's
contract — one vehicle per UDP port, each with the SYSID it was assigned —
against real ArduPilot rather than against a stub.
"""

from __future__ import annotations

import os

import pytest

# pytest imports every test module during collection, including modules it is
# about to deselect. Both guards therefore have to run at import time, or
# `make test` breaks on a machine that has no pymavlink and no vehicles.
mavutil = pytest.importorskip(
    "pymavlink.mavutil", reason="pymavlink is in the 'sitl' extra"
)

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.skipif(
        not os.environ.get("SITL_INSTANCE_COUNT"),
        reason="SITL_INSTANCE_COUNT is unset; no vehicles are running",
    ),
]

# How long to wait for a vehicle's first heartbeat. SITL is up before the test
# runs, but ArduPilot still needs a moment to start streaming.
HEARTBEAT_TIMEOUT_S = 60.0


def _expected_fleet() -> list[tuple[int, int]]:
    """Return (sysid, udp_port) for every vehicle the launcher should have started."""
    count = int(os.environ.get("SITL_INSTANCE_COUNT", "0"))
    sysid_base = int(os.environ.get("SITL_SYSID_BASE", "1"))
    port_base = int(os.environ.get("SITL_OUT_PORT_BASE", "14560"))
    return [(sysid_base + i, port_base + i) for i in range(count)]


@pytest.mark.parametrize(
    ("expected_sysid", "udp_port"),
    _expected_fleet(),
    ids=lambda value: str(value),
)
def test_vehicle_heartbeats_with_its_assigned_sysid(
    expected_sysid: int, udp_port: int
) -> None:
    connection = mavutil.mavlink_connection(f"udpin:127.0.0.1:{udp_port}")
    try:
        heartbeat = connection.recv_match(
            type="HEARTBEAT", blocking=True, timeout=HEARTBEAT_TIMEOUT_S
        )
        assert heartbeat is not None, (
            f"no HEARTBEAT on UDP {udp_port} within {HEARTBEAT_TIMEOUT_S:.0f} s"
        )
        assert heartbeat.get_srcSystem() == expected_sysid, (
            f"UDP {udp_port} carried SYSID {heartbeat.get_srcSystem()}, "
            f"expected {expected_sysid}"
        )
    finally:
        connection.close()


def test_every_vehicle_has_a_distinct_sysid() -> None:
    """A duplicate SYSID makes two aircraft indistinguishable to the Gateway."""
    seen: dict[int, int] = {}
    for _expected_sysid, udp_port in _expected_fleet():
        connection = mavutil.mavlink_connection(f"udpin:127.0.0.1:{udp_port}")
        try:
            heartbeat = connection.recv_match(
                type="HEARTBEAT", blocking=True, timeout=HEARTBEAT_TIMEOUT_S
            )
            assert heartbeat is not None, f"no HEARTBEAT on UDP {udp_port}"
            sysid = heartbeat.get_srcSystem()
        finally:
            connection.close()

        assert sysid not in seen, (
            f"SYSID {sysid} seen on both UDP {seen[sysid]} and UDP {udp_port}"
        )
        seen[sysid] = udp_port

    assert sorted(seen) == [sysid for sysid, _ in _expected_fleet()]
