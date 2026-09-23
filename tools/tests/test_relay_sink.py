"""The sink against the real relay, in process.

This is the pair that the hardware test runs across a LAN: the actual Relay
from agent/, and the actual sink from tools/. Nothing is stubbed between them,
so these tests exercise the protocol rather than either side's idea of it.

Durations are kept short on purpose. The long outage is the hardware test's
job; what is verified here is that the two implementations agree, including
across a sink restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent.config import RelayConfig
from agent.queue import DurableQueue
from agent.relay import Relay
from tests.ports import free_tcp_port, free_udp_port
from tools.relay_sink import (
    Record,
    RelaySink,
    SinkError,
    SinkStore,
    _tls_context,
    build_report,
    check_bind_is_safe,
    decode_batch,
    encode_record,
)

TOKEN = "sink-test-token"


class SinkServer:
    """Runs RelaySink over a real WebSocket, restartable on the same port."""

    def __init__(self, out: Path, port: int, ssl_context: ssl.SSLContext | None = None):
        self.out = out
        self.port = port
        self.ssl_context = ssl_context
        self.store: SinkStore | None = None
        self.sink: RelaySink | None = None
        self._server: Any = None

    async def start(self) -> None:
        import http

        from websockets.asyncio.server import serve

        self.store = SinkStore(self.out)
        self.sink = RelaySink(self.store, TOKEN)
        sink = self.sink

        def process_request(connection: Any, request: Any) -> Any:
            if not sink.authorise(request.headers.get("Authorization")):
                sink.rejected_connections += 1
                return connection.respond(http.HTTPStatus.UNAUTHORIZED, "no\n")
            return None

        self._server = await serve(
            sink.handle,
            "127.0.0.1",
            self.port,
            ssl=self.ssl_context,
            process_request=process_request,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.store is not None:
            self.store.close()
            self.store = None


async def _drive(
    tmp_path: Path,
    sink_port: int,
    scheme: str = "ws",
    ca_path: Path | None = None,
    run_s: float = 1.5,
    interrupt: Any = None,
) -> tuple[DurableQueue, int]:
    """Run a relay against a sink for a while. Returns (queue, records taken in)."""
    udp_port = free_udp_port()
    config = RelayConfig(
        station_id="sink-test",
        gateway_url=f"{scheme}://127.0.0.1:{sink_port}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=udp_port,
        ca_path=ca_path,
    )
    durable_queue = DurableQueue(config.queue_path)
    relay = Relay(config, durable_queue, TOKEN)
    udp = relay.start_intake()

    stop = asyncio.Event()
    sender = asyncio.create_task(_datagram_source(udp_port, stop))
    uplink = asyncio.create_task(relay.run_uplink())

    try:
        await asyncio.sleep(run_s)
        if interrupt is not None:
            await interrupt()
            await asyncio.sleep(run_s)
    finally:
        stop.set()
        await sender
        uplink.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await uplink
        relay.stop()
        udp.close()

    taken_in = durable_queue.next_seq
    return durable_queue, taken_in


async def _datagram_source(port: int, stop: asyncio.Event) -> None:
    """A steady stream of distinguishable datagrams, ~50/s."""
    counter = 0
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while not stop.is_set():
            sender.sendto(f"datagram-{counter:08d}".encode(), ("127.0.0.1", port))
            counter += 1
            await asyncio.sleep(0.02)
    finally:
        sender.close()


# --- the decoder, independently ---------------------------------------------


def test_sink_decodes_what_the_relay_encodes() -> None:
    """Two independent implementations of relay-v1 §6 must agree.

    The sink's decoder was written from the protocol document. If the document
    is wrong or ambiguous about the record layout, this is where it surfaces.
    """
    from agent.framing import Record as RelayRecord
    from agent.framing import encode_records

    originals = [
        RelayRecord(
            seq=n,
            recv_utc_ns=1_700_000_000_000_000_000 + n,
            datagram=bytes([n]) * (n + 1),
        )
        for n in range(20)
    ]

    decoded = decode_batch(encode_records(originals))

    assert [(r.seq, r.recv_utc_ns, r.datagram) for r in decoded] == [
        (r.seq, r.recv_utc_ns, r.datagram) for r in originals
    ]


def test_relay_decodes_what_the_sink_encodes() -> None:
    """And in the other direction, so the stored file is readable by both."""
    from agent.framing import decode_records

    originals = [
        Record(seq=n, recv_utc_ns=n * 1000, datagram=b"x" * n) for n in range(10)
    ]
    encoded = b"".join(encode_record(r) for r in originals)

    decoded = decode_records(encoded)

    assert [(r.seq, r.recv_utc_ns, r.datagram) for r in decoded] == [
        (r.seq, r.recv_utc_ns, r.datagram) for r in originals
    ]


def test_truncated_batch_raises_rather_than_returning_short() -> None:
    from agent.framing import Record as RelayRecord
    from agent.framing import encode_records

    encoded = encode_records([RelayRecord(seq=1, recv_utc_ns=2, datagram=b"abcdef")])

    with pytest.raises(Exception, match=r"declares|truncated"):
        decode_batch(encoded[:-2])


# --- end to end -------------------------------------------------------------


async def test_relay_to_sink_delivers_everything(tmp_path: Path) -> None:
    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port)
    await server.start()

    try:
        durable_queue, taken_in = await _drive(tmp_path, port, run_s=2.0)
    finally:
        await server.stop()

    durable_queue.close()

    store = SinkStore(tmp_path / "sink")
    store.load_all()
    states = store.all_states()

    assert len(states) == 1
    state = states[0]
    assert state.station_id == "sink-test"
    assert sorted(state.stored) == list(range(len(state.stored)))
    assert len(state.stored) > 10, "nothing meaningful was delivered"
    # Allow a small tail still in flight when the relay was cancelled.
    assert len(state.stored) >= taken_in - 100


async def test_sink_restart_resumes_without_loss(tmp_path: Path) -> None:
    """The second way to induce an outage: stop the receiver, not the network."""
    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port)
    await server.start()

    async def restart() -> None:
        await server.stop()
        await asyncio.sleep(1.0)
        await server.start()

    try:
        durable_queue, taken_in = await _drive(
            tmp_path, port, run_s=2.0, interrupt=restart
        )
    finally:
        await server.stop()

    durable_queue.close()

    store = SinkStore(tmp_path / "sink")
    store.load_all()
    state = store.all_states()[0]

    # Contiguous across the restart: the sink resumed from what was on disk.
    assert sorted(state.stored) == list(range(len(state.stored)))
    assert len(state.stored) >= taken_in - 100
    assert state.sessions >= 2, "the relay did not reconnect"
    assert state.gaps == []


async def test_records_survive_on_disk_across_sink_restarts(tmp_path: Path) -> None:
    """resume_from_seq must come from the data, not from memory."""
    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port)
    await server.start()
    try:
        durable_queue, _ = await _drive(tmp_path, port, run_s=1.5)
    finally:
        await server.stop()
    durable_queue.close()

    # A brand-new store object, as if the process had been restarted.
    reopened = SinkStore(tmp_path / "sink")
    reopened.load_all()
    state = reopened.all_states()[0]

    assert state.stored
    assert state.resume_from_seq == max(state.stored) + 1


async def test_a_bad_token_is_rejected_with_http_401(tmp_path: Path) -> None:
    """Rejection must be an HTTP 401 on the upgrade, not a WebSocket close.

    relay-v1 §3: the relay treats 401 as fatal and stops retrying. A close
    frame would leave it reconnecting forever against a credential that will
    never work.
    """
    import websockets

    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port)
    await server.start()

    try:
        with pytest.raises(websockets.InvalidStatus) as raised:
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/relay/v1",
                additional_headers={"Authorization": "Bearer wrong"},
            ):
                pass
    finally:
        await server.stop()

    assert raised.value.response.status_code == 401
    assert server.sink is not None


async def test_relay_stops_retrying_after_a_401(tmp_path: Path) -> None:
    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port)
    await server.start()

    udp_port = free_udp_port()
    config = RelayConfig(
        station_id="sink-test",
        gateway_url=f"ws://127.0.0.1:{port}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=udp_port,
    )
    durable_queue = DurableQueue(config.queue_path)
    relay = Relay(config, durable_queue, "definitely-wrong")
    udp = relay.start_intake()

    uplink = asyncio.create_task(relay.run_uplink())
    try:
        # run_uplink re-raises FatalAuthError rather than looping.
        with pytest.raises(Exception, match=r"401|rejected|Unauthorized|unauthorized"):
            await asyncio.wait_for(uplink, timeout=10.0)
    finally:
        relay.stop()
        udp.close()
        durable_queue.close()
        await server.stop()


# --- the report -------------------------------------------------------------


async def test_report_says_pass_on_a_clean_run(tmp_path: Path) -> None:
    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port)
    await server.start()
    try:
        durable_queue, _ = await _drive(tmp_path, port, run_s=1.5)
    finally:
        await server.stop()
    durable_queue.close()

    store = SinkStore(tmp_path / "sink")
    store.load_all()
    report = build_report(store)

    assert "OVERALL: PASS" in report
    assert "sink-test" in report
    assert report.isascii(), "the report is pasted into a test record"


def test_report_flags_a_hole(tmp_path: Path) -> None:
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")
    store.append(
        state,
        [Record(seq=n, recv_utc_ns=n, datagram=b"x") for n in (0, 1, 2, 5, 6)],
    )
    store.save_counters(state)
    store.close()

    report = build_report(store)

    assert "OVERALL: FAIL" in report
    assert "3-4" in report


def test_report_accepts_a_declared_gap(tmp_path: Path) -> None:
    """A reported gap is a known loss, not an unexplained one - but still a FAIL."""
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")
    store.append(
        state,
        [Record(seq=n, recv_utc_ns=n, datagram=b"x") for n in (0, 1, 2, 5, 6)],
    )
    state.gaps.append((3, 5))
    store.save_counters(state)
    store.close()

    report = build_report(store)

    assert "missing    : none" in report
    assert "gaps       : 3-4" in report
    assert "unrecoverable gap" in report


def test_report_on_no_data(tmp_path: Path) -> None:
    store = SinkStore(tmp_path / "sink")

    assert "NO DATA" in build_report(store)


def test_gap_advances_the_resume_point(tmp_path: Path) -> None:
    """Otherwise the same gap is re-reported on every reconnect, forever."""
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")
    store.append(
        state, [Record(seq=n, recv_utc_ns=n, datagram=b"x") for n in (0, 1, 2)]
    )

    assert state.resume_from_seq == 3

    state.gaps.append((3, 100))
    store.append(
        state, [Record(seq=n, recv_utc_ns=n, datagram=b"x") for n in (100, 101)]
    )
    resume = state.resume_from_seq
    store.close()

    assert resume == 102


# --- TLS --------------------------------------------------------------------


def _make_dev_certificates(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Generate a throwaway CA and server certificate with openssl."""
    ca_key = tmp_path / "ca.key"
    ca_crt = tmp_path / "ca.crt"
    srv_key = tmp_path / "server.key"
    srv_csr = tmp_path / "server.csr"
    srv_crt = tmp_path / "server.crt"
    ext = tmp_path / "server.ext"
    # OpenSSL 3 verifies these strictly. Without keyUsage on the CA the
    # handshake fails with "CA cert does not include key usage extension",
    # which is why the runbook carries the same extensions.
    ext.write_text(
        """basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:127.0.0.1
""",
        encoding="utf-8",
    )

    def run(*args: str) -> None:
        subprocess.run(args, check=True, capture_output=True)

    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_crt),
        "-days",
        "1",
        "-subj",
        "/CN=courier-dev-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    run(
        "openssl",
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(srv_key),
        "-out",
        str(srv_csr),
        "-subj",
        "/CN=127.0.0.1",
    )
    run(
        "openssl",
        "x509",
        "-req",
        "-in",
        str(srv_csr),
        "-CA",
        str(ca_crt),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(srv_crt),
        "-days",
        "1",
        "-extfile",
        str(ext),
    )
    return ca_crt, srv_crt, srv_key


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is needed to make a test cert"
)
async def test_relay_to_sink_over_tls_with_a_development_ca(tmp_path: Path) -> None:
    """The hardware test runs over TLS; verification stays on throughout."""
    ca_crt, srv_crt, srv_key = _make_dev_certificates(tmp_path)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(srv_crt), keyfile=str(srv_key))

    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port, ssl_context=context)
    await server.start()

    try:
        durable_queue, _ = await _drive(
            tmp_path, port, scheme="wss", ca_path=ca_crt, run_s=2.0
        )
    finally:
        await server.stop()
    durable_queue.close()

    store = SinkStore(tmp_path / "sink")
    store.load_all()
    state = store.all_states()[0]

    assert len(state.stored) > 10
    assert sorted(state.stored) == list(range(len(state.stored)))


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is needed to make a test cert"
)
async def test_an_untrusted_certificate_is_refused(tmp_path: Path) -> None:
    """Without the CA the relay must not connect. Verification is never off."""
    _ca_crt, srv_crt, srv_key = _make_dev_certificates(tmp_path)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(srv_crt), keyfile=str(srv_key))

    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port, ssl_context=context)
    await server.start()

    try:
        # No ca_path: the self-signed certificate is not in the system store.
        durable_queue, _ = await _drive(
            tmp_path, port, scheme="wss", ca_path=None, run_s=1.5
        )
    finally:
        await server.stop()
    durable_queue.close()

    store = SinkStore(tmp_path / "sink")
    store.load_all()

    assert store.all_states() == [], "an untrusted certificate was accepted"


# --- the events file --------------------------------------------------------


async def test_events_are_written_for_each_session(tmp_path: Path) -> None:
    port = free_tcp_port()
    server = SinkServer(tmp_path / "sink", port)
    await server.start()
    try:
        durable_queue, _ = await _drive(tmp_path, port, run_s=1.5)
    finally:
        await server.stop()
    durable_queue.close()

    events = list((tmp_path / "sink").glob("*/*.events.jsonl"))
    assert events

    kinds = [
        json.loads(line)["event"]
        for line in events[0].read_text(encoding="utf-8").splitlines()
    ]
    assert "session_open" in kinds


# --- the plaintext bind rule ------------------------------------------------
#
# The relay refuses to SEND a bearer token over ws:// to anything but loopback.
# These cover the other half: the sink refuses to ACCEPT one that way. A
# credential is only as protected as the more permissive end of the link, and
# the sink's default --host is 0.0.0.0.


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "192.168.1.50", "10.0.0.7", "example.org", "::"]
)
def test_sink_refuses_plaintext_on_a_non_loopback_host(host: str) -> None:
    with pytest.raises(SinkError, match=r"plaintext"):
        check_bind_is_safe(host, None)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_sink_allows_plaintext_on_loopback(host: str) -> None:
    """A relay and a sink on one laptop is a legitimate development setup."""
    check_bind_is_safe(host, None)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "127.0.0.1"])
def test_tls_makes_any_host_acceptable(host: str) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    check_bind_is_safe(host, context)


def test_the_refusal_says_what_to_do() -> None:
    """The reader is at a flying site with the runbook on another screen."""
    with pytest.raises(SinkError) as raised:
        check_bind_is_safe("192.168.1.50", None)

    message = str(raised.value)
    assert "--cert" in message
    assert "127.0.0.1" in message
    assert "p1-01-hardware-test" in message


def test_cert_without_key_is_refused() -> None:
    with pytest.raises(SinkError, match=r"together"):
        _tls_context(Path("server.crt"), None)

    with pytest.raises(SinkError, match=r"together"):
        _tls_context(None, Path("server.key"))


def test_neither_cert_nor_key_means_no_tls() -> None:
    assert _tls_context(None, None) is None


def test_the_two_ends_agree_on_what_loopback_means() -> None:
    """The relay and the sink must not disagree about which hosts are safe.

    If one treated ::1 as loopback and the other did not, a setup that the
    relay was willing to send a token over would be one the sink refused to
    accept it on - or worse, the reverse.
    """
    from agent.config import _is_loopback as relay_is_loopback
    from tools.relay_sink import _is_loopback as sink_is_loopback

    for host in [
        "127.0.0.1",
        "localhost",
        "::1",
        "127.0.0.5",
        "",
        "0.0.0.0",
        "192.168.1.50",
        "example.org",
        "10.0.0.7",
    ]:
        assert relay_is_loopback(host) == sink_is_loopback(host), host


# --- counters survive an unclean stop ---------------------------------------
#
# The 2026-09-22 Procedure B report said "received: 57965" against "58003
# stored", which is arithmetically impossible. The records were fsynced per
# batch; the counter was written only on the 1 Hz status tick, and the sink was
# killed before the last one. A reader cannot tell a stale counter from a real
# anomaly, so the counter now moves with the data.


def test_received_total_is_exact_without_any_status_tick(tmp_path: Path) -> None:
    """No status message is ever handled here - only appends."""
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")

    for batch in range(10):
        store.append(
            state,
            [
                Record(seq=batch * 5 + n, recv_utc_ns=n, datagram=b"x" * 16)
                for n in range(5)
            ],
        )

    # Abandon the store without close(), as a killed process would.
    reopened = SinkStore(tmp_path / "sink")
    reopened.load_all()
    recovered = reopened.all_states()[0]

    assert recovered.received_total == 50
    assert len(recovered.stored) == 50
    assert recovered.received_total == len(recovered.stored)
    store.close()
    reopened.close()


def test_duplicates_are_counted_durably_too(tmp_path: Path) -> None:
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")

    records = [Record(seq=n, recv_utc_ns=n, datagram=b"y") for n in range(4)]
    store.append(state, records)
    store.append(state, records)  # a resend after a reconnect

    reopened = SinkStore(tmp_path / "sink")
    reopened.load_all()
    recovered = reopened.all_states()[0]

    assert recovered.received_total == 8
    assert recovered.duplicate_total == 4
    assert len(recovered.stored) == 4
    store.close()
    reopened.close()


def test_the_report_never_shows_fewer_received_than_stored(tmp_path: Path) -> None:
    """The exact impossibility the 2026-09-22 run printed."""
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")
    store.append(
        state, [Record(seq=n, recv_utc_ns=n, datagram=b"z") for n in range(30)]
    )

    reopened = SinkStore(tmp_path / "sink")
    reopened.load_all()
    recovered = reopened.all_states()[0]
    report = build_report(reopened)

    assert recovered.received_total >= len(recovered.stored)
    assert "received   : 30" in report
    assert "(30 stored)" in report
    store.close()
    reopened.close()


def test_a_truncated_counters_file_does_not_lose_the_records(
    tmp_path: Path,
) -> None:
    """Corrupt counters degrade to zero; the records file is the truth."""
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")
    store.append(
        state, [Record(seq=n, recv_utc_ns=n, datagram=b"q") for n in range(12)]
    )
    counters = state.counters_path
    store.close()

    counters.write_text('{"station_id": "station-a", "epo', encoding="utf-8")

    reopened = SinkStore(tmp_path / "sink")
    recovered = reopened.state("station-a", "abc123")

    assert len(recovered.stored) == 12
    assert sorted(recovered.stored) == list(range(12))
    reopened.close()


def test_no_temporary_counter_files_are_left_behind(tmp_path: Path) -> None:
    store = SinkStore(tmp_path / "sink")
    state = store.state("station-a", "abc123")
    store.append(state, [Record(seq=0, recv_utc_ns=0, datagram=b"w")])
    store.close()

    leftovers = list((tmp_path / "sink").rglob("*.tmp"))

    assert leftovers == []
