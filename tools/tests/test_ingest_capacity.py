"""The capacity harness, including the check it runs on itself.

Two things here matter more than the arithmetic.

The counters are read back from a queue built by `DurableQueue` itself, never
from a hand-written schema. A measurement that reads `next_seq` from the wrong
place, or from a column that has been renamed, does not fail - it reports a
plausible rate. That is the wire-offset lesson in a different medium.

And `check_instrument` is exercised in both directions. It exists to say "these
numbers are void", so a version of it that could never say so would be worse
than not having it: every future run would carry a silent endorsement.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.queue import DurableQueue
from tests.ports import free_tcp_port
from tools.ingest_capacity import (
    MeasurementError,
    PhaseResult,
    Run,
    Sample,
    SeverableProxy,
    after_settling,
    check_instrument,
    read_relay_counters,
    summarise,
    summarise_run,
)

# --- reading the relay's counters ------------------------------------------


def a_queue(path: Path, datagrams: int) -> DurableQueue:
    queue = DurableQueue(path=path, max_bytes=10 * 1024 * 1024)
    if datagrams:
        queue.append(
            [(1_700_000_000_000_000_000 + n, b"\xfd" * 40) for n in range(datagrams)]
        )
    return queue


def test_the_counters_are_read_from_a_queue_the_relay_wrote(tmp_path: Path) -> None:
    """Built by `DurableQueue`, so the harness cannot drift from the schema."""
    path = tmp_path / "queue.sqlite3"
    with a_queue(path, 250) as queue:
        expected_next_seq = queue.next_seq
        expected_depth = queue.depth
        expected_bytes = queue.total_bytes

        counters = read_relay_counters(path)

    assert counters.next_seq == expected_next_seq == 250
    assert counters.depth == expected_depth == 250
    assert counters.queued_bytes == expected_bytes
    assert counters.dropped_intake == 0
    assert counters.dropped_cap == 0


def test_next_seq_counts_intake_and_does_not_fall_on_acknowledgement(
    tmp_path: Path,
) -> None:
    """The property the whole intake measurement rests on.

    `depth` falls when records are acknowledged and deleted; `next_seq` must
    not, or intake would be undercounted by exactly the amount that drained -
    which would make drain and intake look equal at any load.
    """
    path = tmp_path / "queue.sqlite3"
    with a_queue(path, 100) as queue:
        queue.acknowledge(79)

        counters = read_relay_counters(path)

    assert counters.next_seq == 100
    assert counters.depth == 20


def test_a_queue_that_grows_after_acknowledgement_still_counts_up(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    with a_queue(path, 50) as queue:
        queue.acknowledge(49)
        queue.append([(1, b"\xfd" * 10)])

        counters = read_relay_counters(path)

    assert counters.next_seq == 51
    assert counters.depth == 1


def test_a_database_that_is_not_a_relay_queue_is_refused(tmp_path: Path) -> None:
    """The paired failure: pointed at the wrong file, say so rather than read 0."""
    path = tmp_path / "not-a-queue.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.commit()
    connection.close()

    with pytest.raises(MeasurementError, match="next_seq"):
        read_relay_counters(path)


def test_reading_the_queue_cannot_write_to_it(tmp_path: Path) -> None:
    """Opened `mode=ro`, because the queue may be the only copy of the data.

    A harness that measures flight data must not be able to damage it, and
    "we only ever run selects" is a property of today's code, not of the file
    handle.
    """
    path = tmp_path / "queue.sqlite3"
    with a_queue(path, 10):
        pass

    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM records")
    finally:
        connection.close()


# --- the severable proxy ---------------------------------------------------


async def echo_server(port: int) -> asyncio.AbstractServer:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            chunk = await reader.read(1024)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", port)


async def test_the_proxy_forwards_while_it_is_whole() -> None:
    """The presence test. Without it, a proxy that forwards nothing would make
    every outage look perfect and every recovery look instant."""
    upstream_port = free_tcp_port()
    upstream = await echo_server(upstream_port)
    proxy = SeverableProxy(listen_port=free_tcp_port(), gateway_port=upstream_port)
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.listen_port)
        writer.write(b"hello relay")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(11), timeout=5.0)
        writer.close()
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert echoed == b"hello relay"
    assert proxy.accepted == 1


async def test_a_severed_proxy_refuses_a_new_connection() -> None:
    upstream_port = free_tcp_port()
    upstream = await echo_server(upstream_port)
    proxy = SeverableProxy(listen_port=free_tcp_port(), gateway_port=upstream_port)
    await proxy.start()
    try:
        await proxy.sever()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.listen_port)
        # The listener still accepts the TCP connection and then drops it, which
        # the relay sees as a closed uplink. EOF is the observable.
        assert await asyncio.wait_for(reader.read(1), timeout=5.0) == b""
        writer.close()
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert proxy.refused_while_severed == 1
    assert proxy.accepted == 0


async def test_severing_drops_a_connection_that_is_already_open() -> None:
    """An outage has to interrupt the live uplink, not just block new ones.

    The relay holds one long-lived WebSocket. A proxy that only refused new
    connections would leave it streaming and there would be no outage at all.
    """
    upstream_port = free_tcp_port()
    upstream = await echo_server(upstream_port)
    proxy = SeverableProxy(listen_port=free_tcp_port(), gateway_port=upstream_port)
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.listen_port)
        writer.write(b"x")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(1), timeout=5.0) == b"x"

        await proxy.sever()

        assert await asyncio.wait_for(reader.read(1), timeout=5.0) == b""
        writer.close()
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()


async def test_a_restored_proxy_carries_traffic_again() -> None:
    """Recovery must actually be measurable after the cut."""
    upstream_port = free_tcp_port()
    upstream = await echo_server(upstream_port)
    proxy = SeverableProxy(listen_port=free_tcp_port(), gateway_port=upstream_port)
    await proxy.start()
    try:
        await proxy.sever()
        proxy.restore()

        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.listen_port)
        writer.write(b"again")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(5), timeout=5.0)
        writer.close()
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert echoed == b"again"


# --- rates -----------------------------------------------------------------


def sample(
    at_s: float,
    intake: int,
    drained: int,
    severed: bool = False,
    dropped_intake: int = 0,
    dropped_cap: int = 0,
) -> Sample:
    return Sample(
        at_s=at_s,
        intake_total=intake,
        drained_total=drained,
        depth=intake - drained,
        queued_bytes=(intake - drained) * 40,
        dropped_intake=dropped_intake,
        dropped_cap=dropped_cap,
        severed=severed,
    )


def test_rates_are_per_second_over_the_phase() -> None:
    result = summarise("recovery", [sample(0.0, 1000, 500), sample(10.0, 2000, 2500)])

    assert result.intake_per_s == pytest.approx(100.0)
    assert result.drain_per_s == pytest.approx(200.0)
    assert result.drain_over_intake == pytest.approx(2.0)


def test_a_phase_with_one_sample_is_refused() -> None:
    """One sample is a reading, not a rate, and averaging it with nothing gives
    zero - which would read as a stalled system rather than a broken run."""
    with pytest.raises(MeasurementError, match="at least two"):
        summarise("baseline", [sample(0.0, 0, 0)])


def test_a_phase_spanning_no_time_is_refused() -> None:
    with pytest.raises(MeasurementError, match="no time"):
        summarise("baseline", [sample(5.0, 0, 0), sample(5.0, 10, 10)])


def test_drain_over_intake_is_none_rather_than_infinite_when_nothing_arrives() -> None:
    result = summarise("recovery", [sample(0.0, 100, 0), sample(10.0, 100, 500)])

    assert result.intake_per_s == 0.0
    assert result.drain_over_intake is None


# --- the instrument checking itself ----------------------------------------


def a_run(
    *,
    baseline: tuple[int, int],
    outage: tuple[int, int],
    recovery: tuple[int, int],
) -> Run:
    """A run from (intake, drained) deltas per phase, at 10 s each."""
    run = Run(sources=11, baseline_s=10.0, outage_s=10.0, recovery_s=10.0)
    phases = {"baseline": baseline, "outage": outage, "recovery": recovery}
    intake_total, drained_total = 0, 0
    at_s = 0.0
    for name, (intake, drained) in phases.items():
        start = sample(at_s, intake_total, drained_total, severed=name == "outage")
        intake_total += intake
        drained_total += drained
        at_s += 10.0
        end = sample(at_s, intake_total, drained_total, severed=name == "outage")
        run.samples[name] = [start, end]
    run.phases = [summarise(name, s) for name, s in run.samples.items()]
    return run


def test_a_sound_run_reports_no_problems() -> None:
    """The presence half: the check must be able to pass.

    Without this, a check that always found a problem would void every run and
    look like diligence.
    """
    run = a_run(baseline=(1000, 1000), outage=(1000, 0), recovery=(1000, 3000))

    assert check_instrument(run) == []


def test_drain_continuing_through_the_outage_voids_the_run() -> None:
    """The failure this check exists for.

    If the proxy is not in the relay's path - the relay still pointed at
    `:8081` - the "outage" is nothing at all, and the recovery figure is just
    the steady-state rate wearing a different label. That reads as success.
    """
    run = a_run(baseline=(1000, 1000), outage=(1000, 900), recovery=(1000, 3000))

    problems = check_instrument(run)

    assert len(problems) == 1
    assert "drain continued through the outage" in problems[0]
    assert "gateway_url" in problems[0]


def test_intake_stopping_during_the_outage_voids_the_run() -> None:
    """No intake means no backlog, so recovery measures an idle system."""
    run = a_run(baseline=(1000, 1000), outage=(0, 0), recovery=(1000, 3000))

    problems = check_instrument(run)

    assert any("intake stopped" in problem for problem in problems)


def test_no_intake_at_all_voids_the_run() -> None:
    run = a_run(baseline=(0, 0), outage=(0, 0), recovery=(0, 0))

    problems = check_instrument(run)

    assert any("receiving nothing" in problem for problem in problems)


def test_a_missing_phase_is_an_error_rather_than_a_default() -> None:
    run = Run(sources=1, baseline_s=1.0, outage_s=1.0, recovery_s=1.0)
    run.phases = [
        PhaseResult(
            name="baseline",
            duration_s=1.0,
            intake_records=1,
            drained_records=1,
            intake_per_s=1.0,
            drain_per_s=1.0,
            depth_start=0,
            depth_end=0,
            queued_bytes_end=0,
            dropped_intake=0,
            dropped_cap=0,
        )
    ]

    with pytest.raises(MeasurementError, match="no phase named 'outage'"):
        run.phase("outage")


def a_run_with_drops(*, dropped_intake: int = 0, dropped_cap: int = 0) -> Run:
    """A sound run except for drops during recovery."""
    run = Run(sources=11, baseline_s=10.0, outage_s=10.0, recovery_s=10.0)
    run.samples = {
        "baseline": [sample(0.0, 0, 0), sample(10.0, 1000, 1000)],
        "outage": [
            sample(10.0, 1000, 1000, severed=True),
            sample(20.0, 2000, 1000, severed=True),
        ],
        "recovery": [
            sample(20.0, 2000, 1000),
            sample(
                30.0,
                3000,
                4000,
                dropped_intake=dropped_intake,
                dropped_cap=dropped_cap,
            ),
        ],
    }
    run.phases = [summarise(name, s) for name, s in run.samples.items()]
    return run


def test_an_intake_drop_is_reported_because_it_flatters_the_ratio() -> None:
    """A dropped datagram never gets a sequence number.

    So `next_seq` is short of what arrived, measured intake is below actual, and
    drain/intake looks better than the truth. Silence here would turn an
    overloaded relay into a passing grade.
    """
    problems = check_instrument(a_run_with_drops(dropped_intake=42))

    assert len(problems) == 1
    assert "42 datagram(s) dropped at intake" in problems[0]
    assert "better than the truth" in problems[0]


def test_a_cap_drop_is_reported_because_the_drain_figure_is_then_void() -> None:
    """Records the cap destroyed are gone. A drain rate measured while data is
    being thrown away is not measuring drain."""
    problems = check_instrument(a_run_with_drops(dropped_cap=7))

    assert len(problems) == 1
    assert "7 record(s) destroyed by the queue cap" in problems[0]


def test_a_run_with_no_drops_reports_nothing_about_drops() -> None:
    """The paired presence test: a counter that always complains is noise."""
    assert check_instrument(a_run_with_drops()) == []


# --- the in-flight tail ----------------------------------------------------
#
# The first live run failed its own check: 1,207 records were ingested during a
# 60 s outage and the harness accused the operator of misconfiguring
# gateway_url. The relay was configured correctly. Severing the link does not
# stop drain instantly, because what the relay already put on the wire is in the
# Gateway's socket buffer and is still made durable.


def test_the_settling_window_is_excluded_from_the_outage_rate() -> None:
    samples = [
        sample(0.0, 1000, 5000),
        sample(1.0, 1100, 5600),  # in-flight tail lands here
        sample(2.0, 1200, 5600),
        sample(10.0, 2000, 5600),
    ]

    kept = after_settling(samples, 2.0)

    assert [s.at_s for s in kept] == [2.0, 10.0]
    assert summarise("outage", kept).drained_records == 0


def test_a_settling_window_longer_than_the_phase_keeps_two_samples() -> None:
    """A misconfigured window must not silently report nothing."""
    samples = [sample(0.0, 0, 0), sample(1.0, 100, 100), sample(2.0, 200, 200)]

    kept = after_settling(samples, 999.0)

    assert len(kept) == 2
    assert kept == samples[-2:]


def test_the_in_flight_tail_is_counted_and_reported() -> None:
    run = Run(sources=1, baseline_s=1.0, outage_s=10.0, recovery_s=1.0)
    run.outage_settle_s = 2.0
    run.samples = {
        "baseline": [sample(0.0, 0, 0), sample(1.0, 200, 200)],
        "outage": [
            sample(1.0, 200, 200, severed=True),
            sample(2.0, 300, 1407, severed=True),
            sample(4.0, 500, 1407, severed=True),
            sample(11.0, 1200, 1407, severed=True),
        ],
        "recovery": [sample(11.0, 1200, 1407), sample(12.0, 1400, 2600)],
    }

    run.phases = summarise_run(run)

    assert run.in_flight_records == 1207
    assert run.phase("outage").drained_records == 0
    assert check_instrument(run) == []


def test_drain_that_continues_past_the_settling_window_still_voids_the_run() -> None:
    """The check must keep working. Excluding the tail must not excuse a proxy
    that is not in the path at all."""
    run = Run(sources=1, baseline_s=1.0, outage_s=10.0, recovery_s=1.0)
    run.outage_settle_s = 2.0
    run.samples = {
        "baseline": [sample(0.0, 0, 0), sample(1.0, 200, 200)],
        "outage": [
            sample(1.0, 200, 200, severed=True),
            sample(2.0, 400, 400, severed=True),
            sample(11.0, 2200, 2200, severed=True),
        ],
        "recovery": [sample(11.0, 2200, 2200), sample(12.0, 2400, 3600)],
    }

    run.phases = summarise_run(run)
    problems = check_instrument(run)

    assert any("drain continued through the outage" in p for p in problems)
    assert any("gateway_url" in p for p in problems)


def test_a_phase_reports_its_depth_range_not_just_the_end() -> None:
    """Depth falling while intake and drain disagree is the tell that a
    "baseline" is really a backlog draining. An endpoint alone hid it."""
    result = summarise("baseline", [sample(0.0, 0, 0), sample(10.0, 1940, 4265)])

    assert result.depth_start == 0
    assert result.depth_end == 1940 - 4265
