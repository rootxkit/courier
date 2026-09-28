"""Gateway configuration."""

from __future__ import annotations

from pathlib import Path

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

    # P1-04: drone_state is inserted when this many rows are buffered or this
    # long after the first one, whichever comes first.
    state_flush_rows: int = Field(
        default=100, ge=1, validation_alias="STATE_FLUSH_ROWS"
    )
    state_flush_interval_s: float = Field(
        default=0.5, gt=0.0, validation_alias="STATE_FLUSH_INTERVAL_S"
    )

    # Where the raw archive's hourly segments live. Server-side, not on a
    # pilot's laptop: the laptop's disk is protected by the relay's queue cap
    # (P7-11), which is a different mechanism for the same principle.
    archive_root: Path = Field(
        default=Path("/var/lib/courier/archive"), validation_alias="ARCHIVE_ROOT"
    )

    # Ceiling on the archive per station. Retention is normally by age - see
    # `telemetry_retention_days`, which is shared with P1-04 - and this is the
    # bound that applies when a station produces more than expected before the
    # period expires.
    #
    # Bounded by policy, never by disk exhaustion. The relay reports a cap drop
    # to the Gateway as a `gap`; the Gateway has nobody downstream to report to,
    # so the ingest_events row written on deletion is the entire audit trail.
    archive_max_gib_per_station: int = Field(
        default=250, ge=1, validation_alias="ARCHIVE_MAX_GIB_PER_STATION"
    )
