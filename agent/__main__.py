"""Console entry point for the ground relay.

    python -m agent --config relay.toml

A console application on purpose. Windows service packaging is deferred: it is
a distribution problem, and solving it before the relay has been run in anger
would be guessing at what the pilot's machine needs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agent.config import load_config, read_token
from agent.queue import DurableQueue
from agent.relay import RELAY_VERSION, Relay
from common.config import ConfigurationError
from common.logging import bind, configure_logging, get_logger


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description="Ground relay: forward QGC's MAVLink stream to the Gateway.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("relay.toml"),
        help="path to the relay TOML configuration (default: relay.toml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(service="agent", level=args.log_level)
    log = get_logger("agent")

    try:
        config = load_config(args.config)
        token = read_token(config.token_path)
    except ConfigurationError as error:
        # The reader is a pilot on a laptop. A sentence, not a traceback.
        log.error("cannot start", extra={"reason": str(error)})
        return 2

    durable_queue = DurableQueue(config.queue_path, max_bytes=config.queue_max_bytes)
    relay = Relay(config, durable_queue, token)
    bound = bind(log, station_id=config.station_id, epoch=durable_queue.epoch)
    bound.info(
        "relay starting",
        extra={
            "relay_version": RELAY_VERSION,
            "queue_path": str(config.queue_path),
            "queue_depth": durable_queue.depth,
        },
    )

    udp = relay.start_intake()
    try:
        asyncio.run(relay.run_uplink())
    except KeyboardInterrupt:
        bound.info("stopping on interrupt")
    finally:
        relay.stop()
        udp.close()
        durable_queue.close()
        bound.info("relay stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
