"""Where ingest time goes, stage by stage. P1-10.

The capacity measurement established *that* the Gateway stores about 290
records/s whatever the fleet size - one 64 KiB batch of about 1,240 records
every four seconds - but not *where* those four seconds go. P1-10 requires the
bottleneck to be named from a measurement, so each stage of a stored batch is
timed here and summarised in one log line per window.

Cheap enough to leave on: a `perf_counter` pair per stage, dictionary updates,
and one log line every `window_s`. Nothing is logged per batch or per record,
because at the rates in question that would itself become a stage.

Stages nest. `process` contains `process.resolve`, `process.write` and the
rest, so the top-level stages sum to roughly the time spent storing batches,
and the nested ones say what `process` is made of. `calls` matters as much as
seconds: a stage called once per message rather than once per batch shows up
there first.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from common import get_logger

_log = get_logger(__name__)

DEFAULT_WINDOW_S = 10.0
MAX_SAMPLES_PER_WINDOW = 10_000


def percentile(ordered: list[float], rank: float) -> float:
    """Nearest-rank percentile of an already sorted, non-empty list."""
    index = max(0, min(len(ordered) - 1, math.ceil(rank / 100 * len(ordered)) - 1))
    return ordered[index]


@dataclass
class StageTimings:
    """Accumulates seconds and calls per stage, and reports once per window."""

    window_s: float = DEFAULT_WINDOW_S
    clock: Callable[[], float] = time.perf_counter
    # Stages whose every call is kept, for percentiles. Opt-in, because a
    # per-message stage such as `process.parse` runs thousands of times a
    # window and keeping each call would become a cost of its own. P1-04's
    # criterion is a p99 on the drone_state insert, hence the default.
    sampled: frozenset[str] = frozenset({"state.insert"})

    _samples: dict[str, list[float]] = field(default_factory=dict, init=False)
    _seconds: dict[str, float] = field(default_factory=dict, init=False)
    _calls: dict[str, int] = field(default_factory=dict, init=False)
    _counts: dict[str, int] = field(default_factory=dict, init=False)
    _window_started: float | None = field(default=None, init=False)

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        """Time the body as one call of `stage`, including when it raises."""
        started = self.clock()
        # The window opens when the first measured stage *starts*. Opening it
        # at the first `add` - when that stage ends - made a window shorter
        # than the time it accounts for, so its shares summed past 100%.
        if self._window_started is None:
            self._window_started = started
        try:
            yield
        finally:
            self.add(stage, self.clock() - started)

    def add(self, stage: str, seconds: float) -> None:
        if self._window_started is None:
            self._window_started = self.clock()
        self._seconds[stage] = self._seconds.get(stage, 0.0) + seconds
        self._calls[stage] = self._calls.get(stage, 0) + 1
        if stage in self.sampled:
            samples = self._samples.setdefault(stage, [])
            if len(samples) < MAX_SAMPLES_PER_WINDOW:
                samples.append(seconds)

    def count(self, name: str, amount: int) -> None:
        """Count something that is not a duration, such as records stored."""
        if self._window_started is None:
            self._window_started = self.clock()
        self._counts[name] = self._counts.get(name, 0) + amount

    def snapshot(self) -> dict[str, float | int]:
        """The current window as flat log fields, without resetting it."""
        fields: dict[str, float | int] = {}
        if self._window_started is not None:
            fields["window_s"] = round(self.clock() - self._window_started, 3)
        for stage in sorted(self._seconds):
            key = stage.replace(".", "_")
            fields[f"{key}_s"] = round(self._seconds[stage], 4)
            fields[f"{key}_calls"] = self._calls[stage]
        for stage in sorted(self._samples):
            key = stage.replace(".", "_")
            ordered = sorted(self._samples[stage])
            fields[f"{key}_p50_ms"] = round(1000 * percentile(ordered, 50), 2)
            fields[f"{key}_p99_ms"] = round(1000 * percentile(ordered, 99), 2)
            fields[f"{key}_max_ms"] = round(1000 * ordered[-1], 2)
        for name in sorted(self._counts):
            fields[name] = self._counts[name]
        return fields

    def samples(self, stage: str) -> list[float]:
        """The current window's samples for a sampled stage, in seconds.

        Logged alongside the percentiles by `report_if_due`, because a p99 of
        one window's twenty inserts is just its maximum; a p99 worth stating
        is computed over a whole run, and that needs the samples.
        """
        return list(self._samples.get(stage, []))

    def report_if_due(self) -> bool:
        """Log and reset the window once it has lasted `window_s`.

        Returns whether it reported, so a test can tell a quiet window from a
        broken one.
        """
        if self._window_started is None:
            return False
        if self.clock() - self._window_started < self.window_s:
            return False
        fields: dict[str, object] = dict(self.snapshot())
        for stage, values in self._samples.items():
            key = f"{stage.replace('.', '_')}_samples_ms"
            fields[key] = [round(1000 * value, 3) for value in values]
        _log.info("ingest stage timings", extra=fields)
        self._samples.clear()
        self._seconds.clear()
        self._calls.clear()
        self._counts.clear()
        self._window_started = None
        return True


# One per process. The stages span the relay server, the store and the
# pipeline, and a single summary line is what makes their proportions
# readable; components take it as a default and tests pass their own.
INGEST_TIMINGS = StageTimings()


def shared_timings() -> StageTimings:
    return INGEST_TIMINGS
