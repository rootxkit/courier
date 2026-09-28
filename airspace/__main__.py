"""Run the airspace monitor: `python -m airspace`. P5-06, P5-07.

Reads the separation policy and the zones from the relational database, then
follows the Gateway's telemetry on the bus. Zones are re-read every
`ZONE_REFRESH_S`, so a zone added through the database takes effect without a
restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import nats
from nats.aio.msg import Msg
from sqlalchemy.ext.asyncio import create_async_engine

from airspace.config import AirspaceSettings
from airspace.monitor import AirspaceMonitor
from airspace.policy import load_policy
from airspace.service import AirspaceService, EventsAuditLog
from airspace.zones import load_zones
from common import configure_logging, get_logger, load_settings

_log = get_logger(__name__)

TICK_S = 1.0
ZONE_REFRESH_S = 60.0


async def run(settings: AirspaceSettings) -> None:
    engine = create_async_engine(str(settings.database_url))
    policy = await load_policy(engine)
    monitor = AirspaceMonitor(policy=policy, zones=await load_zones(engine))
    bus = await nats.connect(str(settings.nats_url))
    service = AirspaceService(monitor=monitor, bus=bus, audit=EventsAuditLog(engine))
    _log.info(
        "airspace monitor running",
        extra={
            "t_cpa_max_s": policy.t_cpa_max_s,
            "d_horizontal_min_m": policy.d_horizontal_min_m,
            "d_vertical_min_m": policy.d_vertical_min_m,
            "neighbour_radius_m": policy.neighbour_radius_m,
            "zones": len(monitor.zones),
        },
    )

    async def on_message(message: Msg) -> None:
        await service.on_telemetry(message.data)

    await bus.subscribe("telemetry.*", cb=on_message)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no add_signal_handler; Ctrl+C still raises there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def ticker() -> None:
        since_refresh_s = 0.0
        while not stop.is_set():
            await asyncio.sleep(TICK_S)
            await service.on_tick()
            since_refresh_s += TICK_S
            if since_refresh_s >= ZONE_REFRESH_S:
                since_refresh_s = 0.0
                try:
                    monitor.zones = await load_zones(engine)
                except Exception as error:
                    # Keep the zones we have: a database hiccup must not
                    # silently make every zone disappear.
                    _log.error("could not reload zones", extra={"error": repr(error)})

    task = asyncio.create_task(ticker())
    try:
        await stop.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await bus.drain()
        await engine.dispose()


def main() -> None:
    settings = load_settings(AirspaceSettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
