"""One of our aircraft heard broadcasting Remote ID is one track. P1-15.

Our aircraft send MAVLink telemetry, and many will also broadcast Remote ID,
as the regulation requires. Without matching they appear twice, under two
ids, and the airspace monitor raises a conflict between an aircraft and
itself. The broadcast serial is matched against `known_drones.serial`, the
projection of the fleet registry's serial numbers.

## What a matched broadcast does

- **While the aircraft's own telemetry is live**, the broadcast is stored,
  with the drone it matched, but not published. The MAVLink track is the
  better one: authenticated through the relay, and several times a second.
- **When its telemetry has gone quiet** (a radio link lost, a relay down),
  the broadcast is published *as that aircraft*: its drone_id, its label,
  still marked `source: remote_id` and unauthenticated. The track does not
  vanish when our link does, and it never becomes two.

Only a serial number is matched (ID type 1). A CAA registration or session
id can move between airframes; matching one would attach a stranger's
broadcast to our aircraft.

## Freshness

The ingest follows `telemetry.*` on the bus and notes when each drone last
sent MAVLink telemetry, ignoring Remote ID observations, its own included. A
drone is live for `live_for_s` after its last message. MAVLink arrives
several times a second, so five seconds of silence is a lost link, and the
broadcast takes over well inside the airspace monitor's 15 s staleness
horizon, before the aircraft would drop out of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway import odid
from gateway.remote_id import SOURCE

_log = get_logger(__name__)

DEFAULT_LIVE_FOR_S = 5.0

_SERIALS = sa.text(
    """
    SELECT serial, drone_id, label FROM known_drones
    WHERE serial IS NOT NULL AND retired_at IS NULL
    """
)


@dataclass(frozen=True, slots=True)
class Registered:
    drone_id: UUID
    label: str


@dataclass
class FleetSerials:
    by_serial: dict[str, Registered] = field(default_factory=dict)

    def match(self, observation: dict[str, Any]) -> Registered | None:
        rid = observation["remote_id"]
        if rid["id_type"] != odid.IdType.SERIAL_NUMBER:
            return None
        return self.by_serial.get(rid["ua_id"])

    async def refresh(self, engine: AsyncEngine) -> None:
        async with engine.connect() as connection:
            rows = (await connection.execute(_SERIALS)).all()
        self.by_serial = {
            str(row.serial): Registered(drone_id=row.drone_id, label=str(row.label))
            for row in rows
        }


@dataclass
class LinkFreshness:
    live_for_s: float = DEFAULT_LIVE_FOR_S
    _heard_s: dict[UUID, float] = field(default_factory=dict, init=False)

    def on_telemetry(self, payload: bytes, *, now_s: float) -> None:
        """A `telemetry.*` message from the bus."""
        try:
            message = json.loads(payload)
            if message.get("source") == SOURCE:
                return
            self._heard_s[UUID(str(message["drone_id"]))] = now_s
        except (ValueError, KeyError, TypeError, AttributeError):
            _log.warning("unreadable telemetry message on the bus")

    def live(self, drone_id: UUID, *, now_s: float) -> bool:
        heard_s = self._heard_s.get(drone_id)
        return heard_s is not None and now_s - heard_s <= self.live_for_s


def as_registered(observation: dict[str, Any], aircraft: Registered) -> dict[str, Any]:
    """The broadcast, published as the aircraft it matched."""
    return {
        **observation,
        "drone_id": str(aircraft.drone_id),
        "label": aircraft.label,
        "remote_id": {**observation["remote_id"], "matched": True},
    }
