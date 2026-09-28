"""Live drone state in Redis, where expiry *is* "link lost". P1-05.

`drone:{drone_id}:state` holds the newest `drone_state` row for a drone. It
expires `link_timeout_s` after that row was **captured**, so the key's
existence is the drone's live status and no process has to decide it.

## Why expiry counts from capture time, not arrival

A relay replays its backlog after an outage. If expiry counted from arrival, a
ten-minute-old position arriving now would make an aircraft whose link died
ten minutes ago look live for another fifteen seconds, with a position that
is ten minutes stale. That is exactly what live state must never say.

So a key expires at `capture_time + link_timeout_s`, capped at
`now + link_timeout_s`:

- **A replayed backlog** captured more than `link_timeout_s` ago expires on
  arrival. It never makes a drone look live.
- **An older record never overwrites a newer one.** This happens when two
  stations relay one aircraft (spec §8) or a backlog interleaves with live
  data. The update is a compare-and-set on the capture time, run in Redis so
  that two writers cannot interleave.
- **A station clock running behind** makes records look old, so the key
  expires early. The result is a false "link lost", never stale state shown
  as live. Which way to fail was chosen on purpose. Correcting station clocks
  is spec §12 question 4, still open. When it is answered, this is one of the
  places that should use the corrected time.
- **A station clock running ahead** is held by the cap: a record from the
  future still expires `link_timeout_s` from now.

## Failure

Redis holds a cache of truth that lives elsewhere: `drone_state` and the
archive. Like the bus, a Redis failure must never stall ingest. The pipeline
catches and logs it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from gateway.drone_state import DroneStateRow
from gateway.publisher import encode_row

KEY_PREFIX = "drone"

# KEYS[1] the state key; ARGV: capture ms, state JSON, expire-at ms, now ms.
# Returns 1 when written, 0 when an equal or newer capture is already held,
# -1 when the record was already too old to be live and nothing was written.
_COMPARE_AND_SET = """
local held = redis.call('HGET', KEYS[1], 'ts_ms')
if held and tonumber(held) >= tonumber(ARGV[1]) then
  return 0
end
if tonumber(ARGV[3]) <= tonumber(ARGV[4]) then
  return -1
end
redis.call('HSET', KEYS[1], 'ts_ms', ARGV[1], 'state', ARGV[2])
redis.call('PEXPIREAT', KEYS[1], ARGV[3])
return 1
"""


def state_key(drone_id: UUID) -> str:
    return f"{KEY_PREFIX}:{drone_id}:state"


async def read_live_state(redis: Redis, drone_id: UUID) -> dict[str, Any] | None:
    """A drone's live state, or None when its link is lost.

    Needs no timeout: expiry was set when the state was written, so an absent
    key already means "not live". The API reads through this (P2-05) without
    constructing a writer.
    """
    held = await redis.hgetall(state_key(drone_id))
    if not held:
        return None
    state = held.get(b"state")
    if state is None:
        return None
    decoded: dict[str, Any] = json.loads(state)
    return decoded


@dataclass
class LiveState:
    """Writes the newest row per drone, and answers whether a drone is live."""

    redis: Redis
    link_timeout_s: float
    clock: Callable[[], float] = time.time

    _script: Any = field(init=False)

    def __post_init__(self) -> None:
        self._script = self.redis.register_script(_COMPARE_AND_SET)

    async def update(
        self, rows: list[DroneStateRow], labels: dict[UUID, str] | None = None
    ) -> dict[UUID, int]:
        """Offer the newest row of each drone in a batch.

        Returns the script's verdict per drone: 1 written, 0 superseded, -1
        too old to be live. The verdicts are returned so that a test can tell
        "written" from "silently ignored".
        """
        labels = labels or {}
        newest: dict[UUID, DroneStateRow] = {}
        for row in rows:
            held = newest.get(row.drone_id)
            if held is None or row.ts > held.ts:
                newest[row.drone_id] = row

        now_ms = int(self.clock() * 1000)
        ttl_ms = int(self.link_timeout_s * 1000)
        verdicts: dict[UUID, int] = {}
        for drone_id, row in newest.items():
            captured_ms = int(row.ts.timestamp() * 1000)
            expire_at_ms = min(captured_ms + ttl_ms, now_ms + ttl_ms)
            state = json.dumps(encode_row(row, labels.get(drone_id)))
            verdicts[drone_id] = int(
                await self._script(
                    keys=[state_key(drone_id)],
                    args=[captured_ms, state, expire_at_ms, now_ms],
                )
            )
        return verdicts

    async def get(self, drone_id: UUID) -> dict[str, Any] | None:
        """The drone's live state, or None when its link is lost."""
        return await read_live_state(self.redis, drone_id)

    async def is_live(self, drone_id: UUID) -> bool:
        return await self.get(drone_id) is not None
