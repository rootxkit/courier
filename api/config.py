"""Core API configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings
from common.config import TelemetryDatabaseSettings


class ApiSettings(
    ServiceSettings,
    PostgresSettings,
    TelemetryDatabaseSettings,
    RedisSettings,
    NatsSettings,
):
    """Everything the core API needs to start.

    The telemetry database too: registering a drone writes its projection
    there (P2-05), because the Gateway cannot read this service's database.
    """

    service_name: str = "api"

    # Loopback by default: there is no operator authentication yet.
    api_host: str = Field(default="127.0.0.1", validation_alias="API_HOST")
    api_port: int = Field(default=8010, ge=1, le=65535, validation_alias="API_PORT")

    # P10-03. The replay page draws the same base map as the console.
    basemap_dir: Path = Field(
        default=Path("local/basemap"), validation_alias="BASEMAP_DIR"
    )
    # P10-03. Two consecutive samples further apart than this are a hole in
    # the track, drawn as one and never joined by a line. Not a statement
    # about any stream rate (spec §6.4): only the point past which a straight
    # segment would be inventing a path.
    replay_gap_threshold_s: float = Field(
        default=3.0, gt=0, validation_alias="REPLAY_GAP_THRESHOLD_S"
    )
    # How far apart in time a hole and a logged cause may be and still be
    # matched. Covers a loss detected at the next `status` and clocks that
    # differ between a station and the Gateway.
    replay_evidence_slack_s: float = Field(
        default=5.0, ge=0, validation_alias="REPLAY_EVIDENCE_SLACK_S"
    )
    # Armed telemetry silent for longer than this is two flights, not one.
    replay_flight_split_s: float = Field(
        default=120.0, gt=0, validation_alias="REPLAY_FLIGHT_SPLIT_S"
    )
    # A window with more rows than this is refused, not thinned.
    replay_max_samples: int = Field(
        default=100_000, gt=0, validation_alias="REPLAY_MAX_SAMPLES"
    )


class ConsoleSettings(ServiceSettings, NatsSettings):
    """The P1-08 console feed.

    Only the bus, deliberately. The console is a NATS subscriber and must not
    reach the database: a browser refresh becoming a hypertable query is the
    thing this design exists to prevent, and a service that cannot connect to
    the database cannot accidentally start doing so.
    """

    service_name: str = "console"

    console_host: str = Field(default="127.0.0.1", validation_alias="CONSOLE_HOST")
    console_port: int = Field(
        default=8000, ge=1, le=65535, validation_alias="CONSOLE_PORT"
    )
    # P1-12. Where `infra/basemap/fetch_basemap.sh` put the base map. Per
    # machine and never committed; relative paths are from the working
    # directory the console is started in.
    basemap_dir: Path = Field(
        default=Path("local/basemap"), validation_alias="BASEMAP_DIR"
    )
