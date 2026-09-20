"""Shared logging and configuration library.

Every service depends on this package. Nothing here may import a service.
"""

from common.config import (
    ConfigurationError,
    Environment,
    LogLevel,
    NatsSettings,
    PostgresSettings,
    RedisSettings,
    ServiceSettings,
    Settings,
    TelemetryDatabaseSettings,
    load_settings,
)
from common.logging import BoundLogger, bind, configure_logging, get_logger

__all__ = [
    "BoundLogger",
    "ConfigurationError",
    "Environment",
    "LogLevel",
    "NatsSettings",
    "PostgresSettings",
    "RedisSettings",
    "ServiceSettings",
    "Settings",
    "TelemetryDatabaseSettings",
    "bind",
    "configure_logging",
    "get_logger",
    "load_settings",
]
