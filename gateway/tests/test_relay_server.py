"""The relay-v1 server side, driven over a real WebSocket on loopback.

Two of these exist because of things that have already shipped broken here:

- The 401 test asserts the HTTP *status code*, not merely that the connection
  failed. A stub that rejected tokens with a WebSocket close once made the
  relay's fatal-auth path untestable while every test passed. `relay-v1.md` §3
  is specific about the code, and the relay's behaviour depends on it.
- The ordering test proves the ack follows the store, rather than assuming it.
  An ack for data still in a buffer turns a Gateway crash into a permanent hole
  in the flight record, and no amount of "it passed" shows the order was right.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
import websockets
from websockets.asyncio.client import connect

from agent.framing import Record as RelayRecord
from agent.framing import encode_records
from gateway.ingest_store import InMemoryIngestStore
from gateway.relay_messages import Gap
from gateway.relay_records import Record
from gateway.relay_server import RelayServer
from gateway.station_state import LinkState

EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"
OTHER_EPOCH = "00112233445566778899aabbccddeeff"
TOKEN = "gateway-test-token"
STATION = "tbilisi-base-1"


class StubAuthenticator:
    """One token, one station. Token policy itself is spec §12 question 1."""

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self.tokens = tokens if tokens is not None else {TOKEN: STATION}

    async def station_for_token(self, token: str) -> str | None:
        return self.tokens.get(token)


@dataclass
class OrderRecordingStore(InMemoryIngestStore):
    """Notes the order of durable writes, so the ack can be placed against it."""

    calls: list[str] = field(default_factory=list)

    async def store_records(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> int:
        if records:
            self.calls.append(f"store:{records[0].seq}-{records[-1].seq}")
        return await super().store_records(station_id, epoch, records)

    async def record_gap(self, station_id: str, epoch: str, gap: Gap) -> None:
        self.calls.append(f"gap:{gap.from_seq}-{gap.to_seq}")
        await super().record_gap(station_id, epoch, gap)


def hello(epoch: str = EPOCH, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "type": "hello",
        "station_id": STATION,
        "epoch": epoch,
        "relay_version": "0.1.0",
        "protocol_version": 1,
        "oldest_seq_held": 0,
        "newest_seq_held": 999_999,
        "monotonic_ns": 1,
        "utc_ns": 2,
    }
    return json.dumps({**body, **overrides})


def status(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "type": "status",
        "queue_depth": 0,
        "queue_bytes": 0,
        "dropped_intake_total": 0,
        "dropped_cap_total": 0,
        "last_datagram_age_ms": 20,
        "uptime_s": 100,
        "monotonic_ns": 1,
        "utc_ns": 1_758_412_800_000_000_000,
    }
    return json.dumps({**body, **overrides})


def batch(first_seq: int, count: int) -> bytes:
    return encode_records(
        [
            RelayRecord(
                seq=first_seq + n,
                recv_utc_ns=1_758_412_800_000_000_000 + n,
                datagram=bytes([n % 256]) * 16,
            )
            for n in range(count)
        ]
    )


@contextlib.asynccontextmanager
async def running(
    store: InMemoryIngestStore | None = None,
    authenticator: StubAuthenticator | None = None,
    **options: Any,
) -> Any:
    server = RelayServer(
        store=store if store is not None else InMemoryIngestStore(),
        authenticator=(
            authenticator if authenticator is not None else StubAuthenticator()
        ),
        host="127.0.0.1",
        port=0,
        **options,
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def url(server: RelayServer) -> str:
    return f"ws://127.0.0.1:{server.port_in_use}/relay/v1"


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def read_until(connection: Any, message_type: str, limit: int = 20) -> Any:
    """Read control messages until one of `message_type` arrives."""
    for _ in range(limit):
        raw = await asyncio.wait_for(connection.recv(), timeout=5.0)
        if isinstance(raw, bytes):
            continue
        payload = json.loads(raw)
        if payload.get("type") == message_type:
            return payload
    raise AssertionError(f"no {message_type!r} within {limit} messages")


# --- authentication --------------------------------------------------------


async def test_a_bad_token_is_rejected_with_http_401() -> None:
    """§3: the status code is load-bearing, not cosmetic.

    The relay treats 401 as fatal and stops retrying. A WebSocket close instead
    would leave it reconnecting for ever against a credential that will never
    work.
    """
    async with running() as server:
        with pytest.raises(websockets.InvalidStatus) as caught:
            async with connect(
                url(server), additional_headers={"Authorization": "Bearer wrong"}
            ):
                pass

    assert caught.value.response.status_code == 401


async def test_a_missing_authorization_header_is_rejected_with_http_401() -> None:
    async with running() as server:
        with pytest.raises(websockets.InvalidStatus) as caught:
            async with connect(url(server)):
                pass

    assert caught.value.response.status_code == 401


async def test_a_valid_token_is_accepted() -> None:
    """The paired presence test. Without it, a server that rejected every
    connection would pass both tests above."""
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        welcome = json.loads(await connection.recv())

    assert welcome["type"] == "welcome"
    assert welcome["protocol_version"] == 1


# --- handshake -------------------------------------------------------------


async def test_an_unknown_epoch_resumes_from_zero() -> None:
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        welcome = json.loads(await connection.recv())

    assert welcome["resume_from_seq"] == 0


async def test_the_resume_point_comes_from_the_store_not_from_memory() -> None:
    """§4.2: a Gateway that restarts must answer the same number.

    The store is pre-loaded here and the server is started fresh against it,
    which is what a restart looks like from the relay's side.
    """
    store = InMemoryIngestStore()
    await store.store_records(
        STATION,
        EPOCH,
        [Record(seq=n, recv_utc_ns=0, datagram=b"x") for n in range(500)],
    )

    async with (
        running(store=store) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        welcome = json.loads(await connection.recv())

    assert welcome["resume_from_seq"] == 500


async def test_a_different_epoch_resumes_from_zero_independently() -> None:
    """§4: the epoch exists so a recreated queue is not mistaken for old data.

    A queue deleted and recreated restarts `seq` at 0. Without the epoch in the
    key, the Gateway would dedupe the new records away as already seen.
    """
    store = InMemoryIngestStore()
    await store.store_records(
        STATION,
        EPOCH,
        [Record(seq=n, recv_utc_ns=0, datagram=b"x") for n in range(500)],
    )

    async with (
        running(store=store) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello(epoch=OTHER_EPOCH))
        welcome = json.loads(await connection.recv())

    assert welcome["resume_from_seq"] == 0


async def test_a_station_id_that_contradicts_the_token_is_refused() -> None:
    """The token is the authority on who this is, not the `hello` body."""
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello(station_id="somebody-elses-base"))
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(connection.recv(), timeout=5.0)


async def test_a_binary_frame_before_hello_is_refused() -> None:
    """§5: `welcome` precedes any data frame."""
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(batch(0, 1))
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(connection.recv(), timeout=5.0)


# --- storing and acknowledging ---------------------------------------------


async def test_records_are_stored_and_then_acknowledged() -> None:
    store = OrderRecordingStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 10))
        ack = await read_until(connection, "ack")

    assert ack == {"type": "ack", "epoch": EPOCH, "seq": 9}
    assert store.calls == ["store:0-9"]
    assert len(store.records[(STATION, EPOCH)]) == 10


async def test_nothing_is_acknowledged_before_it_is_stored() -> None:
    """§4.3, and the ordering an ack promises.

    The store is made slow so that an implementation which acknowledged
    optimistically would send the ack during the delay. The assertion is on the
    observed order, not on the final state, because the final state is the same
    either way.
    """
    order: list[str] = []

    class SlowStore(InMemoryIngestStore):
        async def store_records(
            self, station_id: str, epoch: str, records: list[Record]
        ) -> int:
            if records:
                await asyncio.sleep(0.3)
                order.append("stored")
            return await super().store_records(station_id, epoch, records)

    async with (
        running(store=SlowStore(), ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 4))
        await read_until(connection, "ack")
        order.append("acked")

    assert order == ["stored", "acked"]


async def test_a_retransmitted_batch_is_deduplicated() -> None:
    """§10: at-least-once on the wire, exactly-once after dedupe."""
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 10))
        await read_until(connection, "ack")
        await connection.send(batch(0, 10))
        await connection.send(batch(10, 5))
        ack = await read_until(connection, "ack")

    assert ack["seq"] == 14
    assert len(store.records[(STATION, EPOCH)]) == 15


async def test_the_ack_is_cumulative_across_batches() -> None:
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        for start in (0, 10, 20):
            await connection.send(batch(start, 10))
        ack = await read_until(connection, "ack")
        while ack["seq"] < 29:
            ack = await read_until(connection, "ack")

    assert ack["seq"] == 29


# --- gaps ------------------------------------------------------------------


async def test_a_gap_is_recorded_and_advances_the_resume_point() -> None:
    """§11: without this the watermark sticks at the hole for ever.

    Every later reconnect would ask for records the relay cannot supply, and
    the relay would answer with the same gap again for the life of the epoch.
    """
    store = OrderRecordingStore()

    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 10))
        await read_until(connection, "ack")
        await connection.send(
            json.dumps(
                {
                    "type": "gap",
                    "epoch": EPOCH,
                    "from_seq": 10,
                    "to_seq": 40,
                    "reason": "queue_cap",
                }
            )
        )
        await connection.send(batch(40, 5))
        ack = await read_until(connection, "ack")
        while ack["seq"] < 44:
            ack = await read_until(connection, "ack")

        # A fresh connection sees the advanced resume point.
        async with connect(url(server), additional_headers=auth()) as connection:
            await connection.send(hello())
            welcome = json.loads(await connection.recv())

    assert ack["seq"] == 44
    assert welcome["resume_from_seq"] == 45
    assert store.gaps[(STATION, EPOCH)][0].from_seq == 10
    assert "gap:10-40" in store.calls


async def test_the_gap_is_recorded_before_the_watermark_moves() -> None:
    """Order matters on a crash.

    A watermark past a hole with no record of the hole is a silent
    discontinuity in the flight history - the one outcome this design exists to
    prevent.
    """
    store = OrderRecordingStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(
            json.dumps(
                {
                    "type": "gap",
                    "epoch": EPOCH,
                    "from_seq": 0,
                    "to_seq": 30,
                    "reason": "queue_cap",
                }
            )
        )
        await connection.send(batch(30, 2))
        await read_until(connection, "ack")

    assert store.calls[0] == "gap:0-30"


async def test_a_gap_for_another_epoch_is_refused() -> None:
    async with (
        running(ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(
            json.dumps(
                {
                    "type": "gap",
                    "epoch": OTHER_EPOCH,
                    "from_seq": 0,
                    "to_seq": 10,
                    "reason": "queue_cap",
                }
            )
        )
        with pytest.raises(websockets.ConnectionClosed):
            for _ in range(20):
                await asyncio.wait_for(connection.recv(), timeout=5.0)


# --- status and link state -------------------------------------------------


async def test_an_intake_drop_delta_is_recorded_as_a_loss() -> None:
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(status(dropped_intake_total=0))
        await connection.send(status(dropped_intake_total=40))
        await asyncio.sleep(0.2)

    assert [loss.datagram_count for _, _, loss in store.losses] == [40]


async def test_a_healthy_station_records_no_loss() -> None:
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(status())
        await connection.send(status(uptime_s=101))
        await asyncio.sleep(0.2)

    assert store.losses == []
    assert [state for _, state, _ in store.link_states] == [LinkState.HEALTHY]


async def test_an_unknown_control_message_is_ignored_and_counted() -> None:
    """§14: the connection must survive a message this Gateway does not know."""
    async with (
        running(ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(json.dumps({"type": "from_the_future", "x": 1}))
        await connection.send(batch(0, 3))
        ack = await read_until(connection, "ack")

        assert server.trackers[STATION].ignored_message_count == 1

    assert ack["seq"] == 2
