#!/usr/bin/env python3
"""relay-v1 verification sink - a receiver for the P1-01 hardware test.

This is NOT the Gateway. It exists so that the relay's acceptance criterion
(pull the network cable for two minutes, lose nothing) has something to talk to
across a real network, and so that the flight record can be checked afterwards.

It deliberately does not parse MAVLink, identify vehicles, or write to a
database. If it starts to need any of those, stop: that is P1-02.

It is also the first implementation of docs/protocols/relay-v1.md written from
the document rather than from the relay's source, including its own decoder for
the binary record format. That is the point - a protocol only one program
understands has not been specified, only described.

Run the receiver:

    python tools/relay_sink.py serve --out ./sink-data --token-file sink.token \\
        --cert server.crt --key server.key --host 0.0.0.0 --port 8443

Read the verification report:

    python tools/relay_sink.py report --out ./sink-data

See docs/runbooks/p1-01-hardware-test.md for the full procedure.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http
import ipaddress
import json
import os
import re
import ssl
import struct
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- relay-v1 §6, decoded from the document ---------------------------------
#
# repeated: u64 seq, i64 recv_utc_ns, u16 len, u8 datagram[len], little-endian.
_RECORD_HEADER = struct.Struct("<QqH")
_RECORD_HEADER_BYTES = _RECORD_HEADER.size

_SUMMARY = (__doc__ or "").splitlines()[0]

PROTOCOL_VERSION = 1

# relay-v1 §8: three consecutive misses means the station is unreachable.
STATUS_INTERVAL_S = 1.0
STATUS_MISSES_BEFORE_UNREACHABLE = 3

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class SinkError(RuntimeError):
    """The sink cannot do what was asked of it."""


@dataclass(frozen=True, slots=True)
class Record:
    seq: int
    recv_utc_ns: int
    datagram: bytes


def decode_batch(data: bytes) -> list[Record]:
    """Decode a relay-v1 binary batch.

    Written against the protocol document, not against agent/framing.py. A
    truncated batch raises: silence would be indistinguishable from telemetry
    that never existed, which is the failure this whole exercise is about.
    """
    records: list[Record] = []
    offset = 0
    total = len(data)

    while offset < total:
        if offset + _RECORD_HEADER_BYTES > total:
            raise SinkError(f"truncated record header at byte {offset}")
        seq, recv_utc_ns, length = _RECORD_HEADER.unpack_from(data, offset)
        offset += _RECORD_HEADER_BYTES
        end = offset + length
        if end > total:
            raise SinkError(
                f"record {seq} declares {length} bytes, {total - offset} present"
            )
        records.append(Record(seq, recv_utc_ns, data[offset:end]))
        offset = end

    return records


def encode_record(record: Record) -> bytes:
    """Store records in the same shape they arrived in, so the file is self-describing."""
    return (
        _RECORD_HEADER.pack(record.seq, record.recv_utc_ns, len(record.datagram))
        + record.datagram
    )


def _safe(name: str) -> str:
    """Make a station_id or epoch safe to use as a path component."""
    cleaned = _SAFE_NAME.sub("_", name)[:64]
    return cleaned or "unnamed"


# --- storage ----------------------------------------------------------------


@dataclass
class EpochState:
    """Everything the sink knows about one (station_id, epoch)."""

    station_id: str
    epoch: str
    directory: Path

    stored: set[int] = field(default_factory=set)
    gaps: list[tuple[int, int]] = field(default_factory=list)
    received_total: int = 0
    duplicate_total: int = 0
    relay_restarts: int = 0
    last_uptime_s: int | None = None
    last_status: dict[str, Any] | None = None
    sessions: int = 0

    @property
    def records_path(self) -> Path:
        return self.directory / f"{_safe(self.epoch)}.records"

    @property
    def events_path(self) -> Path:
        return self.directory / f"{_safe(self.epoch)}.events.jsonl"

    @property
    def counters_path(self) -> Path:
        return self.directory / f"{_safe(self.epoch)}.counters.json"

    def covered(self, seq: int) -> bool:
        """True if seq is stored, or inside a gap the relay already reported."""
        if seq in self.stored:
            return True
        return any(start <= seq < end for start, end in self.gaps)

    @property
    def resume_from_seq(self) -> int:
        """The next sequence number this sink needs (relay-v1 §5).

        Counts a reported gap as satisfied. Without that, an unrecoverable gap
        would be re-reported on every single reconnect for the life of the
        epoch, because the watermark could never advance past the hole.
        """
        seq = 0
        while self.covered(seq):
            seq += 1
        return seq

    @property
    def ack_seq(self) -> int:
        """Cumulative ack: everything below resume_from_seq is safe."""
        return self.resume_from_seq - 1


class SinkStore:
    """Append-only storage, one directory per station."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._epochs: dict[tuple[str, str], EpochState] = {}
        self._handles: dict[tuple[str, str], Any] = {}
        root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def state(self, station_id: str, epoch: str) -> EpochState:
        key = (station_id, epoch)
        if key not in self._epochs:
            directory = self._root / _safe(station_id)
            directory.mkdir(parents=True, exist_ok=True)
            state = EpochState(station_id=station_id, epoch=epoch, directory=directory)
            _load(state)
            self._epochs[key] = state
        return self._epochs[key]

    def append(self, state: EpochState, records: list[Record]) -> int:
        """Persist and fsync. Returns how many were new.

        The fsync is the whole contract: relay-v1 §7 says a record is
        acknowledged only once it is durably stored, and an ack is a promise
        the relay acts on by deleting its own copy.
        """
        key = (state.station_id, state.epoch)
        handle = self._handles.get(key)
        if handle is None:
            handle = state.records_path.open("ab")
            self._handles[key] = handle

        new = 0
        for record in records:
            state.received_total += 1
            if record.seq in state.stored:
                state.duplicate_total += 1
                continue
            handle.write(encode_record(record))
            state.stored.add(record.seq)
            new += 1

        handle.flush()
        os.fsync(handle.fileno())

        # Counters are persisted here, under the same fsync as the records they
        # describe, and not only on the 1 Hz status tick. Written on the tick
        # alone they lag behind the data, and an unclean stop leaves a report
        # claiming fewer records received than are stored - arithmetically
        # impossible, and indistinguishable from a real anomaly by anyone
        # reading it later. Observed on 2026-09-22: 57,965 received against
        # 58,003 stored.
        self.save_counters(state)
        return new

    def event(self, state: EpochState, payload: dict[str, Any]) -> None:
        line = json.dumps({"t_utc_ns": time.time_ns(), **payload}, sort_keys=True)
        with state.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def save_counters(self, state: EpochState) -> None:
        payload = {
            "station_id": state.station_id,
            "epoch": state.epoch,
            "received_total": state.received_total,
            "duplicate_total": state.duplicate_total,
            "relay_restarts": state.relay_restarts,
            "last_uptime_s": state.last_uptime_s,
            "sessions": state.sessions,
            "gaps": [list(gap) for gap in state.gaps],
            "last_status": state.last_status,
        }
        # Written to a temporary file and moved into place, so a process that
        # dies mid-write leaves the previous counters intact rather than a
        # truncated file. fsync before the move, because a rename that reaches
        # the disk ahead of the contents would be worse than either.
        temporary = state.counters_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(state.counters_path)

    def close(self) -> None:
        for handle in self._handles.values():
            with contextlib.suppress(OSError):
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
        self._handles.clear()
        for state in self._epochs.values():
            self.save_counters(state)

    def all_states(self) -> list[EpochState]:
        return sorted(self._epochs.values(), key=lambda s: (s.station_id, s.epoch))

    def load_all(self) -> None:
        """Rebuild every epoch found on disk, for the report command."""
        for counters in sorted(self._root.glob("*/*.counters.json")):
            try:
                payload = json.loads(counters.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            self.state(payload["station_id"], payload["epoch"])


def _load(state: EpochState) -> None:
    """Rebuild an epoch's state from what is on disk.

    Resuming from the records file rather than from a remembered watermark is
    deliberate: stopping and restarting the sink is the second way the hardware
    test induces an outage, and the answer has to come from the data.
    """
    if state.counters_path.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError, KeyError):
            payload = json.loads(state.counters_path.read_text(encoding="utf-8"))
            state.received_total = int(payload.get("received_total", 0))
            state.duplicate_total = int(payload.get("duplicate_total", 0))
            state.relay_restarts = int(payload.get("relay_restarts", 0))
            state.last_uptime_s = payload.get("last_uptime_s")
            state.sessions = int(payload.get("sessions", 0))
            state.last_status = payload.get("last_status")
            state.gaps = [
                (int(start), int(end)) for start, end in payload.get("gaps", [])
            ]

    if state.records_path.exists():
        for record in _scan(state.records_path):
            state.stored.add(record.seq)


def _scan(path: Path) -> Iterator[Record]:
    """Read an append-only records file, stopping at the first partial record.

    A truncated tail is expected: the process may have been killed mid-write.
    Everything before it is intact, which is what an append-only file buys.
    """
    data = path.read_bytes()
    offset = 0
    total = len(data)
    while offset + _RECORD_HEADER_BYTES <= total:
        seq, recv_utc_ns, length = _RECORD_HEADER.unpack_from(data, offset)
        offset += _RECORD_HEADER_BYTES
        end = offset + length
        if end > total:
            return
        yield Record(seq, recv_utc_ns, data[offset:end])
        offset = end


# --- the server -------------------------------------------------------------


class RelaySink:
    """The server side of relay-v1."""

    def __init__(self, store: SinkStore, token: str) -> None:
        self._store = store
        self._token = token
        self.rejected_connections = 0

    def authorise(self, header: str | None) -> bool:
        return header == f"Bearer {self._token}"

    async def handle(self, connection: Any) -> None:
        hello_raw = await connection.recv()
        if isinstance(hello_raw, bytes):
            await connection.close(code=1002, reason="expected hello")
            return
        hello = json.loads(hello_raw)
        if hello.get("type") != "hello":
            await connection.close(code=1002, reason="expected hello")
            return

        station_id = str(hello["station_id"])
        epoch = str(hello["epoch"])
        state = self._store.state(station_id, epoch)
        state.sessions += 1

        resume_from = state.resume_from_seq
        self._store.event(
            state,
            {
                "event": "session_open",
                "resume_from_seq": resume_from,
                "relay_version": hello.get("relay_version"),
                "oldest_seq_held": hello.get("oldest_seq_held"),
                "newest_seq_held": hello.get("newest_seq_held"),
                "monotonic_ns": hello.get("monotonic_ns"),
                "utc_ns": hello.get("utc_ns"),
            },
        )

        await connection.send(
            json.dumps(
                {
                    "type": "welcome",
                    "protocol_version": PROTOCOL_VERSION,
                    "resume_from_seq": resume_from,
                }
            )
        )

        print(
            f"[sink] session: station={station_id} epoch={epoch[:8]} "
            f"resume_from_seq={resume_from}",
            flush=True,
        )

        try:
            async for message in connection:
                if isinstance(message, str):
                    await self._on_text(connection, state, json.loads(message))
                else:
                    await self._on_batch(connection, state, message)
        finally:
            self._store.event(state, {"event": "session_close"})
            self._store.save_counters(state)

    async def _on_batch(
        self, connection: Any, state: EpochState, message: bytes
    ) -> None:
        records = decode_batch(message)
        # Persist and fsync BEFORE acknowledging. The relay deletes its own
        # copy on the strength of this ack.
        self._store.append(state, records)
        await connection.send(
            json.dumps({"type": "ack", "epoch": state.epoch, "seq": state.ack_seq})
        )

    async def _on_text(
        self, connection: Any, state: EpochState, payload: dict[str, Any]
    ) -> None:
        kind = payload.get("type")

        if kind == "status":
            uptime = payload.get("uptime_s")
            if (
                isinstance(uptime, int)
                and state.last_uptime_s is not None
                and uptime < state.last_uptime_s
            ):
                # relay-v1 §11 loss #4: records still in the relay's memory at
                # that moment were lost, and this is the only sign of it.
                state.relay_restarts += 1
                self._store.event(
                    state,
                    {
                        "event": "relay_restart_inferred",
                        "previous_uptime_s": state.last_uptime_s,
                        "uptime_s": uptime,
                    },
                )
            if isinstance(uptime, int):
                state.last_uptime_s = uptime
            state.last_status = payload
            self._store.save_counters(state)
            return

        if kind == "gap":
            from_seq = int(payload["from_seq"])
            to_seq = int(payload["to_seq"])
            state.gaps.append((from_seq, to_seq))
            self._store.event(
                state,
                {
                    "event": "gap",
                    "from_seq": from_seq,
                    "to_seq": to_seq,
                    "reason": payload.get("reason"),
                },
            )
            self._store.save_counters(state)
            print(
                f"[sink] GAP reported: {from_seq}..{to_seq - 1} "
                f"({payload.get('reason')})",
                flush=True,
            )
            return

        # Unknown types are ignored, not rejected (relay-v1 §2).


# --- the report -------------------------------------------------------------


def _ranges(values: list[int]) -> list[tuple[int, int]]:
    """Collapse a sorted list of ints into inclusive ranges."""
    ranges: list[tuple[int, int]] = []
    for value in values:
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], value)
        else:
            ranges.append((value, value))
    return ranges


def _format_ranges(ranges: list[tuple[int, int]], limit: int = 12) -> str:
    if not ranges:
        return "none"
    shown = [
        f"{start}" if start == end else f"{start}-{end}"
        for start, end in ranges[:limit]
    ]
    if len(ranges) > limit:
        shown.append(f"... and {len(ranges) - limit} more")
    return ", ".join(shown)


def build_report(store: SinkStore) -> str:
    """Plain ASCII, pasteable into a test record."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("relay-v1 sink verification report")
    lines.append(f"data directory: {store.root}")
    lines.append("=" * 72)

    states = store.all_states()
    if not states:
        lines.append("")
        lines.append("NO DATA. The sink received nothing.")
        return "\n".join(lines)

    overall_pass = True

    for state in states:
        stored = sorted(state.stored)
        lines.append("")
        lines.append(f"station_id : {state.station_id}")
        lines.append(f"epoch      : {state.epoch}")
        lines.append(f"sessions   : {state.sessions}")

        if not stored:
            lines.append("records    : none")
            overall_pass = False
            continue

        lowest, highest = stored[0], stored[-1]
        expected = set(range(lowest, highest + 1))
        gap_covered = {
            seq
            for start, end in state.gaps
            for seq in range(max(start, lowest), min(end, highest + 1))
        }
        missing = sorted(expected - state.stored - gap_covered)

        lines.append(f"seq range  : {lowest} .. {highest}  ({len(stored)} stored)")
        lines.append(f"received   : {state.received_total}")
        lines.append(
            f"duplicates : {state.duplicate_total}  "
            f"(re-sent after a reconnect; harmless once deduped)"
        )
        lines.append(f"missing    : {_format_ranges(_ranges(missing))}")

        if state.gaps:
            gap_text = ", ".join(f"{a}-{b - 1}" for a, b in state.gaps)
            lines.append(f"gaps       : {gap_text}  (reported by the relay)")
        else:
            lines.append("gaps       : none reported")

        lines.append(f"relay restarts inferred from uptime_s: {state.relay_restarts}")

        status = state.last_status or {}
        lines.append(
            "last status: "
            f"queue_depth={status.get('queue_depth')} "
            f"queue_bytes={status.get('queue_bytes')} "
            f"last_datagram_age_ms={status.get('last_datagram_age_ms')}"
        )
        lines.append(
            "drops      : "
            f"intake={status.get('dropped_intake_total')} "
            f"cap={status.get('dropped_cap_total')}"
        )

        verdict_problems: list[str] = []
        if missing:
            verdict_problems.append(f"{len(missing)} sequence numbers never arrived")
        if status.get("dropped_intake_total"):
            verdict_problems.append("the relay dropped datagrams at intake")
        if status.get("dropped_cap_total"):
            verdict_problems.append("the relay dropped records at the queue cap")
        if state.gaps:
            verdict_problems.append("the relay reported an unrecoverable gap")

        lines.append("")
        if verdict_problems:
            overall_pass = False
            lines.append("VERDICT: FAIL")
            for problem in verdict_problems:
                lines.append(f"  - {problem}")
        else:
            lines.append("VERDICT: PASS - contiguous, nothing dropped, nothing lost.")

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"OVERALL: {'PASS' if overall_pass else 'FAIL'}")
    lines.append("=" * 72)
    return "\n".join(lines)


# --- entry points -----------------------------------------------------------


def _is_loopback(host: str) -> bool:
    """True for an address that never leaves this machine."""
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _tls_context(cert: Path | None, key: Path | None) -> ssl.SSLContext | None:
    if cert is None and key is None:
        return None
    if cert is None or key is None:
        raise SinkError(
            "--cert and --key must be given together: a certificate without "
            "its private key cannot serve TLS"
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return context


def check_bind_is_safe(host: str, context: ssl.SSLContext | None) -> None:
    """Refuse to accept bearer tokens in plaintext from the network.

    The relay already refuses to *send* a token over ws:// to anything but
    loopback. The sink is the other half of that rule: without it, the default
    --host 0.0.0.0 with no certificate would sit on the LAN accepting tokens in
    the clear, and the protection would depend entirely on every relay being
    configured correctly. A credential is only as protected as the more
    permissive end of the link.
    """
    if context is not None or _is_loopback(host):
        return
    raise SinkError(
        f"refusing to serve plaintext on {host!r}: a bearer token would cross "
        f"the network in the clear. Pass --cert and --key, or bind 127.0.0.1 "
        f"for a local test. See docs/runbooks/p1-01-hardware-test.md."
    )


async def serve_forever(args: argparse.Namespace) -> int:
    from websockets.asyncio.server import serve

    # Refuse an unsafe bind before touching anything else: a missing token
    # file must not mask a refusal to accept credentials in the clear.
    context = _tls_context(args.cert, args.key)
    check_bind_is_safe(args.host, context)

    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SinkError(f"cannot read token file {args.token_file}: {error}") from error
    if not token:
        raise SinkError(f"token file {args.token_file} is empty")

    store = SinkStore(args.out)
    sink = RelaySink(store, token)

    if context is None:
        print(
            "[sink] no --cert/--key: serving ws:// on loopback only.",
            flush=True,
        )

    def process_request(connection: Any, request: Any) -> Any:
        # A real HTTP 401 on the upgrade, per relay-v1 §3: the relay treats it
        # as fatal and stops retrying, which a WebSocket close would not do.
        if not sink.authorise(request.headers.get("Authorization")):
            sink.rejected_connections += 1
            print("[sink] rejected a connection: bad or missing token", flush=True)
            return connection.respond(
                http.HTTPStatus.UNAUTHORIZED, "invalid relay token\n"
            )
        return None

    scheme = "wss" if context else "ws"
    print(
        f"[sink] listening on {scheme}://{args.host}:{args.port}/relay/v1",
        flush=True,
    )
    print(f"[sink] writing to {args.out.resolve()}", flush=True)
    print("[sink] Ctrl+C to stop and print the report.", flush=True)

    stop = asyncio.Event()
    try:
        async with await serve(
            sink.handle,
            args.host,
            args.port,
            ssl=context,
            process_request=process_request,
            max_size=8 * 1024 * 1024,
        ):
            await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        store.close()

    print()
    print(build_report(store))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(serve_forever(args))
    except KeyboardInterrupt:
        return 0
    except SinkError as error:
        print(f"relay_sink: {error}", file=sys.stderr)
        return 2


def cmd_report(args: argparse.Namespace) -> int:
    if not args.out.is_dir():
        print(f"relay_sink: no data directory at {args.out}", file=sys.stderr)
        return 2
    store = SinkStore(args.out)
    store.load_all()
    print(build_report(store))
    return 0


class _Help(argparse.ArgumentDefaultsHelpFormatter):
    """Show defaults, except where a default is meaningless.

    ArgumentDefaultsHelpFormatter prints "(default: None)" against required
    arguments, which reads as though None were an acceptable value.
    """

    def _get_help_string(self, action: argparse.Action) -> str | None:
        if action.required or action.default is None:
            return action.help
        return super()._get_help_string(action)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="relay_sink.py",
        description=_SUMMARY,
        epilog="Full procedure: docs/runbooks/p1-01-hardware-test.md",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser(
        "serve",
        help="receive from a relay",
        description="Receive and store a relay-v1 stream.",
        formatter_class=_Help,
    )
    serve_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory to store received records in; reused on restart to "
        "resume from what is already on disk",
    )
    serve_parser.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="file holding the bearer token this sink accepts; must match the "
        "contents of the relay's token_path exactly",
    )
    serve_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="address to bind. Anything but loopback requires --cert/--key, so "
        "a bearer token is never accepted in the clear over a network",
    )
    serve_parser.add_argument("--port", type=int, default=8443, help="port to bind")
    serve_parser.add_argument(
        "--cert",
        type=Path,
        help="PEM server certificate. Give it together with --key to serve "
        "wss://; neither works without the other",
    )
    serve_parser.add_argument(
        "--key",
        type=Path,
        help="PEM private key for --cert. Give it together with --cert to "
        "serve wss://; neither works without the other",
    )
    serve_parser.set_defaults(func=cmd_serve)

    report_parser = sub.add_parser(
        "report",
        help="print the verification report",
        description="Print the verification report for a previous run.",
        formatter_class=_Help,
    )
    report_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="the directory a previous `serve` run wrote to",
    )
    report_parser.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    handler: Any = args.func
    result: int = handler(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
