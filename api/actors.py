"""Who did something, as the audit log records it. P2-06, P6-08.

Every `events` row names an actor. Before operator authentication existed
every API change was attributed to the API itself (`SYSTEM`); with P6-08 a
change made through the API is attributed to the signed-in operator.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Actor:
    actor_type: str
    actor_id: str | None = None


# A change with no person behind it: a script, a migration, a test.
SYSTEM = Actor("api")
