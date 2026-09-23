"""The relay-v1 server side.

Conformance is `docs/protocols/relay-v1.md` §13, restated with local meaning in
`docs/specs/p1-02-gateway-ingest.md` §4. The obligations, and where each lives:

1. Validate the bearer token on the upgrade, resolve it to a `station_id`  -> `_process_request`
2. Answer `hello` with the true `resume_from_seq` from durable storage       -> `_handshake`
3. Persist before acknowledging                                             -> `_ingest_batch`
4. Cumulative `ack` carrying the epoch, at least once per second            -> `_acknowledge`
5. Deduplicate on `(station_id, epoch, seq)`                                -> the store
6. Record every `gap` as an event, and advance the resume point past it     -> `_handle_gap`
7. Track `dropped_intake_total` deltas between `status` messages            -> `StationLinkTracker`
8. Three missed `status` messages is unreachable, distinct from radio silent -> `StationLinkTracker`
9. Parse MAVLink only after all of the above                                -> not here at all

Obligation 9 is why this module contains no MAVLink whatsoever. The transport
does not need to understand the payload, and mixing the two turns a parsing bug
into a transport failure - the datagram is carried opaquely, exactly as
protocol §6 requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Final, Protocol

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from common import BoundLogger, bind, get_logger
from gateway.ingest_store import IngestStore, StoreError
from gateway.relay_messages import (
    ControlMessageError,
    Gap,
    Hello,
    IgnoredMessage,
    Status,
    build_ack,
    build_welcome,
    parse_control_message,
)
from gateway.relay_records import RecordFramingError, decode_records
from gateway.station_state import LinkState, StationLinkTracker

_log = get_logger(__name__)

# protocol §7: "at least once per second while data is flowing".
ACK_INTERVAL_S: Final = 1.0

# WebSocket close codes. 1008 is "policy violation", which is what a
# protocol-conformance failure is once the connection is already open.
_CLOSE_PROTOCOL_ERROR: Final = 1008

_AUTHORIZATION_SCHEME: Final = "Bearer "


class StationAuthenticator(Protocol):
    """Resolves a presented bearer token to a station identity.

    Token storage, issuance and revocation are spec §12 question 1, still open.
    This interface is the part that is already decided: protocol §3 says the
    token identifies a *ground station*, not a vehicle, and which vehicles a
    station may carry is server policy evaluated elsewhere. A station is never
    trusted to assert what it is carrying.
    """

    async def station_for_token(self, token: str) -> str | None:
        """The station id, or None if the token is unknown or revoked."""
        ...


@dataclass
class RelayServer:
    """Terminates relay-v1 connections from ground stations."""

    store: IngestStore
    authenticator: StationAuthenticator
    host: str = "127.0.0.1"
    port: int = 8081

    ack_interval_s: float = ACK_INTERVAL_S
    unreachable_after_s: float = 3.0
    radio_silent_after_ms: int = 3_000

    def __post_init__(self) -> None:
        self._server: Server | None = None
        self._trackers: dict[str, StationLinkTracker] = {}

    @property
    def trackers(self) -> dict[str, StationLinkTracker]:
        """Live link state per station, for whoever publishes to the console."""
        return self._trackers

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            self.host,
            self.port,
            process_request=self._process_request,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port_in_use(self) -> int:
        """The bound port, which differs from `port` when 0 was requested."""
        if self._server is None:
            raise RuntimeError("the server is not running")
        return int(next(iter(self._server.sockets)).getsockname()[1])

    # --- authentication ---------------------------------------------------

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """Reject a bad credential at the upgrade, with HTTP 401.

        Protocol §3 is specific about the status code, and it is not
        cosmetic: the relay treats `401` as fatal and stops retrying. A
        WebSocket close instead would leave it reconnecting for ever against a
        credential that will never work, turning an operator problem into a log
        flood.

        This exact substitution has already shipped once - a test stub that
        rejected tokens with a close frame meant the relay's fatal-auth path
        never ran while its tests passed - which is why `agent/` now has a test
        that drives a real 401.
        """
        presented = request.headers.get("Authorization")
        if presented is None or not presented.startswith(_AUTHORIZATION_SCHEME):
            return connection.respond(401, "missing bearer token\n")

        token = presented[len(_AUTHORIZATION_SCHEME) :]
        station_id = await self.authenticator.station_for_token(token)
        if station_id is None:
            # The token is deliberately not logged, not even truncated.
            _log.warning(
                "rejected relay connection", extra={"remote": str(request.path)}
            )
            return connection.respond(401, "unknown or revoked token\n")

        # Carried on the connection so the handler does not re-authenticate.
        connection.station_id = station_id  # type: ignore[attr-defined]
        return None

    # --- connection lifecycle ---------------------------------------------

    async def _handle(self, connection: ServerConnection) -> None:
        station_id: str = connection.station_id  # type: ignore[attr-defined]
        log = bind(_log, station_id=station_id)

        try:
            hello = await self._handshake(connection, station_id, log)
        except ControlMessageError as error:
            log.warning("handshake rejected", extra={"error": str(error)})
            await connection.close(_CLOSE_PROTOCOL_ERROR, "bad hello")
            return

        tracker = self._trackers.setdefault(
            station_id,
            StationLinkTracker(
                station_id=station_id,
                unreachable_after_s=self.unreachable_after_s,
                radio_silent_after_ms=self.radio_silent_after_ms,
            ),
        )

        session = _Session(
            server=self,
            connection=connection,
            station_id=station_id,
            epoch=hello.epoch,
            tracker=tracker,
            log=log,
        )
        await session.run()

    async def _handshake(
        self, connection: ServerConnection, station_id: str, log: BoundLogger
    ) -> Hello:
        """Read `hello`, answer `welcome` with the durable resume point."""
        raw = await connection.recv()
        if isinstance(raw, bytes):
            raise ControlMessageError(
                "the first frame must be a text `hello`, got a binary frame; "
                "protocol §5 requires `welcome` before any data frame"
            )

        message = parse_control_message(raw)
        if not isinstance(message, Hello):
            kind = getattr(message, "type", type(message).__name__)
            raise ControlMessageError(f"expected `hello`, got {kind!r}")

        if message.station_id != station_id:
            # The token is the authority on who this is. A station claiming a
            # different id in `hello` is either misconfigured or attempting to
            # write under another station's identity; either way its records
            # would land under a key its credential does not cover.
            raise ControlMessageError(
                f"hello claims station_id {message.station_id!r} but the token "
                f"resolves to {station_id!r}"
            )

        # Declared before the resume point is read, so a station that has
        # recreated its queue has the previous epoch closed first. Reading the
        # watermark first would be harmless today and wrong the moment closing
        # an epoch affects what the watermark means.
        await self.store.open_epoch(station_id, message.epoch)
        resume_from_seq = await self.store.resume_from_seq(station_id, message.epoch)

        # Protocol §11: the server asking for records that never existed is a
        # protocol error the relay will close on, and spec §4 says the Gateway
        # must make it impossible by construction. If it happens, the query is
        # keyed wrongly - two epochs or two stations confused - and shipping
        # the number anyway would put the fault on the relay's side of the log.
        if resume_from_seq > message.newest_seq_held + 1:
            raise ControlMessageError(
                f"computed resume_from_seq={resume_from_seq} exceeds the "
                f"station's newest_seq_held={message.newest_seq_held} + 1 for "
                f"epoch {message.epoch}; the Gateway has confused two epochs "
                f"or two stations"
            )

        await connection.send(build_welcome(resume_from_seq))
        return message


@dataclass
class _Session:
    """One station's connection, after a successful handshake."""

    server: RelayServer
    connection: ServerConnection
    station_id: str
    epoch: str
    tracker: StationLinkTracker
    log: BoundLogger

    def __post_init__(self) -> None:
        # The highest seq durably stored for this epoch, cumulative. -1 means
        # nothing is storable yet, which is distinct from 0 - acknowledging
        # seq 0 would claim a record that may never have arrived.
        self._watermark = -1
        self._acked = -1
        self._last_state: LinkState | None = None

    async def run(self) -> None:
        acker = asyncio.create_task(self._acknowledge_periodically())
        try:
            async for message in self.connection:
                if isinstance(message, bytes):
                    await self._ingest_batch(message)
                else:
                    await self._handle_control(message)
        except websockets.WebSocketException as error:
            self.log.info("relay session ended", extra={"error": str(error)})
        except (StoreError, RecordFramingError, ControlMessageError) as error:
            self.log.error("closing relay session", extra={"error": str(error)})
            with contextlib.suppress(websockets.WebSocketException):
                await self.connection.close(_CLOSE_PROTOCOL_ERROR, str(error)[:120])
        finally:
            acker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await acker
            # A final ack for anything stored since the last tick. The relay
            # survives without it - protocol §5 makes a lost ack cost a
            # retransmission, never a gap - but sending it saves the station
            # resending a batch it will only be told to discard.
            await self._acknowledge()

    async def _ingest_batch(self, frame: bytes) -> None:
        """Store a batch durably. Nothing is acknowledged before this returns."""
        records = decode_records(frame)
        if not records:
            return
        self._watermark = await self.server.store.store_records(
            self.station_id, self.epoch, records
        )

    async def _handle_control(self, payload: str) -> None:
        message = parse_control_message(payload)
        now_s = time.monotonic()

        if isinstance(message, Status):
            for loss in self.tracker.observe_status(message, now_s=now_s):
                await self.server.store.record_loss(self.station_id, self.epoch, loss)
            await self._publish_state_change(now_s, message.utc_ns)
            return

        if isinstance(message, Gap):
            await self._handle_gap(message, now_s)
            return

        if isinstance(message, IgnoredMessage):
            # protocol §14: ignore, do not reject. Counted so that a relay
            # speaking a newer dialect is visible.
            self.tracker.observe_ignored_message()
            return

        # A second `hello` on an open connection. §5 sends one per connection.
        raise ControlMessageError("`hello` received on an established session")

    async def _handle_gap(self, gap: Gap, now_s: float) -> None:
        if gap.epoch != self.epoch:
            raise ControlMessageError(
                f"gap declares epoch {gap.epoch} on a session for {self.epoch}"
            )

        # Recorded before the watermark moves. §11: a recorded gap advances the
        # resume point, and the order matters on a crash - a watermark past a
        # hole with no record of the hole would mean the flight history has a
        # silent discontinuity, which is the one outcome this design exists to
        # prevent. P10-03 replay must render the hole, not interpolate across
        # it: a replay that draws a smooth track through missing data invents
        # evidence.
        await self.server.store.record_gap(self.station_id, self.epoch, gap)
        self.tracker.observe_gap(gap, now_s=now_s)
        self._watermark = await self.server.store.store_records(
            self.station_id, self.epoch, []
        )

    async def _acknowledge_periodically(self) -> None:
        while True:
            await asyncio.sleep(self.server.ack_interval_s)
            await self._acknowledge()

    async def _acknowledge(self) -> None:
        """Send a cumulative ack, if there is anything new to acknowledge."""
        if self._watermark < 0 or self._watermark == self._acked:
            return
        with contextlib.suppress(websockets.WebSocketException):
            await self.connection.send(build_ack(self.epoch, self._watermark))
            self._acked = self._watermark

    async def _publish_state_change(self, now_s: float, at_utc_ns: int) -> None:
        state = self.tracker.state(now_s=now_s)
        if state == self._last_state:
            return
        self._last_state = state
        await self.server.store.record_link_state(
            self.station_id, state, at_utc_ns=at_utc_ns
        )
