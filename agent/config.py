"""Agent configuration.

At Stage 0 the agent is the ground relay: it reads the MAVLink stream QGC
forwards on loopback and pushes it to the Gateway. It has no database and no
bus — it is deliberately the simplest thing on the ground PC, because it runs
on a machine nobody administers.
"""

from __future__ import annotations

from pydantic import AnyWebsocketUrl, Field

from common import ServiceSettings


class AgentSettings(ServiceSettings):
    """Everything the relay needs to start."""

    service_name: str = "agent"

    # The local socket QGC forwards to; see ARCHITECTURE.md §2.
    mavlink_listen_host: str = Field(
        default="127.0.0.1", validation_alias="MAVLINK_LISTEN_HOST"
    )
    mavlink_listen_port: int = Field(
        default=14445, ge=1, le=65535, validation_alias="MAVLINK_LISTEN_PORT"
    )

    # Where telemetry goes, and the credential that authenticates this
    # vehicle's stream. Both are required: an unauthenticated relay is how a
    # spoofed SYSID gets into the fleet view (P1-07).
    gateway_url: AnyWebsocketUrl = Field(validation_alias="GATEWAY_URL")
    gateway_token: str = Field(min_length=1, validation_alias="GATEWAY_TOKEN")

    # Disk-backed queue that replays after an internet dropout (P1-01).
    queue_path: str = Field(default="./relay-queue", validation_alias="QUEUE_PATH")
