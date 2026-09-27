"""The relay's side of the relay-v1 handshake, including the paths that hurt.

Every test here drives the real `Relay` against a stub that can be made to
misbehave in one specific way. The stub is deliberately not the sink: the point
is to say things a correct server never would.

Three of these cover code that shipped without ever executing. The existing
integration tests assert `gateway.gaps == []` — that a gap does *not* happen —
which says nothing whatever about the code that sends one. That is the pattern
this file exists to break: presence is tested, not only absence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import socket
from pathlib import Path
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from agent.config import RelayConfig
from agent.framing import RECORD_HEADER_BYTES, decode_records
from agent.queue import DurableQueue
from agent.relay import BACKOFF_MAX_S, ProtocolError, Relay
from tests.ports import free_tcp_port, free_udp_port

TOKEN = "handshake-test-token"
DATAGRAM_BYTES = 32
RECORD_BYTES = RECORD_HEADER_BYTES + DATAGRAM_BYTES


class ScriptedGateway:
    """A server that answers `hello` with whatever it is told to."""

    def __init__(self, port: int, resume_from_seq: int) -> None:
        self.port = port
        self.resume_from_seq = resume_from_seq
        self.welcome_type = "welcome"
        self.hellos: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []
        self.records: dict[int, bytes] = {}
        self.acks_to_send: list[dict[str, Any]] = []
        self._server: Any = None

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, connection: ServerConnection) -> None:
        hello = json.loads(await connection.recv())
        self.hellos.append(hello)

        await connection.send(
            json.dumps(
                {
                    "type": self.welcome_type,
                    "protocol_version": 1,
                    "resume_from_seq": self.resume_from_seq,
                }
            )
        )

        for ack in self.acks_to_send:
            await connection.send(json.dumps(ack))

        with contextlib.suppress(websockets.WebSocketException):
            async for message in connection:
                if isinstance(message, str):
                    payload = json.loads(message)
                    if payload.get("type") == "gap":
                        self.gaps.append(payload)
                    continue
                for record in decode_records(message):
                    self.records[record.seq] = record.datagram


def make_relay(
    tmp_path: Path,
    port: int,
    *,
    queue_max_bytes: int = 64 * 1024 * 1024,
    intake_queue_size: int = 10000,
) -> tuple[Relay, DurableQueue, RelayConfig]:
    config = RelayConfig(
        station_id="handshake-test",
        gateway_url=f"ws://127.0.0.1:{port}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=free_udp_port(),
        queue_max_bytes=queue_max_bytes,
        intake_queue_size=intake_queue_size,
    )
    durable_queue = DurableQueue(config.queue_path, max_bytes=config.queue_max_bytes)
    return Relay(config, durable_queue, TOKEN), durable_queue, config


def fill(durable_queue: DurableQueue, count: int) -> None:
    """Put `count` records through the queue without any sockets."""
    durable_queue.append([(1_000 + n, b"x" * DATAGRAM_BYTES) for n in range(count)])


async def run_uplink_briefly(relay: Relay, seconds: float = 1.5) -> None:
    task = asyncio.create_task(relay.run_uplink())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --- #1: the gap path, driven by real eviction ------------------------------


async def test_cap_eviction_produces_a_gap_with_the_documented_range(
    tmp_path: Path,
) -> None:
    """The relay reports exactly the records the cap destroyed.

    Not "a gap was sent": the precise half-open range from relay-v1 §11, where
    from_seq is inclusive and to_seq exclusive. An off-by-one here would
    misstate which telemetry is missing from a flight record, which is the
    question an accident investigation asks.
    """
    port = free_tcp_port()
    # Room for five records; the sixth onwards evicts the oldest.
    relay, durable_queue, _ = make_relay(
        tmp_path, port, queue_max_bytes=RECORD_BYTES * 5
    )

    fill(durable_queue, 12)

    # Eviction really happened, and left 7..11 on disk.
    assert durable_queue.dropped_cap_total == 7
    assert durable_queue.oldest_seq_held == 7
    assert durable_queue.newest_seq_held == 11

    # The server wants seq 3 onward. 3..6 no longer exist anywhere.
    gateway = ScriptedGateway(port, resume_from_seq=3)
    await gateway.start()
    try:
        await run_uplink_briefly(relay)
    finally:
        await gateway.stop()
        # Read the queue before closing it, not after.
        oldest_after = durable_queue.oldest_seq_held
        epoch = durable_queue.epoch
        durable_queue.close()

    assert len(gateway.gaps) == 1, "the relay did not report the gap"
    gap = gateway.gaps[0]

    assert gap["type"] == "gap"
    assert gap["from_seq"] == 3
    assert gap["to_seq"] == 7
    assert gap["reason"] == "queue_cap"
    assert gap["epoch"] == epoch

    # The half-open range names 3, 4, 5, 6 — and not 7, which still exists.
    missing = list(range(gap["from_seq"], gap["to_seq"]))
    assert missing == [3, 4, 5, 6]
    assert oldest_after not in missing


async def test_after_a_gap_the_relay_resumes_from_what_it_still_holds(
    tmp_path: Path,
) -> None:
    """Having reported the hole, it sends everything it does have."""
    port = free_tcp_port()
    relay, durable_queue, _ = make_relay(
        tmp_path, port, queue_max_bytes=RECORD_BYTES * 5
    )
    fill(durable_queue, 12)
    oldest = durable_queue.oldest_seq_held

    gateway = ScriptedGateway(port, resume_from_seq=0)
    await gateway.start()
    try:
        await run_uplink_briefly(relay, seconds=2.0)
    finally:
        await gateway.stop()
        durable_queue.close()

    assert gateway.gaps
    assert sorted(gateway.records) == list(range(oldest, 12))


async def test_no_gap_when_the_server_asks_for_records_still_held(
    tmp_path: Path,
) -> None:
    """The absence half of the pair, now that presence is covered."""
    port = free_tcp_port()
    relay, durable_queue, _ = make_relay(tmp_path, port)
    fill(durable_queue, 10)

    gateway = ScriptedGateway(port, resume_from_seq=4)
    await gateway.start()
    try:
        await run_uplink_briefly(relay)
    finally:
        await gateway.stop()
        durable_queue.close()

    assert gateway.gaps == []
    assert sorted(gateway.records) == [4, 5, 6, 7, 8, 9]


# --- #2: the two ProtocolError raises ---------------------------------------


async def test_a_reply_that_is_not_welcome_is_a_protocol_error(
    tmp_path: Path,
) -> None:
    port = free_tcp_port()
    relay, durable_queue, _ = make_relay(tmp_path, port)
    fill(durable_queue, 3)

    gateway = ScriptedGateway(port, resume_from_seq=0)
    gateway.welcome_type = "greetings"
    await gateway.start()
    try:
        with pytest.raises(ProtocolError, match=r"expected welcome"):
            await relay._session()
    finally:
        await gateway.stop()
        durable_queue.close()


async def test_a_server_claiming_unsent_records_is_a_protocol_error(
    tmp_path: Path,
) -> None:
    """relay-v1 §11: this is not a gap, and must not be treated as one.

    A gap says "data was lost". This says the two ends disagree about which
    epoch or station they are discussing. Rebasing onto the server's number
    would write records whose sequence means something different at each end,
    so the relay stops instead.
    """
    port = free_tcp_port()
    relay, durable_queue, _ = make_relay(tmp_path, port)
    fill(durable_queue, 5)  # highest seq is 4

    gateway = ScriptedGateway(port, resume_from_seq=99)
    await gateway.start()
    try:
        with pytest.raises(ProtocolError, match=r"never produced past"):
            await relay._session()
    finally:
        await gateway.stop()
        durable_queue.close()

    assert gateway.gaps == [], "a disagreement was misreported as data loss"
    assert gateway.records == {}


async def test_resuming_exactly_one_past_the_end_is_legitimate(
    tmp_path: Path,
) -> None:
    """Everything acknowledged: resume_from_seq == newest + 1 is normal."""
    port = free_tcp_port()
    relay, durable_queue, _ = make_relay(tmp_path, port)
    fill(durable_queue, 5)

    gateway = ScriptedGateway(port, resume_from_seq=5)
    await gateway.start()
    try:
        await run_uplink_briefly(relay, seconds=1.0)
    finally:
        await gateway.stop()
        durable_queue.close()

    assert gateway.hellos, "the handshake did not complete"
    assert gateway.gaps == []


# --- 371: an ack from a stale epoch must delete nothing ---------------------


async def test_an_ack_from_another_epoch_deletes_nothing(tmp_path: Path) -> None:
    """A late ack must not delete records it does not describe.

    relay-v1 §7. The consequence of getting this wrong is silent and permanent:
    records vanish from the relay's queue having never reached the server, and
    nothing anywhere records that it happened.
    """
    port = free_tcp_port()
    relay, durable_queue, _ = make_relay(tmp_path, port)
    fill(durable_queue, 10)

    gateway = ScriptedGateway(port, resume_from_seq=0)
    # An ack for everything, carrying an epoch that is not this queue's.
    gateway.acks_to_send = [
        {"type": "ack", "epoch": "0" * 32, "seq": 9},
        {"type": "ack", "epoch": "deadbeef" * 4, "seq": 9},
    ]
    await gateway.start()
    try:
        await run_uplink_briefly(relay, seconds=1.5)
    finally:
        await gateway.stop()

    depth = durable_queue.depth
    oldest = durable_queue.oldest_seq_held
    durable_queue.close()

    assert depth == 10, "a stale-epoch ack deleted records"
    assert oldest == 0


async def test_an_ack_for_the_current_epoch_does_delete(tmp_path: Path) -> None:
    """The presence half: the same message with the right epoch works."""
    port = free_tcp_port()
    relay, durable_queue, _ = make_relay(tmp_path, port)
    fill(durable_queue, 10)

    gateway = ScriptedGateway(port, resume_from_seq=0)
    gateway.acks_to_send = [{"type": "ack", "epoch": durable_queue.epoch, "seq": 4}]
    await gateway.start()
    try:
        await run_uplink_briefly(relay, seconds=1.5)
    finally:
        await gateway.stop()

    depth = durable_queue.depth
    durable_queue.close()

    assert depth == 5, "a valid ack did not delete anything"


# --- #3: the intake drop path -----------------------------------------------


async def test_a_full_intake_queue_counts_drops_and_keeps_receiving(
    tmp_path: Path,
) -> None:
    """relay-v1 §11 loss #2, which no `gap` can ever describe.

    dropped_intake_total is a status field the Gateway is required to act on,
    and the code that increments it had never run. The relay must keep taking
    datagrams while dropping: blocking intake would lose telemetry it could not
    even count.
    """
    port = free_tcp_port()
    # One slot, so a burst overruns the hand-off almost immediately.
    relay, durable_queue, config = make_relay(tmp_path, port, intake_queue_size=1)

    udp = relay.start_intake()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(4000):
            sender.sendto(b"x" * DATAGRAM_BYTES, ("127.0.0.1", config.bind_port))
        await asyncio.sleep(1.5)
    finally:
        sender.close()
        relay.stop()
        udp.close()

    dropped = durable_queue.dropped_intake_total
    stored = durable_queue.next_seq
    status = relay._status_message()
    durable_queue.close()

    assert dropped > 0, "the intake queue never overflowed; raise the burst size"
    assert stored > 0, "intake stopped accepting datagrams instead of dropping"
    assert status["dropped_intake_total"] == dropped
    assert status["dropped_cap_total"] == 0


async def test_a_quiet_link_reports_no_intake_drops(tmp_path: Path) -> None:
    """The absence half of the pair."""
    port = free_tcp_port()
    relay, durable_queue, config = make_relay(tmp_path, port)

    udp = relay.start_intake()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(20):
            sender.sendto(b"x" * DATAGRAM_BYTES, ("127.0.0.1", config.bind_port))
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
    finally:
        sender.close()
        relay.stop()
        udp.close()

    dropped = durable_queue.dropped_intake_total
    stored = durable_queue.next_seq
    durable_queue.close()

    assert dropped == 0
    assert stored > 0


async def test_intake_drops_do_not_break_the_sequence(tmp_path: Path) -> None:
    """Contiguous seq across a drop is why gap cannot describe this loss."""
    port = free_tcp_port()
    relay, durable_queue, config = make_relay(tmp_path, port, intake_queue_size=1)

    udp = relay.start_intake()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(3000):
            sender.sendto(b"x" * DATAGRAM_BYTES, ("127.0.0.1", config.bind_port))
        await asyncio.sleep(1.5)
    finally:
        sender.close()
        relay.stop()
        udp.close()

    dropped = durable_queue.dropped_intake_total
    held = [r.seq for r in durable_queue.read_from(0, max_bytes=1 << 24)]
    durable_queue.close()

    assert dropped > 0
    assert held == list(range(len(held))), "the sequence has a hole in it"


# --- #4: the reconnect backoff obeys its own documented cap -----------------


async def test_the_jittered_backoff_never_exceeds_the_documented_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """relay-v1 §12 caps the reconnect backoff at 10 s. Jitter must not lift it.

    `backoff * (0.5 + random())` with the backoff already at the cap sleeps for
    up to 15 s - a five-second hole in recovery that nobody budgeted for and
    that the document does not describe. Rule zero: the document is correct and
    the relay has the bug.

    Worst-case jitter is forced rather than hoped for, and the test asserts the
    clamp *fires*. A run where the delay merely happened to land under 10 s
    would prove nothing about the arithmetic that keeps it there.
    """
    relay, durable_queue, _ = make_relay(tmp_path, free_tcp_port())

    # The session is stubbed to fail instantly. Pointing the relay at a closed
    # port would exercise more, but a refused connect costs ~2 s on Windows,
    # and this test is about the loop's arithmetic, not about `connect`.
    async def failing_session() -> None:
        raise OSError("the Gateway is not there")

    monkeypatch.setattr(relay, "_session", failing_session)

    delays: list[float] = []
    enough = asyncio.Event()
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float, *args: Any, **kwargs: Any) -> Any:
        delays.append(delay)
        # Stop once the cap has had its chance to bind; the count is a guard so
        # a relay that never reaches the cap fails on the assertion below
        # rather than hanging here.
        if delay >= BACKOFF_MAX_S or len(delays) > 20:
            enough.set()
        return await real_sleep(0, *args, **kwargs)

    # `agent.relay` calls `random.random()` and `asyncio.sleep()` through the
    # module objects, so patching them here is what the relay sees.
    monkeypatch.setattr(random, "random", lambda: 1.0)
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    task = asyncio.create_task(relay.run_uplink())
    try:
        await asyncio.wait_for(enough.wait(), timeout=10.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        durable_queue.close()

    assert delays, "the uplink never backed off"
    assert max(delays) <= BACKOFF_MAX_S, (
        f"slept for {max(delays)} s; relay-v1 section 12 caps the backoff at "
        f"{BACKOFF_MAX_S} s"
    )
    assert BACKOFF_MAX_S in delays, (
        "the backoff never reached the cap, so the clamp never ran and this "
        "test proved nothing"
    )
