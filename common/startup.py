"""The first thing every service does.

Configuration is validated before logging is configured, so a bad environment
fails with a plain exception rather than a half-initialised process, and
logging is configured before anything else runs, so no line is lost.
"""

from __future__ import annotations

from common.config import ServiceSettings, load_settings
from common.logging import BoundLogger, bind, configure_logging, get_logger

__all__ = ["start_service"]


def start_service[SettingsT: ServiceSettings](
    settings_class: type[SettingsT],
) -> tuple[SettingsT, BoundLogger]:
    """Load configuration, install JSON logging, announce the start.

    Returns the validated settings and a logger already carrying the service
    name and environment, so callers never have to repeat them.

    Raises ConfigurationError if the environment is missing or invalid.
    """
    settings = load_settings(settings_class)
    configure_logging(service=settings.service_name, level=settings.log_level)

    log = bind(
        get_logger(settings.service_name),
        env=settings.env.value,
    )
    log.info("service starting")
    return settings, log
