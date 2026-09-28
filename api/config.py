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
