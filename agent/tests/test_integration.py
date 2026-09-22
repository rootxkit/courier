"""End-to-end: a real UDP socket, a real queue, a real WebSocket, an outage.

No hardware and no network beyond loopback. A pymavlink source sends the
ADR-001 message mix at the measured rates; a stub Gateway that exists only in
this file speaks relay-v1 back.

The stub is killed mid-run and restarted, which is the whole point: P1-01's
acceptance criterion is that pulling the network cable loses nothing. That
criterion is verified on hardware by a human. This test verifies the part that
can be verified without one — that every record crosses the gap exactly once,
in order, and that the relay keeps accepting datagrams throughout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

import websockets
from pymavlink.dialects.v20 import ardupilotmega as mav2
from websockets.asyncio.server import Server, ServerConnection, serve

from agent.config import RelayConfig
from agent.framing import decode_records
from agent.queue import DurableQueue
from agent.relay import Relay

# Seconds. The outage length is the figure the task specified; the rest are
# sized to be comfortably longer than the relay's 100 ms batch interval.
WARMUP_S = 2.0
OUTAGE_S = 10.0

# Limits, not sleeps. The relay can be a full BACKOFF_MAX_S into its backoff
# when the sink returns, so anything shorter than that is a coin flip.
RECOVERY_LIMIT_S = 45.0
DRAIN_LIMIT_S = 30.0

TOKEN = "integration-test-token"

# The ADR-001 mix, trimmed to the messages the pipeline names plus the
# high-rate ones that dominate the byte count. Reproducing all 31 observed
# types would not exercise anything further: the relay does not parse MAVLink,
# so what varies between types is only the datagram length.
MESSAGE_RATES_HZ: tuple[tuple[str, float], ...] = (
    ("attitude", 10.0),
    ("vfr_hud", 10.0),
    ("global_position_int", 3.0),
    ("battery_status", 3.0),
    ("ekf_status_report", 3.0),
    ("sys_status", 2.0),
    ("gps_raw_int", 2.0),
    ("heartbeat", 1.0),
)


class StubGateway:
    """A minimal relay-v1 server. Lives in this test and nowhere else."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.store: dict[int, bytes] = {}
        self.arrival_order: list[list[int]] = []
        self.epochs: set[str] = set()
        self.gaps: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.rejected_tokens: list[str | None] = []
        self._server: Server | None = None

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def highest_contiguous(self) -> int:
        """Highest seq such that 0..seq are all present."""
        seq = -1
        while seq + 1 in self.store:
            seq += 1
        return seq

    async def _handle(self, connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        presented = request.headers.get("Authorization")
        if presented != f"Bearer {TOKEN}":
            self.rejected_tokens.append(presented)
            await connection.close(code=1008, reason="unauthorised")
            return

        hello = json.loads(await connection.recv())
        assert hello["type"] == "hello"
        self.epochs.add(hello["epoch"])

        # The server is authoritative about what it holds (relay-v1 §5).
        await connection.send(
            json.dumps(
                {
                    "type": "welcome",
                    "protocol_version": 1,
                    "resume_from_seq": self.highest_contiguous + 1,
                }
            )
        )

        session_order: list[int] = []
        self.arrival_order.append(session_order)

        with contextlib.suppress(websockets.WebSocketException):
            async for message in connection:
                if isinstance(message, str):
                    payload = json.loads(message)
                    if payload["type"] == "status":
                        self.statuses.append(payload)
                    elif payload["type"] == "gap":
                        self.gaps.append(payload)
                    continue

                for record in decode_records(message):
                    session_order.append(record.seq)
                    # Persist before acknowledging (relay-v1 §7). Here that is
                    # a dict, but the ordering is the point.
                    self.store[record.seq] = record.datagram

                if session_order:
                    await connection.send(
                        json.dumps(
                            {
                                "type": "ack",
                                "epoch": hello["epoch"],
                                "seq": self.highest_contiguous,
                            }
                        )
                    )


def _build_messages() -> list[tuple[bytes, float]]:
    """Return (encoded frame, period_s) for each message in the mix."""
    link = mav2.MAVLink(None, srcSystem=1, srcComponent=1)
    link.signing.sign_outgoing = False

    # Keyword arguments throughout. MAVLink constructor order is not the wire
    # order and not the documentation order, and getting it wrong yields a
    # frame that packs happily and means something else.
    builders = {
        "heartbeat": lambda: mav2.MAVLink_heartbeat_message(
            type=mav2.MAV_TYPE_QUADROTOR,
            autopilot=mav2.MAV_AUTOPILOT_ARDUPILOTMEGA,
            base_mode=0,
            custom_mode=0,
            system_status=mav2.MAV_STATE_ACTIVE,
            mavlink_version=3,
        ),
        "sys_status": lambda: mav2.MAVLink_sys_status_message(
            onboard_control_sensors_present=0,
            onboard_control_sensors_enabled=0,
            onboard_control_sensors_health=0,
            load=500,
            voltage_battery=12600,
            current_battery=-1,
            battery_remaining=87,
            drop_rate_comm=0,
            errors_comm=0,
            errors_count1=0,
            errors_count2=0,
            errors_count3=0,
            errors_count4=0,
        ),
        "gps_raw_int": lambda: mav2.MAVLink_gps_raw_int_message(
            time_usec=0,
            fix_type=3,
            lat=417151000,
            lon=448271000,
            alt=450000,
            eph=120,
            epv=130,
            vel=0,
            cog=0,
            satellites_visible=14,
        ),
        "attitude": lambda: mav2.MAVLink_attitude_message(
            time_boot_ms=0,
            roll=0.01,
            pitch=0.02,
            yaw=1.57,
            rollspeed=0.0,
            pitchspeed=0.0,
            yawspeed=0.0,
        ),
        "global_position_int": lambda: mav2.MAVLink_global_position_int_message(
            time_boot_ms=0,
            lat=417151000,
            lon=448271000,
            alt=450000,
            relative_alt=60000,
            vx=10,
            vy=20,
            vz=0,
            hdg=9000,
        ),
        "vfr_hud": lambda: mav2.MAVLink_vfr_hud_message(
            airspeed=12.0,
            groundspeed=12.5,
            heading=90,
            throttle=45,
            alt=450.0,
            climb=0.5,
        ),
        "battery_status": lambda: mav2.MAVLink_battery_status_message(
            id=0,
            battery_function=0,
            type=0,
            temperature=2500,
            voltages=[12600] * 10,
            current_battery=1500,
            current_consumed=4200,
            energy_consumed=30,
            battery_remaining=87,
            time_remaining=0,
            charge_state=0,
            voltages_ext=[0] * 4,
            mode=0,
            fault_bitmask=0,
        ),
        "ekf_status_report": lambda: mav2.MAVLink_ekf_status_report_message(
            flags=831,
            velocity_variance=0.1,
            pos_horiz_variance=0.2,
            pos_vert_variance=0.15,
            compass_variance=0.05,
            terrain_alt_variance=0.0,
            airspeed_variance=0.0,
        ),
    }

    return [
        (bytes(builders[name]().pack(link)), 1.0 / hz) for name, hz in MESSAGE_RATES_HZ
    ]


async def _fake_vehicle(port: int, stop: asyncio.Event) -> int:
    """Send the message mix to the relay's socket. Returns datagrams sent."""
    import socket

    messages = _build_messages()
    next_due = {index: time.monotonic() for index in range(len(messages))}
    sent = 0

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while not stop.is_set():
            now = time.monotonic()
            for index, (frame, period) in enumerate(messages):
                if now >= next_due[index]:
                    sender.sendto(frame, ("127.0.0.1", port))
                    sent += 1
                    next_due[index] = now + period
            await asyncio.sleep(0.01)
    finally:
        sender.close()
    return sent


async def _wait_for(predicate: Any, limit_s: float, interval_s: float = 0.2) -> bool:
    """Poll until true, or give up. Never a bare sleep for a condition."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _free_udp_port() -> int:
    """Pick a free UDP port.

    The config deliberately refuses port 0 — "let the OS choose" is never what
    a pilot means — so the test asks for a concrete one instead.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_no_records_are_lost_across_an_outage(tmp_path: Path) -> None:
    """Kill the Gateway for 10 s mid-flight. Lose nothing."""
    gateway_port = _free_port()
    gateway = StubGateway(gateway_port)
    await gateway.start()

    config = RelayConfig(
        station_id="integration-test",
        gateway_url=f"ws://127.0.0.1:{gateway_port}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=_free_udp_port(),
    )
    durable_queue = DurableQueue(config.queue_path, max_bytes=64 * 1024 * 1024)
    relay = Relay(config, durable_queue, TOKEN)

    udp = relay.start_intake()
    udp_port = config.bind_port

    stop_vehicle = asyncio.Event()
    vehicle = asyncio.create_task(_fake_vehicle(udp_port, stop_vehicle))
    uplink = asyncio.create_task(relay.run_uplink())

    try:
        await asyncio.sleep(WARMUP_S)
        delivered_before = gateway.highest_contiguous
        assert delivered_before > 0, "nothing arrived before the outage"

        # --- the cable is pulled -------------------------------------------
        await gateway.stop()
        await asyncio.sleep(OUTAGE_S)

        # The relay must still be accepting datagrams while disconnected.
        assert durable_queue.depth > 0, "nothing was queued during the outage"

        # --- and plugged back in -------------------------------------------
        await gateway.start()

        # Wait for progress rather than sleeping a fixed time. The relay may be
        # deep in backoff when the sink returns - up to BACKOFF_MAX_S before it
        # even retries - and a fixed RECOVERY_S that happened to be long enough
        # on a fast machine is a test that fails on a slower one for no reason
        # of its own. This is what failed in CI.
        assert await _wait_for(
            lambda: gateway.highest_contiguous > delivered_before, RECOVERY_LIMIT_S
        ), "the relay did not resume delivering after the sink returned"

        # Then let the backlog drain, again by condition rather than by clock.
        assert await _wait_for(lambda: durable_queue.depth < 100, DRAIN_LIMIT_S), (
            "the backlog did not drain"
        )
    finally:
        stop_vehicle.set()
        await vehicle
        uplink.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await uplink
        relay.stop()
        udp.close()
        await gateway.stop()
        # Read the counters before closing the database, not after.
        received_by_relay = durable_queue.next_seq
        intake_drops = durable_queue.dropped_intake_total
        cap_drops = durable_queue.dropped_cap_total
        durable_queue.close()

    delivered = gateway.highest_contiguous

    # Every sequence number from 0 to the high-water mark arrived.
    assert sorted(gateway.store) == list(range(delivered + 1))

    # The outage was actually crossed.
    assert delivered > delivered_before, "no progress after reconnection"

    # Nothing was dropped at either stage.
    assert intake_drops == 0
    assert cap_drops == 0
    assert gateway.gaps == []

    # Almost everything the relay took in was delivered; a little may still be
    # in flight when the test stops.
    assert delivered >= received_by_relay - 200

    # Within a session, records arrive in ascending order (relay-v1 §10).
    for session in gateway.arrival_order:
        assert session == sorted(session), "records arrived out of order"

    # One epoch throughout: the queue was never recreated.
    assert len(gateway.epochs) == 1

    # status kept flowing, which is what tells the Gateway the station is alive.
    assert len(gateway.statuses) >= 2
    assert all("last_datagram_age_ms" in s for s in gateway.statuses)
    assert all("dropped_intake_total" in s for s in gateway.statuses)
    assert all("dropped_cap_total" in s for s in gateway.statuses)


async def test_duplicates_across_a_reconnect_dedupe_to_exactly_once(
    tmp_path: Path,
) -> None:
    """At-least-once on the wire, exactly-once after dedupe (relay-v1 §10).

    The stub deliberately acknowledges nothing, so the relay resends its whole
    backlog on every reconnect. Keying on seq must collapse that to one copy.
    """
    gateway_port = _free_port()
    gateway = StubGateway(gateway_port)
    await gateway.start()

    config = RelayConfig(
        station_id="integration-test",
        gateway_url=f"ws://127.0.0.1:{gateway_port}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=_free_udp_port(),
    )
    durable_queue = DurableQueue(config.queue_path)
    relay = Relay(config, durable_queue, TOKEN)

    udp = relay.start_intake()
    udp_port = config.bind_port

    stop_vehicle = asyncio.Event()
    vehicle = asyncio.create_task(_fake_vehicle(udp_port, stop_vehicle))
    uplink = asyncio.create_task(relay.run_uplink())

    try:
        await asyncio.sleep(1.5)
        # Two quick disconnects, each forcing a resend from resume_from_seq.
        for _ in range(2):
            await gateway.stop()
            await asyncio.sleep(0.6)
            await gateway.start()
            await asyncio.sleep(1.5)
    finally:
        stop_vehicle.set()
        await vehicle
        uplink.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await uplink
        relay.stop()
        udp.close()
        await gateway.stop()
        durable_queue.close()

    delivered_once = sorted(gateway.store)
    assert delivered_once == list(range(len(delivered_once)))

    # The wire genuinely carried duplicates; dedupe is doing real work.
    total_on_the_wire = sum(len(session) for session in gateway.arrival_order)
    assert total_on_the_wire >= len(delivered_once)


async def test_a_bad_token_is_refused(tmp_path: Path) -> None:
    """The relay must not retry a rejected credential into a log flood."""
    gateway_port = _free_port()
    gateway = StubGateway(gateway_port)
    await gateway.start()

    config = RelayConfig(
        station_id="integration-test",
        gateway_url=f"ws://127.0.0.1:{gateway_port}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=_free_udp_port(),
    )
    durable_queue = DurableQueue(config.queue_path)
    relay = Relay(config, durable_queue, "wrong-token")

    udp = relay.start_intake()
    uplink = asyncio.create_task(relay.run_uplink())
    try:
        await asyncio.sleep(2.0)
    finally:
        uplink.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await uplink
        relay.stop()
        udp.close()
        await gateway.stop()
        durable_queue.close()

    assert gateway.rejected_tokens
    assert gateway.store == {}
