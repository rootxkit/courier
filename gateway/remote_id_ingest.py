"""Remote ID ingest: receiver datagrams in, aircraft on the bus out. P1-15.

    python -m gateway.remote_id_ingest

Receivers send one UDP datagram per Open Drone ID message or message pack
they hear, as JSON:

    {"receiver_id": "rx-tbilisi-1", "transmitter": "AA:BB:CC:DD:EE:FF",
     "payload_hex": "12...", "rssi_dbm": -71}

`payload_hex` is the message exactly as broadcast, so this one decoder
serves every kind of receiver; an adapter for a receiver that decodes on its
own (a commercial unit's MQTT, say) re-encodes to this rather than adding a
second decoder. Each completed observation is published as
`telemetry.<aircraft id>`, beside the Gateway's MAVLink telemetry.

## What this does not do yet

- **Receivers are not authenticated.** The socket binds to 127.0.0.1 by
  default, so only a process on this host can feed it. Exposing it means
  deciding how a receiver proves itself, as the relay does for stations
  (relay-v1); until then a datagram is trusted as much as the broadcast it
  claims to carry, which is not at all.
- **Nothing is stored.** Remote ID aircraft are on the map and in the
  airspace monitor, and their alerts are in the audit log, but their tracks
  are not in the telemetry database, so replay does not have them.
- **An aircraft seen both ways is two tracks.** Matching a broadcast serial
  to a registered aircraft is the rest of P1-15.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import nats

from common import configure_logging, get_logger, load_settings
from common.geoid import GeoidGrid
from gateway import odid
from gateway.config import RemoteIdSettings
from gateway.publisher import Bus
from gateway.remote_id import Frame, Geoid, RemoteIdTracker

_log = get_logger(__name__)

MAX_DATAGRAM_BYTES = 4096
REQUIRED_FIELDS = ("receiver_id", "transmitter", "payload_hex")


class DatagramError(ValueError):
    """A datagram that is not a receiver report."""


def parse_datagram(data: bytes, *, received_at: datetime) -> Frame:
    if len(data) > MAX_DATAGRAM_BYTES:
        raise DatagramError(f"{len(data)} bytes, more than {MAX_DATAGRAM_BYTES}")
    try:
        report = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatagramError(f"not JSON: {error}") from error
    if not isinstance(report, dict):
        raise DatagramError("not a JSON object")
    for name in REQUIRED_FIELDS:
        if not isinstance(report.get(name), str) or not report[name]:
            raise DatagramError(f"missing {name}")
    try:
        payload = bytes.fromhex(report["payload_hex"])
    except ValueError as error:
        raise DatagramError("payload_hex is not hex") from error
    rssi = report.get("rssi_dbm")
    if rssi is not None and (
        isinstance(rssi, bool) or not isinstance(rssi, int | float)
    ):
        raise DatagramError("rssi_dbm is not a number")
    return Frame(
        receiver_id=report["receiver_id"],
        transmitter=report["transmitter"],
        # The ingest's clock, not the receiver's: receivers are not trusted
        # to keep time, and the monitor compares aircraft by arrival.
        received_at=received_at,
        payload=payload,
        rssi_dbm=None if rssi is None else float(rssi),
    )


def wall_clock() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class RemoteIdIngest:
    tracker: RemoteIdTracker
    bus: Bus
    clock_s: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = wall_clock
    refused: int = field(default=0, init=False)
    published: int = field(default=0, init=False)

    async def on_datagram(self, data: bytes, source: str) -> None:
        try:
            frame = parse_datagram(data, received_at=self.wall())
            observation = self.tracker.take(frame, now_s=self.clock_s())
        except (DatagramError, odid.DecodeError) as error:
            self.refused += 1
            _log.warning(
                "remote id datagram refused",
                extra={"source": source, "error": str(error)},
            )
            return
        if observation is None:
            return
        try:
            await self.bus.publish(
                f"telemetry.{observation['drone_id']}",
                json.dumps(observation).encode("utf-8"),
            )
        except Exception as error:
            _log.error(
                "could not publish a remote id observation",
                extra={"drone_id": observation["drone_id"], "error": repr(error)},
            )
            return
        self.published += 1


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, ingest: RemoteIdIngest) -> None:
        self.ingest = ingest
        self.tasks: set[asyncio.Task[None]] = set()

    def datagram_received(self, data: bytes, addr: tuple[str | object, ...]) -> None:
        task = asyncio.create_task(self.ingest.on_datagram(data, str(addr[0])))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


async def listen(
    ingest: RemoteIdIngest, host: str, port: int
) -> asyncio.DatagramTransport:
    """Bind the receiver socket; every datagram goes to `ingest`."""
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: _Protocol(ingest), local_addr=(host, port)
    )
    return transport


def load_geoid(path: Path | None) -> Geoid | None:
    if path is None:
        _log.warning(
            "no geoid model configured; Remote ID aircraft will have no AMSL "
            "altitude and the airspace monitor will not evaluate them"
        )
        return None
    return GeoidGrid.load(path)


async def run(settings: RemoteIdSettings) -> None:
    bus = await nats.connect(str(settings.nats_url))
    ingest = RemoteIdIngest(
        tracker=RemoteIdTracker(geoid=load_geoid(settings.geoid_path)), bus=bus
    )
    transport = await listen(
        ingest, settings.remote_id_bind_host, settings.remote_id_bind_port
    )
    _log.info(
        "remote id ingest running",
        extra={
            "host": settings.remote_id_bind_host,
            "port": settings.remote_id_bind_port,
            "geoid": str(settings.geoid_path) if settings.geoid_path else None,
        },
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        transport.close()
        await bus.drain()


def main() -> None:
    settings = load_settings(RemoteIdSettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
