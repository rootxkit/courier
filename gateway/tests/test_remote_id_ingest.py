"""Receiver datagrams to the bus. P1-15."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gateway import odid
from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_ingest import DatagramError, RemoteIdIngest, parse_datagram
from gateway.tests.rid_frames import NOW, FlatGeoid, basic, location, pack


class FakeBus:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.fail:
            raise ConnectionError("bus down")
        self.sent.append((subject, json.loads(payload)))


def datagram(payload: bytes, **extra: Any) -> bytes:
    report = {
        "receiver_id": "rx-1",
        "transmitter": "AA:BB:CC:00:00:01",
        "payload_hex": payload.hex(),
        "rssi_dbm": -70,
        **extra,
    }
    return json.dumps(report).encode()


def ingest(bus: FakeBus) -> RemoteIdIngest:
    return RemoteIdIngest(
        tracker=RemoteIdTracker(geoid=FlatGeoid()),
        bus=bus,
        clock_s=lambda: 0.0,
        wall=lambda: NOW,
    )


async def test_a_complete_broadcast_is_published_as_telemetry() -> None:
    bus = FakeBus()
    service = ingest(bus)

    await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")

    assert len(bus.sent) == 1
    subject, message = bus.sent[0]
    assert subject == f"telemetry.{message['drone_id']}"
    assert message["source"] == "remote_id"
    assert message["ts"] == NOW.isoformat()
    assert service.published == 1


async def test_a_refused_datagram_publishes_nothing_and_is_counted() -> None:
    bus = FakeBus()
    service = ingest(bus)

    await service.on_datagram(b"not json", "127.0.0.1")
    await service.on_datagram(datagram(b"\x12\x00\x01"), "127.0.0.1")

    assert bus.sent == []
    assert service.refused == 2


async def test_a_bus_failure_is_logged_not_raised() -> None:
    service = ingest(FakeBus(fail=True))

    await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")

    assert service.published == 0


async def test_an_incomplete_broadcast_waits_quietly() -> None:
    bus = FakeBus()
    service = ingest(bus)

    await service.on_datagram(datagram(location()), "127.0.0.1")

    assert bus.sent == []
    assert service.refused == 0


def test_the_datagram_fields_arrive_in_the_frame() -> None:
    frame = parse_datagram(datagram(basic(), rssi_dbm=-55.5), received_at=NOW)

    assert (frame.receiver_id, frame.transmitter, frame.rssi_dbm) == (
        "rx-1",
        "AA:BB:CC:00:00:01",
        -55.5,
    )
    assert odid.decode(frame.payload)[0] == odid.decode(basic())[0]


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (b"[1, 2]", "object"),
        (json.dumps({"transmitter": "x", "payload_hex": "00"}).encode(), "receiver_id"),
        (datagram(b"", payload_hex="zz"), "hex"),
        (datagram(b"\x00", rssi_dbm="loud"), "rssi"),
        (datagram(b"\x00", rssi_dbm=True), "rssi"),
        (b" " * 5000, "more than"),
    ],
    ids=["not-object", "no-receiver", "not-hex", "rssi-text", "rssi-bool", "too-big"],
)
def test_malformed_datagrams_are_refused(data: bytes, complaint: str) -> None:
    assert parse_datagram(datagram(basic()), received_at=NOW)
    with pytest.raises(DatagramError, match=complaint):
        parse_datagram(data, received_at=NOW)


# --- the socket and the geoid, as the service wires them ------------------------


async def test_a_datagram_on_the_socket_reaches_the_bus() -> None:
    import asyncio
    import socket

    from gateway.remote_id_ingest import listen

    bus = FakeBus()
    service = ingest(bus)
    transport = await listen(service, "127.0.0.1", 0)
    port = transport.get_extra_info("sockname")[1]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(datagram(pack(basic(), location())), ("127.0.0.1", port))
        for _ in range(200):
            if bus.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        transport.close()

    assert len(bus.sent) == 1
    assert bus.sent[0][1]["source"] == "remote_id"


def test_without_a_geoid_path_there_is_no_geoid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import remote_id_ingest

    warnings: list[str] = []
    monkeypatch.setattr(
        remote_id_ingest._log,
        "warning",
        lambda message, *a, **k: warnings.append(message),
    )

    assert remote_id_ingest.load_geoid(None) is None
    assert any("no geoid model configured" in w for w in warnings)


def test_a_geoid_path_loads_the_grid(tmp_path: Any) -> None:
    from common.tests.test_geoid import pgm
    from gateway.remote_id_ingest import load_geoid

    path = tmp_path / "grid.pgm"
    path.write_bytes(pgm())
    geoid = load_geoid(path)

    assert geoid is not None
    assert geoid.undulation_m(0.0, 0.0) == pytest.approx(-100.0 + 0.01 * 1900)
