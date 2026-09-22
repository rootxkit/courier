# P1-01 test records

One entry per run of `p1-01-hardware-test.md`. Newest first.

A record is written whether the run passes or fails. A failed run that is never
written down is a failure that gets rediscovered later, at worse cost.

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
