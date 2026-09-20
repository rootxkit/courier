"""Dispatch configuration."""

from __future__ import annotations

from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings


class DispatchSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    """Everything the assignment engine needs to start.

    Scoring weights and the energy reserve are deliberately absent. They are
    operational parameters that change without a deploy, so they live in the
    database alongside the rest of the tunable configuration (P4-05). The
    35% reserve in particular is never a value a process starts up with.
    """

    service_name: str = "dispatch"
