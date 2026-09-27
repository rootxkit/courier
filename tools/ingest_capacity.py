"""Measure ingest intake and drain as separate rates, across a real outage.

P1-10. `relay-v1.md` §10 claims a 30-minute outage "drains in seconds". That
was bandwidth arithmetic. On 2026-09-25, with eleven sources, the relay queue
grew by roughly 790 records/s after a two-minute Gateway outage and never
drained -- so the claim is not merely optimistic, it has the sign wrong.

## What is measured, and why these two signals

Two monotonic counters already exist, so nothing here changes the protocol and
nothing needs new instrumentation on the hot path:

**Intake** is `meta.next_seq` in the relay's own SQLite queue. It is the next
sequence number to be assigned, so it counts every datagram the relay has ever
accepted and committed, and it never goes backwards or down on acknowledgement.

**Drain** is `relay_epochs.highest_contiguous_seq` in the telemetry database.
It is the Gateway's durable watermark: the highest sequence number such that
every record up to it is stored. That is exactly "records durably ingested",
which is what a drain rate has to mean here -- bytes off a socket would not
count, because a record is not drained until it is safe.

Both are read without disturbing the system: the queue read-only over WAL, the
watermark with a single indexed select.

Queue depth is deliberately *not* the primary signal. It is a difference of the
two rates, so quoting it alone hides which side moved, and it is unreadable
during the outage itself -- the relay cannot report status to a Gateway it
cannot reach, which is the very window that matters.

## Why a proxy rather than stopping the Gateway

An outage produced by killing the Gateway measures a cold start as well: empty
connection pools, an unwarmed page cache, Alembic and settings work, NATS
reconnection. Those are real costs but they are not the outage, and mixing them
in was the flaw in the original observation.

So the relay connects to a severable proxy in this process. Cutting it gives the
relay exactly what a network failure gives it -- a dead uplink -- while the
Gateway keeps running with everything warm. Restoring it measures recovery and
nothing else.

## Phases

    baseline   the system keeping up, or not; intake and drain should match
    outage     proxy severed; drain must be 0 and intake must continue
    recovery   proxy restored; drain is now the maximum the Gateway can do,
               because the relay always has a backlog ready to send

The `outage` phase doubles as the instrument's own check. If drain is not zero
while the uplink is severed, the measurement is wrong and the numbers that
follow mean nothing, so it is asserted rather than assumed.

## Usage

Point the relay's `gateway_url` at this proxy, then:

    python -m tools.ingest_capacity --sources 11 --json out/cap-11.json

Run it at 1, 3 and 11 sources. The comparison across fleet sizes is the point;
a single number says nothing about whether the limit scales with the fleet.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DEFAULT_LISTEN_PORT = 18081
DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 8081
DEFAULT_SAMPLE_INTERVAL_S = 1.0


class MeasurementError(RuntimeError):
    """The measurement cannot be trusted, so it is not reported."""


# --- the severable proxy ---------------------------------------------------


@dataclass
class SeverableProxy:
    """A TCP proxy that can be cut and restored, standing in for the network.

    Not a general proxy: it forwards bytes in both directions and knows nothing
    about WebSocket framing, which is all that is needed. Severing closes every
    live connection and refuses new ones, so the relay sees a dead uplink and
    takes its own §12 backoff path -- the real one, not a simulated one.
    """

    listen_host: str = "127.0.0.1"
    listen_port: int = DEFAULT_LISTEN_PORT
    gateway_host: str = DEFAULT_GATEWAY_HOST
    gateway_port: int = DEFAULT_GATEWAY_PORT

    def __post_init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._severed = False
        self.accepted = 0
        self.refused_while_severed = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.listen_host, self.listen_port
        )

    async def stop(self) -> None:
        # Close the listener, then cancel the live connections, and only then
        # wait for the server. The order is load-bearing: from Python 3.12
        # `wait_closed` waits for outstanding handler tasks, and those tasks sit
        # in `_pump` awaiting a read that will not return until the peer sends
        # EOF. Waiting before cancelling deadlocks whenever a connection is
        # open - which at the end of a measurement run it always is, because the
        # relay is still streaming.
        if self._server is not None:
            self._server.close()
        await self._close_all()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    @property
    def severed(self) -> bool:
        return self._severed

    async def sever(self) -> None:
        """Cut the link: refuse new connections and drop existing ones."""
        self._severed = True
        await self._close_all()

    def restore(self) -> None:
        self._severed = False

    async def _close_all(self) -> None:
        for writer in list(self._writers):
            with contextlib.suppress(OSError):
                writer.close()
        self._writers.clear()
        for task in list(self._connections):
            task.cancel()
        for task in list(self._connections):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._connections.clear()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._severed:
            self.refused_while_severed += 1
            writer.close()
            return

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.gateway_host, self.gateway_port
            )
        except OSError:
            writer.close()
            return

        self.accepted += 1
        self._writers.add(writer)
        self._writers.add(upstream_writer)

        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            await asyncio.gather(
                self._pump(reader, upstream_writer),
                self._pump(upstream_reader, writer),
            )
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            for side in (writer, upstream_writer):
                self._writers.discard(side)
                with contextlib.suppress(OSError):
                    side.close()
            if task is not None:
                self._connections.discard(task)

    @staticmethod
    async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()


# --- reading the two counters ----------------------------------------------


@dataclass(frozen=True, slots=True)
class RelayCounters:
    """What the relay's own queue knows about itself."""

    next_seq: int
    depth: int
    queued_bytes: int
    # Datagrams the UDP thread handed over that the writer could not take,
    # because the in-memory intake queue was full. These never received a
    # sequence number, so `next_seq` undercounts arrivals by exactly this much.
    # Intake is therefore "accepted", not "offered", and the difference has to
    # be visible or the measurement flatters itself.
    dropped_intake: int
    # Records the queue cap destroyed. Real data loss, and a reason to distrust
    # any drain figure measured over the same window.
    dropped_cap: int


def read_relay_counters(queue_path: Path) -> RelayCounters:
    """The queue's counters, read-only.

    Opened `mode=ro` through a URI so this can never write to a queue that is
    the only copy of unacknowledged flight data. WAL lets it read while the
    relay commits.
    """
    uri = f"file:{queue_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        if "next_seq" not in meta:
            raise MeasurementError(
                f"{queue_path} has no next_seq; is it a relay queue?"
            )
        depth, queued_bytes = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(nbytes), 0) FROM records"
        ).fetchone()
        return RelayCounters(
            next_seq=int(meta["next_seq"]),
            depth=int(depth),
            queued_bytes=int(queued_bytes),
            dropped_intake=int(meta.get("dropped_intake_total", 0)),
            dropped_cap=int(meta.get("dropped_cap_total", 0)),
        )
    finally:
        connection.close()


async def read_watermark(engine: AsyncEngine, station_id: str) -> int:
    """The Gateway's durable watermark for the station's open epoch.

    Summed across epochs rather than taken from one, because a relay that
    restarts opens a new epoch and its watermark restarts at -1; the total
    ingested is what a drain rate is about. `+ 1` turns each watermark into a
    count, since -1 means nothing is stored.
    """
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT COALESCE(SUM(highest_contiguous_seq + 1), 0) "
                "FROM relay_epochs WHERE station_id = :station"
            ),
            {"station": station_id},
        )
        return int(result.scalar_one())


# --- samples and phases ----------------------------------------------------


@dataclass
class Sample:
    at_s: float
    intake_total: int
    drained_total: int
    depth: int
    queued_bytes: int
    dropped_intake: int
    dropped_cap: int
    severed: bool


@dataclass
class PhaseResult:
    name: str
    duration_s: float
    intake_records: int
    drained_records: int
    intake_per_s: float
    drain_per_s: float
    depth_start: int
    depth_end: int
    queued_bytes_end: int
    dropped_intake: int
    dropped_cap: int

    @property
    def drain_over_intake(self) -> float | None:
        if self.intake_per_s <= 0.0:
            return None
        return self.drain_per_s / self.intake_per_s


def summarise(name: str, samples: list[Sample]) -> PhaseResult:
    if len(samples) < 2:
        raise MeasurementError(
            f"phase {name!r} has {len(samples)} sample(s); at least two are needed "
            "to form a rate"
        )
    first, last = samples[0], samples[-1]
    duration_s = last.at_s - first.at_s
    if duration_s <= 0.0:
        raise MeasurementError(f"phase {name!r} spans no time")

    intake = last.intake_total - first.intake_total
    drained = last.drained_total - first.drained_total
    return PhaseResult(
        name=name,
        duration_s=duration_s,
        intake_records=intake,
        drained_records=drained,
        intake_per_s=intake / duration_s,
        drain_per_s=drained / duration_s,
        depth_start=first.depth,
        depth_end=last.depth,
        queued_bytes_end=last.queued_bytes,
        dropped_intake=last.dropped_intake - first.dropped_intake,
        dropped_cap=last.dropped_cap - first.dropped_cap,
    )


@dataclass
class Run:
    sources: int
    baseline_s: float
    outage_s: float
    recovery_s: float
    samples: dict[str, list[Sample]] = field(default_factory=dict)
    phases: list[PhaseResult] = field(default_factory=list)

    def phase(self, name: str) -> PhaseResult:
        for result in self.phases:
            if result.name == name:
                return result
        raise MeasurementError(f"no phase named {name!r}")


class Measurement:
    def __init__(
        self,
        *,
        queue_path: Path,
        engine: AsyncEngine,
        station_id: str,
        proxy: SeverableProxy,
        interval_s: float,
    ) -> None:
        self._queue_path = queue_path
        self._engine = engine
        self._station_id = station_id
        self._proxy = proxy
        self._interval_s = interval_s
        self._t0 = time.monotonic()

    async def sample(self) -> Sample:
        counters = await asyncio.to_thread(read_relay_counters, self._queue_path)
        drained = await read_watermark(self._engine, self._station_id)
        return Sample(
            at_s=time.monotonic() - self._t0,
            intake_total=counters.next_seq,
            drained_total=drained,
            depth=counters.depth,
            queued_bytes=counters.queued_bytes,
            dropped_intake=counters.dropped_intake,
            dropped_cap=counters.dropped_cap,
            severed=self._proxy.severed,
        )

    async def collect(self, seconds: float, label: str) -> list[Sample]:
        samples: list[Sample] = [await self.sample()]
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(
                min(self._interval_s, max(deadline - time.monotonic(), 0.0))
            )
            sample = await self.sample()
            samples.append(sample)
            sys.stderr.write(
                f"  {label:9s} +{sample.at_s:6.1f}s  "
                f"intake={sample.intake_total:>10d}  "
                f"drained={sample.drained_total:>10d}  "
                f"depth={sample.depth:>9d}\n"
            )
            sys.stderr.flush()
        return samples


async def run_measurement(args: argparse.Namespace, engine: AsyncEngine) -> Run:
    proxy = SeverableProxy(
        listen_port=args.listen_port,
        gateway_host=args.gateway_host,
        gateway_port=args.gateway_port,
    )
    await proxy.start()
    sys.stderr.write(
        f"proxy listening on ws://127.0.0.1:{args.listen_port}/relay/v1 "
        f"-> {args.gateway_host}:{args.gateway_port}\n"
        "point the relay's gateway_url at the proxy, not at the Gateway\n\n"
    )

    measurement = Measurement(
        queue_path=args.relay_queue,
        engine=engine,
        station_id=args.station,
        proxy=proxy,
        interval_s=args.interval_s,
    )
    run = Run(
        sources=args.sources,
        baseline_s=args.baseline_s,
        outage_s=args.outage_s,
        recovery_s=args.recovery_s,
    )

    try:
        sys.stderr.write(f"baseline for {args.baseline_s:.0f}s\n")
        run.samples["baseline"] = await measurement.collect(args.baseline_s, "baseline")

        sys.stderr.write(f"\nsevering the uplink for {args.outage_s:.0f}s\n")
        await proxy.sever()
        run.samples["outage"] = await measurement.collect(args.outage_s, "outage")

        sys.stderr.write(f"\nrestoring; recovery for {args.recovery_s:.0f}s\n")
        proxy.restore()
        run.samples["recovery"] = await measurement.collect(args.recovery_s, "recovery")
    finally:
        await proxy.stop()

    run.phases = [summarise(name, s) for name, s in run.samples.items()]
    return run


def check_instrument(run: Run) -> list[str]:
    """Reasons the numbers cannot be trusted. Empty means they can.

    The outage phase is the instrument checking itself. Drain must stop when
    the uplink is cut; if it does not, either the proxy is not in the path or
    the watermark is not measuring what this claims, and in both cases the
    recovery figure is meaningless. Stated as findings rather than asserted, so
    a run that fails the check still shows its numbers and says why they are
    void.
    """
    problems: list[str] = []
    outage = run.phase("outage")
    baseline = run.phase("baseline")

    if outage.drained_records != 0:
        problems.append(
            f"drain did not stop during the outage ({outage.drained_records} "
            "records ingested with the uplink severed) - the proxy is probably "
            "not in the relay's path, so check gateway_url"
        )
    if outage.intake_records <= 0:
        problems.append(
            "intake stopped during the outage, so nothing was buffered and "
            "there was no backlog to drain - are the sources still running?"
        )
    if baseline.intake_records <= 0:
        problems.append("no intake during baseline - the relay is receiving nothing")

    # Drops do not void the run, but they change what the numbers mean, so they
    # are reported as loudly as a fault. An intake drop means `next_seq` is
    # short of what actually arrived, so the true intake rate is higher than
    # measured and the drain/intake ratio is optimistic. A cap drop means
    # telemetry was destroyed, and a drain rate measured while records are being
    # thrown away is not a drain rate at all.
    for phase in run.phases:
        if phase.dropped_intake:
            problems.append(
                f"{phase.dropped_intake} datagram(s) dropped at intake during "
                f"{phase.name}: measured intake is below actual, so drain/intake "
                "is better than the truth"
            )
        if phase.dropped_cap:
            problems.append(
                f"{phase.dropped_cap} record(s) destroyed by the queue cap during "
                f"{phase.name}: data was lost, and drain measured over that "
                "window is meaningless"
            )
    return problems


def report(run: Run, problems: list[str]) -> None:
    print()
    print(f"=== ingest capacity, {run.sources} source(s)")
    print()
    header = (
        f"{'phase':10s} {'secs':>7s} {'intake/s':>10s} {'drain/s':>10s} "
        f"{'depth end':>10s} {'queued MiB':>11s} {'drop in':>8s} {'drop cap':>9s}"
    )
    print(header)
    print("-" * len(header))
    for phase in run.phases:
        print(
            f"{phase.name:10s} {phase.duration_s:7.1f} "
            f"{phase.intake_per_s:10.1f} {phase.drain_per_s:10.1f} "
            f"{phase.depth_end:10d} {phase.queued_bytes_end / 1048576:11.1f} "
            f"{phase.dropped_intake:8d} {phase.dropped_cap:9d}"
        )

    recovery = run.phase("recovery")
    baseline = run.phase("baseline")
    print()
    print("Intake is the relay's next_seq; drain is the Gateway's durable watermark.")
    print()

    # The headline. Recovery drain is the most the Gateway can do, because the
    # relay has a backlog ready and is never waiting for data.
    intake_reference = max(baseline.intake_per_s, recovery.intake_per_s)
    print(f"max drain rate      {recovery.drain_per_s:10.1f} records/s")
    print(f"intake rate         {intake_reference:10.1f} records/s")
    if intake_reference > 0:
        ratio = recovery.drain_per_s / intake_reference
        print(f"drain / intake      {ratio:10.2f}x")
        print()
        if ratio <= 1.0:
            print(
                "VERDICT: drain does not exceed intake, so a backlog never clears.\n"
                "         Any outage becomes permanent lag. Nothing reports a fault:\n"
                "         the relay is buffering as designed and the console shows an\n"
                "         ever-older fleet."
            )
        else:
            headroom = recovery.drain_per_s - intake_reference
            print(
                f"VERDICT: drain exceeds intake by {headroom:.1f} records/s, so one\n"
                f"         second of outage needs about "
                f"{1.0 / (ratio - 1.0):.1f}s of recovery."
            )

    if problems:
        print()
        print("MEASUREMENT NOT VALID:")
        for problem in problems:
            print(f"  - {problem}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ingest_capacity", description=__doc__)
    parser.add_argument(
        "--relay-queue",
        type=Path,
        default=Path("local/e2e/relay-queue.sqlite3"),
        help="the relay's SQLite queue; read-only (default: %(default)s)",
    )
    parser.add_argument(
        "--station", default="tbilisi-base-1", help="station_id (default: %(default)s)"
    )
    parser.add_argument(
        "--sources",
        type=int,
        required=True,
        help="how many vehicles are streaming, recorded with the result",
    )
    parser.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument("--gateway-host", default=DEFAULT_GATEWAY_HOST)
    parser.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT)
    parser.add_argument("--baseline-s", type=float, default=30.0)
    parser.add_argument("--outage-s", type=float, default=60.0)
    parser.add_argument("--recovery-s", type=float, default=120.0)
    parser.add_argument("--interval-s", type=float, default=DEFAULT_SAMPLE_INTERVAL_S)
    parser.add_argument(
        "--json", type=Path, default=None, help="also write the samples and rates here"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("TELEMETRY_DATABASE_URL", ""),
        help="telemetry database (default: $TELEMETRY_DATABASE_URL)",
    )
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if not args.database_url:
        print(
            "TELEMETRY_DATABASE_URL is not set and --database-url was not given.",
            file=sys.stderr,
        )
        return 2
    if not args.relay_queue.exists():
        print(f"{args.relay_queue} does not exist.", file=sys.stderr)
        return 2

    engine = create_async_engine(args.database_url)
    try:
        run = await run_measurement(args, engine)
    finally:
        await engine.dispose()

    problems = check_instrument(run)
    report(run, problems)

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "sources": run.sources,
            "phases": [
                {**asdict(phase), "drain_over_intake": phase.drain_over_intake}
                for phase in run.phases
            ],
            "samples": {
                name: [asdict(sample) for sample in samples]
                for name, samples in run.samples.items()
            },
            "problems": problems,
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(parse_args(argv)))
    except KeyboardInterrupt:
        return 130
    except MeasurementError as error:
        print(f"measurement failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
