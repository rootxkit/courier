"""Core API configuration."""

from __future__ import annotations

from pydantic import Field

from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings


class ApiSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    """Everything the core API needs to start."""

    service_name: str = "api"


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
