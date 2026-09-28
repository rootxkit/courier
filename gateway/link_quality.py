"""Link quality per vehicle: packet loss and heartbeat gaps. P1-09.

Both are read from what already arrives. Nothing is sent to the aircraft,
because Stage 0 is receive-only.

## Packet loss, from the MAVLink sequence number

Every MAVLink frame carries a one-byte sequence number that its sender
increments for each frame it sends. A jump from 17 to 20 means 18 and 19 were
sent and never arrived. The number is read with pymavlink's `get_seq()`, never
from a byte offset (CLAUDE.md: a wrong offset returns plausible nonsense).

What is measured is loss **on the whole path** to the Gateway. That path runs
from the aircraft over the radio, through QGC and the relay, and across the
internet. A loss here does not say which leg dropped the frame. The relay's
drop counters (relay-v1 §8) cover its own leg separately.

Sequence numbers wrap at 256. A jump of more than half the range is treated as
a restart or a reordering, not as 200 lost frames. Counting it as loss would
report a reboot mid-flight as a catastrophic link.

## Heartbeat gaps, from capture time

ArduPilot sends HEARTBEAT at 1 Hz. The longest interval between two
consecutive heartbeats in the window is reported. Capture time is the relay's
receive time, so a gap measures the whole path, not the aircraft's timer.

## Round-trip latency: not measurable at Stage 0

A round trip needs something sent and answered. The Gateway sends nothing
before Phase 3B, so no RTT is reported, rather than an estimate that cannot
be checked. TASKS.md P1-09 records this.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from gateway.classify import HEARTBEAT_ID
from gateway.parsing import ParsedMessage

SEQ_MODULUS = 256
# A forward jump larger than this is a sender restart or reordering, not loss.
MAX_PLAUSIBLE_GAP = SEQ_MODULUS // 2
DEFAULT_WINDOW_S = 10.0


@dataclass(frozen=True, slots=True)
class LinkQuality:
    """One vehicle's link over the last window."""

    window_s: float
    received: int
    lost: int
    heartbeat_gap_max_s: float | None

    @property
    def loss_pct(self) -> float | None:
        """Lost as a share of sent, or None before anything has arrived."""
        sent = self.received + self.lost
        if sent == 0:
            return None
        return 100.0 * self.lost / sent

    def as_dict(self) -> dict[str, float | int | None]:
        loss = self.loss_pct
        return {
            "window_s": self.window_s,
            "received": self.received,
            "lost": self.lost,
            "loss_pct": None if loss is None else round(loss, 2),
            "heartbeat_gap_max_s": (
                None
                if self.heartbeat_gap_max_s is None
                else round(self.heartbeat_gap_max_s, 3)
            ),
        }


@dataclass
class LinkQualityTracker:
    """Tracks one source's frames. One per `(station, source)`, like the
    accumulator: two stations are two links (spec §8)."""

    window_s: float = DEFAULT_WINDOW_S

    # (capture time, received, lost) per observed frame, oldest first.
    _events: deque[tuple[datetime, int, int]] = field(default_factory=deque, init=False)
    # (capture time, gap since the previous heartbeat), oldest first.
    _heartbeat_gaps: deque[tuple[datetime, float]] = field(
        default_factory=deque, init=False
    )
    _last_seq: int | None = field(default=None, init=False)
    _last_heartbeat: datetime | None = field(default=None, init=False)
    _latest: datetime | None = field(default=None, init=False)

    def observe(self, message: ParsedMessage, ts: datetime) -> None:
        seq = int(message.payload.get_seq())
        lost = 0
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) % SEQ_MODULUS
            if gap < MAX_PLAUSIBLE_GAP:
                lost = gap
        self._last_seq = seq
        self._events.append((ts, 1, lost))

        if message.message_id == HEARTBEAT_ID:
            if self._last_heartbeat is not None:
                interval_s = (ts - self._last_heartbeat).total_seconds()
                if interval_s >= 0:
                    self._heartbeat_gaps.append((ts, interval_s))
            self._last_heartbeat = ts

        if self._latest is None or ts > self._latest:
            self._latest = ts
        self._expire()

    def snapshot(self) -> LinkQuality:
        received = sum(event[1] for event in self._events)
        lost = sum(event[2] for event in self._events)
        gaps = [gap for _, gap in self._heartbeat_gaps]
        return LinkQuality(
            window_s=self.window_s,
            received=received,
            lost=lost,
            heartbeat_gap_max_s=max(gaps) if gaps else None,
        )

    def _expire(self) -> None:
        """Drop what is older than the window, measured from the newest
        capture seen - not the wall clock, so a replayed backlog is judged on
        its own timeline."""
        if self._latest is None:
            return
        horizon = self._latest.timestamp() - self.window_s
        while self._events and self._events[0][0].timestamp() < horizon:
            self._events.popleft()
        while self._heartbeat_gaps and self._heartbeat_gaps[0][0].timestamp() < horizon:
            self._heartbeat_gaps.popleft()
