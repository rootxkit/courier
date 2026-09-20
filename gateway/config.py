"""Gateway configuration."""

from __future__ import annotations

from pydantic import Field

from common import (
    NatsSettings,
    RedisSettings,
    ServiceSettings,
    TelemetryDatabaseSettings,
)


class GatewaySettings(
    ServiceSettings, TelemetryDatabaseSettings, RedisSettings, NatsSettings
):
    """Everything the Gateway needs to start.

    The Gateway writes telemetry to TimescaleDB, live state to Redis, and
    publishes to NATS. It does not touch the relational database.
    """

    service_name: str = "gateway"

    # UDP socket the MAVLink stream arrives on. Whatever is on the other end —
    # QGC forwarding at Stage 0, mavlink-router at Stage 1, an onboard agent at
    # Stage 2 — is deliberately not this service's concern.
    mavlink_bind_host: str = Field(
        default="0.0.0.0", validation_alias="MAVLINK_BIND_HOST"
    )
    mavlink_bind_port: int = Field(
        default=14445, ge=1, le=65535, validation_alias="MAVLINK_BIND_PORT"
    )

    # A vehicle whose state has not been refreshed within this window is
    # treated as link-lost; it matches the Redis TTL in ARCHITECTURE.md §1.
    link_timeout_s: float = Field(
        default=15.0, gt=0.0, validation_alias="LINK_TIMEOUT_S"
    )
