"""Airspace configuration."""

from __future__ import annotations

from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings


class AirspaceSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    """Everything corridor reservation and deconfliction need to start.

    Separation minima, altitude bands and alert thresholds are not here. They
    are airspace policy, they are edited by operators, and they belong in the
    database where a change is audited (P5-01, P5-03).
    """

    service_name: str = "airspace"
