"""The console feed serves only a browser holding a valid ticket. P6-08.

Over a real socket: the app runs under uvicorn on a free port and a
`websockets` client connects to it, sending the ticket as a browser does, in
a cookie. No broker: the app starts against a dead NATS port, as in
`test_console_snapshot.py`, and messages are put on its hub directly. What
is under test is who receives them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from api.auth import FEED_COOKIE, Operator, Role, issue_feed_ticket
from api.telemetry_ws import CLOSE_SIGN_IN_REQUIRED, create_app
from tests.ports import free_tcp_port

SECRET = b"console-feed-test-secret-0123456789ab"
VIEWER = Operator(id=uuid4(), username="v", display_name="V", role=Role.VIEWER)


class Clock:
    def __init__(self, now_s: float) -> None:
        self.now_s = now_s

    def __call__(self) -> float:
        return self.now_s


@contextlib.asynccontextmanager
async def serving(clock: Clock) -> AsyncIterator[tuple[FastAPI, str]]:
    app = create_app(
        f"nats://127.0.0.1:{free_tcp_port()}",
        feed_secret=SECRET,
        connect_timeout_s=0.2,
        clock_s=clock,
    )
    port = free_tcp_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "the console never started"
        yield app, f"ws://127.0.0.1:{port}/ws/telemetry"
    finally:
        server.should_exit = True
        await task


def cookie(value: str) -> dict[str, str]:
    return {"Cookie": f"{FEED_COOKIE}={value}"}


def ticket(now_s: float, ttl_s: float = 60.0, secret: bytes = SECRET) -> str:
    return issue_feed_ticket(secret, VIEWER, now_s=now_s, ttl_s=ttl_s)


async def wait_until_attached(app: FastAPI) -> None:
    """The handler attaches to the hub only after the ticket has passed."""
    for _ in range(500):
        if app.state.hub.clients:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never attached")


async def closed_code(url: str, headers: dict[str, str]) -> int | None:
    async with connect(url, additional_headers=headers) as socket:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(socket.recv(), timeout=5)
    return closed.value.rcvd.code if closed.value.rcvd else None


async def test_no_ticket_is_closed_with_sign_in_required() -> None:
    async with serving(Clock(1000.0)) as (_, url):
        assert await closed_code(url, {}) == CLOSE_SIGN_IN_REQUIRED


async def test_a_forged_ticket_is_closed_too() -> None:
    async with serving(Clock(1000.0)) as (_, url):
        forged = ticket(1000.0, secret=b"f" * 40)
        assert await closed_code(url, cookie(forged)) == CLOSE_SIGN_IN_REQUIRED


async def test_a_valid_ticket_receives_the_feed() -> None:
    """The paired presence: same app, same message, a real ticket."""
    async with (
        serving(Clock(1000.0)) as (app, url),
        connect(url, additional_headers=cookie(ticket(1000.0))) as socket,
    ):
        await wait_until_attached(app)
        app.state.hub.broadcast("telemetry.abc", json.dumps({"n": 1}).encode())
        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))

    assert message == {"kind": "telemetry", "name": "abc", "data": {"n": 1}}


async def test_the_feed_closes_when_the_ticket_runs_out() -> None:
    """How a revoked session loses the feed: its ticket is not renewed."""
    clock = Clock(1000.0)
    async with (
        serving(clock) as (app, url),
        connect(url, additional_headers=cookie(ticket(1000.0, ttl_s=0.3))) as socket,
    ):
        # Admitted first: this is expiry, not a refusal at the door.
        await wait_until_attached(app)
        clock.now_s = 1000.5
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(socket.recv(), timeout=5)

    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == CLOSE_SIGN_IN_REQUIRED
