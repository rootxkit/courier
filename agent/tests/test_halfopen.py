"""A half-open uplink: bytes stop, the socket stays up, nothing is refused.

Procedure B in the hardware runbook stops the receiver, and the relay gets an
immediate connection refusal. A pulled cable gives silence instead, and the
relay has to notice from missing pongs. That is a different code path, and it
was untested until this file.

Integrity is not what is at risk here. In the half-open state the relay writes
into a void, no acks come back, and so no records are deleted — the queue only
grows. What is at risk is recovery latency: how long the relay spends believing
a dead link is alive. This measures that number.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from agent.config import RelayConfig
from agent.queue import DurableQueue
from agent.relay import Relay
from agent.tests.blackhole import BlackholeProxy
from tools.relay_sink import RelaySink, SinkStore

TOKEN = "halfopen-test-token"

# The relay currently relies on the websockets client default ping settings
# (20 s interval, 20 s timeout), so detection can take up to 40 s plus the time
# to the next ping. Generous, because the point is to MEASURE, not to assert a
# number we have not chosen yet.
DETECTION_LIMIT_S = 120.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class SinkServer:
    """The real sink, behind whatever port it is given."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.store: SinkStore | None = None
        self._server: Any = None

    async def start(self) -> int:
        import http

        from websockets.asyncio.server import serve

        self.store = SinkStore(self.out)
        sink = RelaySink(self.store, TOKEN)

        def process_request(connection: Any, request: Any) -> Any:
            if not sink.authorise(request.headers.get("Authorization")):
                return connection.respond(http.HTTPStatus.UNAUTHORIZED, "no\n")
            return None

        self._server = await serve(
            sink.handle, "127.0.0.1", 0, process_request=process_request
        )
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.store is not None:
            self.store.close()
            self.store = None


async def _source(port: int, stop: asyncio.Event) -> None:
    counter = 0
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while not stop.is_set():
            sender.sendto(f"datagram-{counter:08d}".encode(), ("127.0.0.1", port))
            counter += 1
            await asyncio.sleep(0.02)
    finally:
        sender.close()


async def _wait_for(predicate: Any, limit_s: float, interval_s: float = 0.2) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


async def test_a_half_open_uplink_is_detected_and_recovered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Stall the link without closing it; measure how long detection takes."""
    caplog.set_level(logging.WARNING, logger="agent.relay")

    sink = SinkServer(tmp_path / "sink")
    sink_port = await sink.start()

    udp_port = free_port()
    stop_source = asyncio.Event()
    source = asyncio.create_task(_source(udp_port, stop_source))

    detection_s: float | None = None

    async with BlackholeProxy("127.0.0.1", sink_port) as proxy:
        config = RelayConfig(
            station_id="halfopen-test",
            gateway_url=f"ws://127.0.0.1:{proxy.port}/relay/v1",  # type: ignore[arg-type]
            token_path=tmp_path / "relay.token",
            queue_path=tmp_path / "relay-queue.sqlite3",
            bind_port=udp_port,
        )
        durable_queue = DurableQueue(config.queue_path)
        relay = Relay(config, durable_queue, TOKEN)
        udp = relay.start_intake()
        uplink = asyncio.create_task(relay.run_uplink())

        try:
            # Let a first session establish and deliver something.
            assert await _wait_for(lambda: proxy.connections >= 1, 15.0)
            await asyncio.sleep(2.0)

            store = sink.store
            assert store is not None
            assert await _wait_for(
                lambda: bool(store.all_states()) and store.all_states()[0].stored,
                15.0,
            ), "nothing was delivered before the stall"
            delivered_before = len(store.all_states()[0].stored)

            # --- the cable is "pulled" -------------------------------------
            connections_before = proxy.connections
            caplog.clear()
            stalled_at = time.monotonic()
            proxy.engage()

            # Let any ack already past the proxy land, then take the watermark
            # that must not move for the rest of the stall.
            await asyncio.sleep(1.0)
            oldest_at_stall = durable_queue.oldest_seq_held

            # Detection shows as the relay abandoning the session. The
            # reconnect that follows opens a new TCP connection to the proxy,
            # which is observable without reading the relay's internals.
            detected = await _wait_for(
                lambda: any(
                    record.getMessage() == "uplink session ended"
                    for record in caplog.records
                ),
                DETECTION_LIMIT_S,
                interval_s=0.25,
            )
            detection_s = time.monotonic() - stalled_at

            print(
                f"\n[halfopen] detection took {detection_s:.1f}s "
                f"(limit {DETECTION_LIMIT_S:.0f}s)"
            )
            assert detected, (
                f"the relay did not notice a dead uplink within "
                f"{DETECTION_LIMIT_S:.0f}s"
            )

            # Integrity is never at risk in this state: no acks arrive through
            # a blackhole, so nothing is deleted. Only recovery latency is.
            # Measured against the watermark as it stood once the stall had
            # settled - records acknowledged BEFORE the stall were legitimately
            # deleted, and asserting against 0 would just be wrong.
            assert durable_queue.depth > 0
            assert durable_queue.oldest_seq_held == oldest_at_stall, (
                "records were deleted while no ack could have arrived"
            )

            # --- and plugged back in ---------------------------------------
            proxy.clear()
            assert await _wait_for(
                lambda: proxy.connections > connections_before, 30.0
            ), "the relay did not reconnect after the stall cleared"

            assert await _wait_for(
                lambda: len(store.all_states()[0].stored) > delivered_before, 30.0
            ), "no progress after recovery"
            await asyncio.sleep(2.0)
        finally:
            stop_source.set()
            await source
            uplink.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await uplink
            relay.stop()
            udp.close()
            taken_in = durable_queue.next_seq
            durable_queue.close()
            await sink.stop()

    reopened = SinkStore(tmp_path / "sink")
    reopened.load_all()
    state = reopened.all_states()[0]
    stored = sorted(state.stored)
    reopened.close()

    # Same criteria as Procedure B.
    assert stored == list(range(len(stored))), "the sequence has a hole in it"
    assert len(stored) >= taken_in - 200
    assert state.gaps == []
    assert detection_s is not None


async def test_bytes_held_by_the_proxy_are_delivered_not_dropped(
    tmp_path: Path,
) -> None:
    """The fixture models a stall, not a lossy link.

    If the proxy dropped bytes instead of holding them, a stall shorter than
    the detection time would corrupt the WebSocket stream and the relay would
    reconnect for the wrong reason - the test would pass while measuring
    something else entirely.
    """
    received: list[bytes] = []
    ready = asyncio.Event()

    async def echo_target(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        ready.set()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                received.append(data)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    target = await asyncio.start_server(echo_target, "127.0.0.1", 0)
    target_port = int(target.sockets[0].getsockname()[1])

    try:
        async with BlackholeProxy("127.0.0.1", target_port) as proxy:
            _reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            await asyncio.wait_for(ready.wait(), 5.0)

            writer.write(b"before-")
            await writer.drain()
            await asyncio.sleep(0.3)
            assert b"".join(received) == b"before-"

            proxy.engage()
            writer.write(b"during-")
            await writer.drain()
            await asyncio.sleep(0.5)
            assert b"".join(received) == b"before-", "bytes crossed the blackhole"

            proxy.clear()
            await asyncio.sleep(0.5)
            assert b"".join(received) == b"before-during-", "held bytes were lost"

            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        target.close()
        await target.wait_closed()


async def test_the_proxy_forwards_transparently_when_open(tmp_path: Path) -> None:
    """The presence half: with the switch off it is an ordinary proxy."""
    seen: list[bytes] = []

    async def target(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            data = await reader.read(64)
            seen.append(data)
            writer.write(b"pong")
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(target, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        async with BlackholeProxy("127.0.0.1", port) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"ping")
            await writer.drain()

            assert await asyncio.wait_for(reader.read(4), 5.0) == b"pong"
            assert seen == [b"ping"]
            assert proxy.connections == 1
            assert proxy.blackholed is False

            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
