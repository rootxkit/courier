# P1-01 test records

One entry per run of `p1-01-hardware-test.md`. Newest first.

A record is written whether the run passes or fails. A failed run that is never
written down is a failure that gets rediscovered later, at worse cost.

---

## 2026-09-24 — single-aircraft hardware check, full chain — **PASS**

One aircraft on USB, QGroundControl forwarding to `localhost:14445`, the whole
Stage 0 chain running on one machine. Procedure in
[`local-end-to-end.md`](local-end-to-end.md).

**This does not close P1-08.** That criterion is ten SITL vehicles moving in a
browser, and it needs P0-08 first. This is a hardware check of the chain, which
is a different claim and a weaker one.

### Chain

```
aircraft --USB--> QGC --UDP 14445--> relay --ws--> Gateway --> TimescaleDB
                                                          \--> NATS --> browser
```

Every hop ran as a separate process. Nothing was stubbed and no synthetic
source was substituted.

### What arrived

`tools/mavlink_probe.py listen` before starting the relay, 12 s:

| Source | Classification | Sample |
|---|---|---|
| SYSID 1 / COMP 1 | vehicle | `GLOBAL_POSITION_INT` 3.00 Hz, `HEARTBEAT` 1.00 Hz, 25 message types |
| SYSID 255 / COMP 190 | ground station | `HEARTBEAT` 1.00 Hz |

The ADR-001 shape exactly. The Gateway registered 1/1 as a vehicle and never
registered 255/190 as one.

### Rows

`drone_state`, written continuously:

```
rows: 306 -> 324 in 6s  (3.0/s)
```

**3.0 rows/s against a 3 Hz `GLOBAL_POSITION_INT` stream.** A row is emitted on
position and carries whatever else has accumulated, so the row rate is one
message's rate and not the sum of all of them.

A row as the browser received it over the WebSocket:

```
drone_id           0b63df96-30ba-41ba-b3fe-7647edb2b0ee
ts                 2026-09-24T07:44:12.913530+00:00
lat_deg            0.0
lon_deg            0.0
alt_amsl_m         0.1
alt_above_home_m   1.264
heading_deg        245.47
batt_pct           0.0
mode               STABILIZE
armed              False
gps_fix_type       1
sat_count          0
alt_agl_m present: False
```

`drone_id` is the registered binding, resolved at the record's timestamp. The
mode string came from pymavlink's own mapping, not a transcribed table.

### What "indoors" looks like, and why none of it is a fault

`gps_fix_type 1`, `sat_count 0` — no GPS fix on a bench.

`lat/lon` arrived as `0, 0`. **They were stored, and that was wrong** — see the
ruling below. They are now written as `NULL`, and the console lists such a
drone as present but unplaced rather than drawing it at 0°N 0°E.

`batt_pct 0.0`, `batt_voltage_v 0.001` — USB power, no battery attached.

`heading_deg 245.5` is real and tracks rotation; the compass works indoors.
That is what the visual check exercises.

### Two defects found by running it

**1. `localhost` resolves to IPv6 and Docker publishes IPv4 only.** The Gateway
failed to reach NATS, logging `TimeoutError` in a loop while `docker compose
ps` reported every service healthy — because they were. Measured:

| target | IPv4 `127.0.0.1` | IPv6 `::1` |
|---|---|---|
| NATS 4222 | 1 ms | times out |
| TimescaleDB 5433 | 15 ms | times out |
| Redis 6379 | 1 ms | times out |

A stack that reports healthy while nothing can reach it is the worst shape for
this failure, because the obvious diagnostic says everything is fine. Recorded
in the runbook; `infra/.env.example` still ships `localhost` because CI runs on
Linux, where it resolves to IPv4.

**2. A deprecation warning firing several times per datagram.** The Gateway log
filled with pymavlink's `.name` deprecation notice. The cause was a default
argument:

```text
getattr(message_class, "msgname", getattr(message_class, "name", ""))
```

Python evaluates the default **eagerly**, so `.name` was read on every call
even though `msgname` existed. The form looks like a fallback and is not one.
Replaced with an explicit `hasattr` check; measured 0 warnings from 50 calls
afterwards, against several per datagram before.

### The position ruling, and what the aircraft actually said

Storing `0, 0` writes a coordinate that cannot be told apart from a real one
without consulting another column, so it is only wrong at the point where
somebody forgets to check. Unknown is not a value — the same principle as
`batt_consumed_wh` being `None` rather than `0`.

The validity signal was **determined from this aircraft's own output**, read
back out of the raw archive rather than assumed:

| | |
|---|---|
| EKF flags, all 84 reports | `0xa7` = `ATTITUDE \| VELOCITY_HORIZ \| VELOCITY_VERT \| POS_VERT_ABS \| CONST_POS_MODE` |
| `EKF_POS_HORIZ_ABS` | **clear** |
| `GLOBAL_POSITION_INT` lat/lon | `0, 0` for every one of the 84 |
| `GPS_RAW_INT` | `fix_type 1`, lat/lon stale at −15.06, 26.78 — in Zambia |

`GLOBAL_POSITION_INT` is the EKF's fused estimate, not raw GPS, so
`gps_fix_type` is the wrong test — and the Zambian coordinate in `GPS_RAW_INT`
is why reading either message as the authority on the other gives a wrong
answer. `EKF_POS_HORIZ_ABS` is the flag, and `CONST_POS_MODE` being set
corroborates it: ArduPilot holding a constant position because it has no
horizontal source.

4,103 rows already written at `0, 0` were cleared to `NULL`.

### A test that deleted real data

Running the database suite against the same database found that
`ArchiveRetention.sweep()` covered **every** station. The tests set a 12 KiB
ceiling, sized for a handful of synthetic segments, and applied it to the live
station holding 3.3 MB from this run. **14,288 `archive_segments` rows had been
marked deleted across successive runs**, while every file sat untouched on
disk.

Nothing ever failed, and that is the part worth keeping: deleting a segment
whose file is already gone is *deliberately* not an error, because retention
has to be re-runnable after a crash. The tolerance that makes the sweep
restartable is the same one that made this silent.

`sweep()` now takes `only_station`, every test sweep is scoped, and the index
was repaired.

> A test that can reach data it did not create will eventually delete some.

### What found these

Three of the four defects from this run were found by **running the system
against itself**, not by writing more tests:

| Defect | Found by |
|---|---|
| Position stored as `0,0` | Reading a real row on the console |
| Retention sweeping every station | Running the database suite while the Gateway was live |
| `.name` deprecation on every datagram | Watching the Gateway's log under real traffic |
| EKF flag being the right signal | Reading the aircraft's own frames out of the archive |

None of them would have been caught by more unit tests, because each was a
property of the system *in operation*: a real row, a shared database, a log
under load, an aircraft with no fix. The unit tests all passed throughout.

> A test suite answers "does this component do what I said it does". Running
> the thing answers "is what I said still true when everything is connected".
> They are different questions and the second one found more today.

The retention finding is the one to remember, because it was a near miss
rather than a defect: the index rows it touched were live, and only the file
paths were wrong. With a matching archive root it would have deleted recorded
flight data, and nothing would have failed. The guards in
`gateway/tests/conftest.py` exist so that the next version of that mistake
cannot reach anything real.

### Not covered

- Flight. A bench aircraft does not arm, move or acquire a fix, so nothing
  here exercises position, velocity or the altitude fields under real values.
- Ten vehicles. P1-08's criterion, blocked on P0-08.
- Any outage. The relay's queue, reconnection and gap reporting were verified
  in the Procedure B run of 2026-09-22, not here.

---

## 2026-09-24 — a component verified in isolation, reported as a chain

Not a runbook procedure. Two findings from the same root, both instances of
CLAUDE.md's rule about not reporting an inference as an observation.

### 1. "The chain now runs" — it did not

The report at the end of the 2026-09-23 session said the chain "now runs
QGC -> relay -> Gateway -> TimescaleDB -> NATS -> browser" and that it was
"verified against the live stack, not mocked".

**There was no runnable Gateway at that moment.** `gateway/` held the modules —
transport, store, parser, classifier, binder, assembler, writer, publisher —
and nothing composed them. There was no `__main__.py` and no pipeline
connecting the relay-v1 server to the conversion chain; `RelayServer` stored
records and stopped there.

What was actually verified was narrower and worth stating precisely: a NATS
message published *directly* through `TelemetryPublisher` arrived at a browser
over the WebSocket. That is a real check of the last two links. It says nothing
about the six before it, because nothing was driving them.

The failure is in the reporting, not the code. Every component was tested. The
claim that they were connected was an inference from "all the parts exist",
and the word "verified" was attached to it.

**What it should have said:** "the publisher-to-browser link is verified
end to end; the Gateway that would drive it does not exist yet." That sentence
was available at the time and is shorter than the one that was written.

Found while preparing the single-aircraft hardware check, when the step "start
the Gateway" had nothing to start. `gateway/pipeline.py` and `python -m gateway`
were written then.

> **The pattern:** a chain is not verified by verifying its links. Each
> component passing its own tests is evidence about components. Saying "the
> chain runs" requires having run the chain, and the check for that is whether
> a single command exists that starts it.

### 2. A test that could never have passed on CI

`test_the_generated_units_match_the_xml_definitions` read pymavlink's XML
message definitions as an independent source for every scaling factor. It
passed locally and failed on its first CI run, reporting `xml=None` for every
field.

Measured rather than assumed, by downloading the artifacts from PyPI:

| pymavlink artifact | XML files shipped |
|---|---|
| 2.4.49 Windows wheel | 19 |
| 2.4.50 Windows wheel | 19 |
| 2.4.49 sdist | 21 |
| 2.4.50 sdist | 21 |
| 2.4.49 manylinux x86_64 | **0** |
| 2.4.50 manylinux x86_64 | **0** |

**A packaging difference between platforms, not a version change.** The CI log
showed 2.4.50 against 2.4.49 locally, which was a plausible cause and the wrong
one; checking both versions on both platforms is what separated them. Chasing
the version would have produced a pin that fixed nothing.

The test compounded it. Its helper skipped definition files that were not
there, so an absent source became an empty table and then "every unit
disagrees" — a silent degradation dressed up as a specific finding.

Both fixed: the table is extracted by `tools/refresh_mavlink_units.py` and
committed, so the comparison runs on every platform; the loader raises if the
table is missing or empty instead of comparing against nothing; and where the
XML *is* installed a second test checks the committed table against it, so the
pin cannot drift unnoticed on the machines that can tell.

---

## 2026-09-23 — an intermittent test failure, investigated

Not a runbook procedure. One run of the full suite under coverage failed two
tests at once; every run before and after passed. Recorded because the
investigation found one real defect and ruled out the obvious explanation for
the other half, and because the next occurrence should not start from zero.

### What happened

```
FAILED agent/tests/test_halfopen.py::test_a_half_open_uplink_is_detected_and_recovered
FAILED gateway/tests/test_relay_server.py::test_a_different_epoch_resumes_from_zero_independently
2 failed, 496 passed, 23 deselected in 66.69s
```

The run took **66.69 s** against a normal 85-89 s.

> **A failing run that is shorter than a passing one falsifies the timeout
> theory on its own.** A test that waits out its limit spends that time before
> failing, so a timeout can only make the suite slower. A run that finishes
> early failed *fast* - a raised exception, a refused bind, a wrong answer.
> Check the elapsed time before investigating margins: it is free, and it
> eliminates a whole class of explanation in one number.

That observation was available immediately and was not used until after the
timing hypothesis had been measured out. Reach for it first next time.

### The working hypothesis, and why it was wrong

The hypothesis was that coverage instrumentation slows execution and the
tightest margin fails first. It is a good hypothesis - that is exactly what the
`assert 65 > 65` failure turned out to be - and it is testable, so it was
tested.

**Handshake timing, 300 rounds each**, on 16 cores, contention from 32 busy
processes:

| Condition | min | median | p99 | max |
|---|---|---|---|---|
| Idle | 1.2 ms | 1.4 ms | 3.0 ms | 3.6 ms |
| All cores saturated | 1.7 ms | 1.8 ms | 81.6 ms | 90.6 ms |

The budget is websockets' 10 s open timeout. The worst case with every core
busy is **90.6 ms, or 0.9% of it** - a margin of about 110x - and 600
handshakes returned zero wrong answers. **Slowness cannot produce that
failure.** The hypothesis is disproved for the Gateway test, not merely
unconfirmed.

**Half-open detection, under coverage and full CPU saturation:**

| Condition | Detection | Limit | Theoretical worst case |
|---|---|---|---|
| Idle | 9.8 s | 40 s | 12 s |
| Coverage + 32 busy processes | 10.0 s | 40 s | 12 s |

Instrumentation moved it by 0.2 s. That margin is healthy and needed no change.

### The defect that was found

`agent/tests/test_halfopen.py` picked the relay's **UDP** intake port by
probing a **TCP** socket:

```text
udp_port = free_port()          # SOCK_STREAM
...
bind_port=udp_port              # used for UDP
```

TCP and UDP are separate port spaces. Measured directly: a port held on UDP is
bound happily by a TCP probe, and a second UDP bind of it then fails with
`WSAEADDRINUSE` (10048). The relay binds its intake socket *exclusively*, so a
collision is not a warning - it is `PortInUseError` and an immediate failure,
with nothing in the message hinting that the port was chosen wrongly.

That is a fast, hard failure, which matches a 66 s run. `test_handshake.py` and
`test_integration.py` already probed `SOCK_DGRAM` correctly; this file was the
only one that did not.

Fixed, and the knowledge moved somewhere it can travel: `tests/ports.py` offers
`free_tcp_port()` and `free_udp_port()` and deliberately no protocol-agnostic
`free_port()`, with `tests/test_port_helpers.py` failing if any test file
defines its own probe. That guard immediately found a fourth file,
`tools/tests/test_relay_sink.py`, which had its own pair - so the pattern had
already spread further than the one broken copy.

### What is still unexplained

The Gateway failure. It is not a timing margin - that is measured, above - and
it is not port exhaustion: TIME_WAIT peaked at 301 sockets against a dynamic
range of 16384 (`netsh int ipv4 show dynamicport tcp`) and drains within a
minute. The assertion itself is deterministic given the store, so a wrong
answer would have to come from somewhere other than arithmetic.

**Not reproduced** in six subsequent full runs, including two under coverage
with every core saturated. Rather than keep guessing, both tests now explain
their own timeouts: elapsed time, what was being awaited, and the observable
state at the moment of giving up. The Gateway handshake carries the measured
figures in its failure message, so the next person does not repeat this
investigation to rule slowness out again.

### Reproducing the contention

`tools` has no home for a load generator, so it lived in a scratch file:
32 processes each running a tight integer loop for the duration of the test
run, on a 16-core machine.

---

## 2026-09-22 — cost of a refused reconnect on Windows, measured

Not a runbook procedure. A bench measurement, taken while fixing a flaky
integration test, of how long a *failed* reconnect attempt costs before the
relay's backoff timer even starts.

### What was measured

A TCP port was bound, its number recorded, and the socket closed, so nothing is
listening. Ten connection attempts were then timed at three layers, all to
`127.0.0.1`. The relay's own `websockets.connect` is the top layer; the two
below it are there to locate the cost.

| Layer                         | min    | median | max    |
|-------------------------------|--------|--------|--------|
| blocking `socket.connect`     | 2.019s | 2.045s | 2.057s |
| `loop.sock_connect`           | 2.024s | 2.042s | 2.060s |
| `loop.create_connection`      | 2.030s | 2.043s | 2.056s |
| `websockets.connect`          | 2.034s | 2.045s | 2.070s |

Windows 11 Pro 26200, Python 3.13.2, websockets 17.1, `ProactorEventLoop`.

### What it means

**~2.05 s, and it is the operating system, not the library.** The figure is
identical at every layer, including a plain blocking socket with no asyncio in
the picture, so nothing in `websockets` or in the relay is responsible.

The error is `WinError 10061`, "actively refused" — an RST *was* received. It
still takes two seconds, because the Windows TCP stack does not surface the
first RST: it retransmits the SYN and only reports the refusal once its retries
are spent. On loopback, with the peer answering instantly, the entire two
seconds is retransmit backoff.

The Linux figure is **not measured**. It is expected to be near zero (an RST on
loopback is reported immediately), but that is reasoning, not evidence, and CI
runs on Linux. Measure it before relying on it.

### Why this is in the record

The ground stations run Windows — that is where QGC is. So for a station, a
reconnect attempt against a Gateway that is down costs ~2 s *before* the
documented backoff begins. The real retry period is `2.05 s + backoff`, not
`backoff`, and at the cap that is 12 s per attempt rather than 10 s.

Two consequences for P1-02, which sizes Gateway restart behaviour:

- A Gateway restart that takes longer than a few seconds is not free. Each
  station spends two seconds per attempt discovering the Gateway is still
  absent, and that cost is paid on every attempt during the restart, not once.
- With many stations reconnecting at once the arithmetic compounds: the
  connection attempts themselves arrive spread over a window two seconds wider
  than the jitter alone implies. That helps — but it is accidental help from an
  OS timer, not a property anyone designed, and it disappears the day a station
  runs on Linux.

It is also why `test_the_jittered_backoff_never_exceeds_the_documented_cap`
stubs `_session` instead of pointing the relay at a dead port. Six real
attempts would cost twelve seconds of wall clock to test arithmetic.

### Reproducing

```bash
.venv/Scripts/python - <<'EOF'
import socket, statistics, time
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
d = []
for _ in range(10):
    s = socket.socket()
    t0 = time.monotonic()
    try:
        s.connect(("127.0.0.1", port))
    except OSError as error:
        err = error
    d.append(time.monotonic() - t0)
    s.close()
print(min(d), statistics.median(d), max(d), err)
EOF
```

---

## 2026-09-22 — half-open uplink detection, measured

Not a runbook procedure: a bench measurement made with the TCP proxy fixture in
`agent/tests/blackhole.py`, which stalls a connection without closing it. This
is the case a pulled cable produces and Procedure B does not — a stopped
receiver refuses the connection immediately, while a dead link is silent.

**Method.** A WebSocket client through the proxy to a local server; the
blackhole engaged; time until the client raised `ConnectionClosed`. The stall
was placed at two points in the ping cycle, because where it lands changes the
answer by up to a full `ping_interval`.

| `ping_interval` | `ping_timeout` | `close_timeout` | stall lands | detected |
|---|---|---|---|---|
| 20 | 20 | 10 | just after a ping | **49.5 s** |
| 20 | 20 | 10 | late in the cycle | 35.0 s |
| 10 | 10 | 5 | just after a ping | 24.5 s |
| 5 | 5 | 2 | just after a ping | 11.5 s |
| 5 | 5 | 2 | late in the cycle | 8.0 s |

The formula is exact — predicted 50 / 25 / 12, measured 49.5 / 24.5 / 11.5:

```
detection  = (time to the next ping) + ping_timeout + close_timeout
worst case = ping_interval + ping_timeout + close_timeout
```

`close_timeout` is in the sum because the close handshake waits for a close
frame a dead link can never deliver. It is a third of the budget, and the term
most easily forgotten.

**End to end through the relay**, with the full stack and a live UDP source:

| Settings | Detection |
|---|---|
| 20 / 20 / 10 (websockets defaults, as shipped before) | **48.0 s** |
| 10 / 10 / 5 (now the default) | **23.0 s** |
| 5 / 5 / 2 (used by the fast test) | 9.8 s |

Cause in the relay's log: `keepalive ping timeout`, then `timed out while
closing connection`.

**Two fixture defects found while measuring**, both of which would have
produced a plausible wrong number:

1. The proxy first let the far end's **close** through the blackhole. When the
   sink's own keepalive fired first, the relay received an immediate TCP close
   — so the number measured was the sink's timeout, not the relay's. A pulled
   cable delivers no FIN; the fixture now holds closes too.
2. Bytes are **held, not dropped**. Dropping would corrupt the WebSocket stream
   and the relay would reconnect for the wrong reason.

**Caveat.** Loopback. A real link adds RTT, and a cable pull may be detected
sooner because the interface going down can fail pending sends outright. These
figures are the pessimistic case, which is the right one to design against.

Recorded in `relay-v1.md` §8 as a bounded requirement: detect within 25 s,
defaults 10 / 10 / 5, sum required to stay below P7-01's 30 s.

---

## 2026-09-22 — Procedure B (stop the receiver), local loopback — **PASS**

**Procedure:** B — the receiver is stopped and restarted, rather than the
network being cut.
**Result:** PASS.
**Scope:** this exercises relay → sink. It says nothing about QGC → relay; see
the upstream analysis below.

### Environment

| | |
|---|---|
| Relay commit | `7b6d7e2` |
| Topology | relay and sink on one machine, `ws://127.0.0.1:8443` loopback |
| Aircraft link | USB direct to the ground station (not the radio) |
| Ground station | Windows 11 Pro 10.0.26200 |
| QGroundControl | running throughout, forwarding to `127.0.0.1:14445`, untouched |
| station_id | `tbilisi-base-1` |
| epoch | `c268560c8ae2ec326ca2c621be65d424` |

Loopback rather than a second machine, so TLS was not exercised. Procedure A
over a real LAN remains outstanding.

### Timeline (UTC)

| Time | Event |
|---|---|
| 16:11:52 | Relay starts. `uplink established`, `resume_from_seq: 0`, fresh epoch |
| 16:12:49 – 16:15:49 | Phase 1, 3 min. 24,771 records delivered |
| 16:16:59 | **Sink stopped** (both processes of its tree) |
| 16:17:03 – 16:19:03 | Outage, 2 min. Queue grew to 5,089 records, seq 25399–30487 |
| 16:19:33 | **Sink restarted**, same `--out` directory |
| 16:19:34 | Relay reconnects, `resume_from_seq: 25399` |
| ~16:20:40 | Backlog fully drained, 0 records outstanding |
| 16:20:00 – 16:23:00 | Phase 3, 3 min |
| 16:23:20 | Relay and sink stopped |

### Sink report

```
========================================================================
relay-v1 sink verification report
data directory: local\sink-data
========================================================================

station_id : tbilisi-base-1
epoch      : c268560c8ae2ec326ca2c621be65d424
sessions   : 2
seq range  : 0 .. 58002  (58003 stored)
received   : 57965
duplicates : 0  (re-sent after a reconnect; harmless once deduped)
missing    : none
gaps       : none reported
relay restarts inferred from uptime_s: 0
last status: queue_depth=0 queue_bytes=0 last_datagram_age_ms=39
drops      : intake=0 cap=0

VERDICT: PASS - contiguous, nothing dropped, nothing lost.

========================================================================
OVERALL: PASS
========================================================================
```

### Relay log — outage and resume

```
16:16:59 WARNING uplink session ended   {'error': 'no close frame received or sent', 'retry_in_s': 0.5}
16:17:02 WARNING uplink session ended   {'error': '[WinError 1225] ... refused the network connection', 'retry_in_s': 1.0}
16:17:05 WARNING uplink session ended   {..., 'retry_in_s': 2.0}
16:17:09 WARNING uplink session ended   {..., 'retry_in_s': 4.0}
16:17:13 WARNING uplink session ended   {..., 'retry_in_s': 8.0}
16:17:25 WARNING uplink session ended   {..., 'retry_in_s': 10.0}
16:17:39 WARNING uplink session ended   {..., 'retry_in_s': 10.0}
16:17:51 WARNING uplink session ended   {..., 'retry_in_s': 10.0}
...
16:19:34 INFO    uplink established     {'resume_from_seq': 25399}
```

Backoff ran 0.5 → 1 → 2 → 4 → 8 → 10 → 10 → 10, capped with jitter, as
`relay-v1.md` §12 specifies.

### The result that matters

The restarted sink computed `resume_from_seq = 25399` by scanning its own
records file, and that matched the relay's `oldest_seq_held` exactly. Resumption
came from disk, not from memory. That is the property Procedure B exists to
test, and everything else in the report follows from it.

### Upstream integrity — `UPSTREAM: CLEAN`

Run afterwards over the stored capture with `python -m tools.analyze_capture`:

```
  source             frames    lost   loss %   gaps
  1/1                 57301       0   0.000%      0
  255/190               702       0   0.000%      0

UPSTREAM: CLEAN - no MAVLink sequence gaps. Nothing was lost
between QGC and the relay during this capture.
```

Nothing was lost between QGC and the relay — a link the relay's own counters
cannot see at all.

**QGC sent exactly one MAVLink frame per UDP datagram**, a constant 1.00 across
all 71 buckets. That is worth recording, because it means a dropped datagram is
exactly one lost frame: on this setup MAVLink-sequence analysis is a direct
measurement of loss rather than an approximation of it.

It is an observed behaviour of this QGC build, not a guarantee. A different
version, or `mavlink-router` at Stage 1, may coalesce. `analyze_capture.py`
therefore counts frames per datagram rather than assuming the ratio, and the
frames-per-second column is computed from frames, not datagrams — a capture
where the ratio changes will still be measured correctly, and the ratio column
will say so.

**Frame rate was flat at 82.3–83.8 frames/s across the whole capture**,
including the outage:

```
  time        datagrams/s   frames/s  frames/datagram
  16:16:52           82.5       82.5             1.00
  16:17:02           82.9       82.9             1.00   <-- outage
  16:18:32           82.5       82.5             1.00   <-- outage
  16:19:32           82.5       82.5             1.00   <-- outage
  16:19:42           82.7       82.7             1.00
```

The relay kept receiving at full rate while disconnected. An earlier apparent
variation across the outage was an artefact of measuring delivery to the sink;
these buckets are keyed on the time the relay received each datagram.

### Criteria

| Criterion | Result |
|---|---|
| One epoch | PASS — `c268560c…`, 2 sessions |
| Zero missing seqs | PASS — verified independently by decoding the records file: 58,003 entries, all unique, contiguous 0..58002 |
| Zero intake drops | PASS — 0 |
| Zero cap drops | PASS — 0 |
| Duplicates stored | PASS — 0 |

### Defects found by this run

1. **`received_total` under-reports after an unclean stop.** The report shows
   `received: 57965` against `58003 stored`, which is arithmetically
   impossible. The records file is correct; the counter was persisted only on
   the 1 Hz `status` tick and at session close, and the sink was killed with
   `Stop-Process -Force`, so the final write never happened. Fixed
   subsequently: the counter is now updated where records are appended, under
   the same fsync.

2. **Two relays and two sinks were already running when the test began**,
   left from earlier manual work. Both relays had bound UDP 14445 with
   `SO_REUSEADDR` and were splitting the forwarded stream between them — the
   hazard the runbook warns about in prose. Fixed subsequently: the bind is now
   exclusive and a second relay fails with a readable error.

Neither defect affected this run's data: the stray processes were stopped and
the queue and sink data deleted before the clean start.

### Not covered

- TLS, and therefore the certificate and CA handling in §1 of the runbook.
- A real network. Stopping the sink gives an immediate connection refusal; a
  pulled cable gives silence, which is a different detection path in the relay
  and remains untested.
- QGC → relay integrity. See `tools/analyze_capture.py` and the MAVLink-seq
  criterion added to the runbook.

---

## 2026-09-25 — ten-vehicle SITL run, and four things only a screenshot showed

Ten SITL vehicles in WSL (SYSIDs 201-210), one real aircraft (`hexa-01`,
SYSID 1), QGC forwarding to the relay on Windows. Both acceptance halves
confirmed visually: QGC showed 201-210 with distinct SYSIDs, and the console
listed 11 drones with 10 markers placed.

Four defects were then found by *looking at the running system* rather than by
any test. This is the second time that has been the dominant source of real
findings, against a suite of 478 passing tests.

### 1. Station state was computed, recorded, and never published

The console showed "Stations: none" beside a station that was connected and
streaming. Three separate faults, each of which alone would have caused it:

- `TelemetryPublisher.publish_station` had no caller anywhere outside its own
  unit test. `_Session._publish_state_change` wrote to `ingest_events` and
  stopped there. `RelayServer.trackers` even carried the docstring "for
  whoever publishes to the console" — the seam was designed and the consumer
  was never written.
- State was published only *on change*, so a console attaching after the
  station came up could never learn it.
- State was evaluated only when a `status` message arrived. `unreachable` is
  defined by the **absence** of `status`, so the one transition §9 exists to
  distinguish could not be reached while a session was open: no message, no
  evaluation. A station that went quiet held `healthy` indefinitely.

The third is the one worth remembering. It is the same shape as the relay
`gap` that shipped unexecuted and the PARAM_VALUE offset that made
BIDIRECTIONAL unreportable: **code that is structurally unable to produce one
of its own outcomes**, with tests that pass because they only ever exercise
the outcomes it can produce.

Fixed by reporting on a timer and logging on change, with the state at connect
recorded explicitly so the log does not depend on whether a tick beat the
first `status`.

### 2. The map has no base layer, and never could have

`https://demotiles.maplibre.org/style.json` loads fine. Measured, not assumed:
its tile set declares `maxzoom: 6` and its only vector layers are `geolines`,
`centroids` and `countries`. The page opens at zoom 11 and eases to 14. Above
zoom 6 there is no data at all, and even in range there are no streets in the
data. The blue is the style's own `background` layer.

Nothing is broken. The style was chosen for having no API key and no vendor
account, and the consequence — that it is not a street map — was never stated.
Choosing a provider is a licensing decision and is deferred.

### 3. Drones were listed by UUID prefix

Ten aircraft shown as `9e1e607e`, `ba7f9168`, `685def77`. Each correct, none
usable. The registry label now travels on the bus with each row and the id is
secondary.

This one was not only cosmetic: it is why the heading comparison below was
made against the wrong vehicle.

### 4. Heading: 358° in QGC, 1° in the console

Reported as a three-degree disagreement between two fields. It was neither.

Measured from the archive, for SYSID 201 over 1015 samples: circular mean
**359.40°**, maximum deviation from it **1.92°**, and every single sample
within three degrees of north. The fleet sits on the 0/360 wrap. At the end of
the run the ten vehicles read 0.26, 0.23, 0.20 … 359.99 — SITL-09 at 0.01° and
SITL-10 at 359.99° are two hundredths of a degree apart in reality and 360
apart as numbers.

So "358 and 1" is one heading read twice, on either side of the wrap, on two
vehicles that could not be told apart because of defect 3.

The fields themselves agree. Pinned from pymavlink, not memory:
`GLOBAL_POSITION_INT.hdg` is `cdeg`, `VFR_HUD.heading` is `deg`. Both derive
from ArduPilot's AHRS yaw, which is true north, as ARCHITECTURE.md specifies.
On the real aircraft at the same instant: `ATTITUDE.yaw` 254.82°,
`GLOBAL_POSITION_INT.hdg` 254.82°, `VFR_HUD.heading` 254.00°.

**A latent defect found on the way.** `drone_state.heading_deg` is
last-writer-wins between the two fields, one of which has 0.01° resolution and
one 1°. It does not currently surface, because a row is emitted by the
`GLOBAL_POSITION_INT` handler *after* that handler sets the heading, so the
`VFR_HUD` value never reaches a row — confirmed against the database: 237 of
16,642 stored headings are whole degrees, which is chance for a `cdeg` field,
not the ~50% a real interleave would give. It would surface the moment row
emission moves.

### The aircraft's firmware version is not in the archive

Searched both segments from the hardware run. No `AUTOPILOT_VERSION` and no
boot banner: capture began after boot, and QGC had already consumed the reply
to its own request. What is there is `HEARTBEAT.autopilot=3`
(ARDUPILOTMEGA), `type=2` (QUADROTOR).

Stage 0 is receive-only, so we cannot request the version ourselves. But QGC
requests it at every connect, so a relay attached *before* QGC connects would
capture the reply. That is a Stage-0-compatible way to record what each
airframe is flying, and it needs a task.

### TERRAIN_REPORT is present and empty, which vindicates dropping `alt_agl_m`

The real aircraft sent 3,280 `TERRAIN_REPORT` messages. Every one has
`loaded=0`, `pending=0`, `terrain_height=0.0`, `current_height=0.0` — the
message is emitted whether or not terrain data is aboard, and this airframe
has none.

`current_height` is documented in metres AGL. Had it been mapped to an
`alt_agl_m` column, an entire flight would have been recorded at 0.0 m above
ground: an aircraft shown as landed while flying. The value is not missing, it
is confidently wrong, which is the exact failure mode P5-00 exists to prevent.

### `stop_sitl.sh` cried wolf

The teardown reported "WARNING - 2 SITL process(es) still running" and exited
1. Both had already been killed and were awaiting reaping; a check moments
later found zero. `SIGKILL` is not synchronous, and the verification ran
immediately after it.

Worth fixing rather than tolerating for the reason the project keeps returning
to: a warning that is sometimes false is one people learn to scroll past, and
this particular warning exists because a silent teardown once left five
simulators flying.

### The Gateway does not keep up with ten SITL vehicles

Observed while verifying the fix, after a deliberate two-minute Gateway outage
to restart it on new code. Relay queue depth, sampled through the console
feed:

```
+ 0.1s  1291968
+20.1s  1327326
+40.1s  1333538
+60.1s  1339448
```

Growing by roughly 790 records per second, so the backlog from that outage
never drains at this fleet size. The relay behaved exactly as designed —
buffered, reported `buffering=True`, `data_is_lost=False`, lost nothing — and
the console said so correctly. But an outage that cannot be recovered from is
a capacity limit, and it is not yet written down anywhere as a number.

Ten simulated vehicles streaming at full rate through one station is heavier
than the Stage 0 target, so this is a bound to establish rather than a
regression. It needs its own task and a measurement that is not a side effect
of a restart.
