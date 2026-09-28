"""Stage timings: the instrument P1-10 names the bottleneck with.

Tested in both directions. A summary that never reports is indistinguishable
from a quiet Gateway, and one that reports every batch would become a stage
of its own at the rates being measured.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from gateway.pipeline import IngestPipeline
from gateway.stage_timing import StageTimings
from gateway.tests.test_pipeline import (
    EPOCH,
    STATION,
    FakePublisher,
    FakeResolver,
    FakeWriter,
    heartbeat,
    position,
    record,
)


class Clock:
    def __init__(self) -> None:
        self.now_s = 100.0

    def __call__(self) -> float:
        return self.now_s


def timings(window_s: float = 10.0) -> tuple[StageTimings, Clock]:
    clock = Clock()
    return StageTimings(window_s=window_s, clock=clock), clock


def test_a_measured_stage_accumulates_seconds_and_calls() -> None:
    stages, clock = timings()

    for _ in range(3):
        with stages.measure("process.resolve"):
            clock.now_s += 0.004

    snapshot = stages.snapshot()
    assert snapshot["process_resolve_s"] == pytest.approx(0.012)
    assert snapshot["process_resolve_calls"] == 3


def test_a_stage_that_raises_is_still_timed() -> None:
    stages, clock = timings()

    with pytest.raises(RuntimeError), stages.measure("store"):
        clock.now_s += 2.0
        raise RuntimeError("database gone")

    assert stages.snapshot()["store_s"] == pytest.approx(2.0)


def test_nothing_is_reported_before_the_window_has_elapsed() -> None:
    stages, clock = timings(window_s=10.0)
    stages.count("records", 1240)
    clock.now_s += 9.9

    assert stages.report_if_due() is False
    assert stages.snapshot()["records"] == 1240


class Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_a_window_that_has_elapsed_is_reported_once_and_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The presence half: the summary line actually appears, with its fields.

    Captured on the module's own logger with propagation off, rather than
    through the root logger, where a handler left behind by another test may
    hold a stream that has already been closed.
    """
    logger = logging.getLogger("gateway.stage_timing")
    captured = Captured()
    monkeypatch.setattr(logger, "propagate", False)
    # setLevel, not an attribute patch: the logger caches isEnabledFor per
    # level, and only setLevel clears that cache. Patching `level` left a
    # cached "INFO is off" from an earlier test in force, so this passed or
    # failed depending on which tests had logged through this module first.
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(captured)
    try:
        stages, clock = timings(window_s=10.0)
        with stages.measure("store"):
            clock.now_s += 0.5
        stages.count("records", 1240)
        clock.now_s += 10.0

        assert stages.report_if_due() is True
    finally:
        logger.removeHandler(captured)
        logger.setLevel(previous_level)

    [line] = [r for r in captured.records if r.getMessage() == "ingest stage timings"]
    fields = cast(Any, line)
    assert fields.records == 1240
    assert fields.store_calls == 1
    assert fields.window_s == pytest.approx(10.5)
    assert stages.snapshot() == {}
    assert stages.report_if_due() is False


def test_an_idle_gateway_reports_nothing() -> None:
    stages, clock = timings()
    clock.now_s += 3600.0

    assert stages.report_if_due() is False


async def test_the_pipeline_times_resolve_once_per_batch() -> None:
    """Pins what is being measured.

    Until P1-13, resolution ran once per MAVLink message, so a 1,240-record
    batch cost at least 1,240 database queries and `process_resolve_calls`
    equalled the record count. It now runs once per batch; if that changes
    back, the stage timings will show it first.
    """
    stages, _ = timings()
    writer = FakeWriter()
    pipeline = IngestPipeline(
        station_id=STATION,
        resolver=cast(Any, FakeResolver()),
        writer=cast(Any, writer),
        publisher=cast(Any, FakePublisher()),
        timings=stages,
    )
    batch = [record(0, heartbeat())] + [
        record(n, position(), offset_ns=n * 250_000_000) for n in range(1, 6)
    ]

    await pipeline.process(EPOCH, batch)

    snapshot = stages.snapshot()
    assert snapshot["process_resolve_calls"] == 1
    assert snapshot["process_parse_calls"] == 6
    assert snapshot["process_write_calls"] == 1
    assert snapshot["process_publish_calls"] == 1
