"""Core API configuration."""

from __future__ import annotations

from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings


class ApiSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    """Everything the core API needs to start."""

    service_name: str = "api"
