# 002 — Drain rate: a requirement, and what is reported when it is not met

- **Status:** PROPOSED — measurements complete; the requirement awaits a ruling
- **Date:** 2026-09-27
- **Task:** P1-10

## Context

`relay-v1.md` §10 claimed a 30-minute outage "drains in seconds". On
2026-09-25, after a two-minute Gateway outage with eleven sources, the relay
queue grew by about 790 records/s and never drained. P1-10 asked for intake
and drain to be measured separately, the bottleneck to be named from a
measurement, §10 to be corrected, and a requirement to be proposed. **This
record stops at the proposal. It changes neither the protocol nor the
Gateway.**

## Method

`tools/ingest_capacity.py` sits between the relay and the Gateway as a TCP
proxy that can be severed, so an outage is a dead uplink with the Gateway still
running and warm. Intake is the relay's `meta.next_seq`; drain is the
Gateway's durable watermark, `relay_epochs.highest_contiguous_seq`. Phases:
settle, 30 s baseline, 60 s outage, 120 s recovery.

Environment: one Windows 11 development machine with 15.8 GiB of RAM. The dev
stack runs in Docker Desktop, SITL (ArduCopter 4.8.0-dev, SYSID 201-211, 4 Hz
stream rate) in WSL2, and the Gateway and relay on Windows, all over loopback.
The 11-source runs fed SITL straight to the relay on UDP 14445, with no QGC in
the path. Free memory stayed at or above 5.6 GiB, and neither drop counter
moved in any run.

## Evidence

### Intake and drain

| Sources | Intake | Max drain | Drain / intake | Baseline | Drain stopped after the cut |
|---|---|---|---|---|---|
| 1 | 193.5 /s | 290.4 /s | 1.50x | steady | 0.0 s |
| 3 | 415.9 /s | 398.4 /s | 0.96x | steady | 1.0 s |
| 11 | 1,281.2 /s | 289.4 /s | 0.23x | **saturated** | 47.6 s (14,887 records in flight) |

The 1- and 3-source rows are from the first harness runs, re-read with the
corrected in-flight check. The 11-source row is from the 13:00 run on
2026-09-27. A second 11-source run, with stage timing on, gave intake 1,201/s
and drain 268-330/s. Its outage phase was void because the Gateway's backlog
outlasted the 60 s outage. The harness now reports that case as such, where it
used to blame the proxy.

Two things follow:

- **Drain does not scale with load.** It sits near 300 records/s whether the
  station carries one aircraft or eleven.
- **Intake rises by roughly 110-140 records/s for each aircraft.** Above about
  two SITL aircraft per station, the backlog never clears.

### The bottleneck

The Gateway logs `ingest stage timings` (`gateway/stage_timing.py`). Summed
over the second 11-source run:

| Stage | Share of batch time | Calls |
|---|---|---|
| `process.resolve` | **96%** | 119,751, one per record |
| `process.parse` | 1.8% | 119,751 |
| `store` (index, watermark) | 1.1% | 214 |
| `store.archive` (zstd + fsync) | 0.1% | 214 |
| `decode` | <0.1% | 214 |

Each `resolve` call takes about 3.3 ms, because `IngestPipeline` calls
`BindingResolver.resolve` once per MAVLink message and each call runs its own
`source_bindings` SELECT. At 3.3 ms per call the ceiling is about 300
records/s, which is what the drain column shows. `resolve_batch` exists and
was written for exactly this case, but the pipeline does not call it.

The persist-before-ack fsync, the relay's SQLite commits, and the batch size
were all candidates. None of them is the constraint:

- The archive write with its fsync is 0.1% of the time.
- The relay never drops at intake.
- The Gateway receives batches faster than it stores them.

### Caveats

- **Loopback on one machine.** The per-query latency on a dedicated server will
  differ. The number of queries per record will not.
- **The sources were not bound to drones.** So `process.write` and
  `process.publish` did not run. With bound sources, drain will be lower, not
  higher.
- **Eleven SITL vehicles on one station is not a real radio net.** A real
  station is limited by its radio. `ARCHITECTURE.md` §3 plans three aircraft
  per net at reduced rates. The number that matters in production is the
  Gateway's total across all stations, since every station shares it.

## Proposal (for a ruling)

### 1. The requirement

> **At its design load, the Gateway drains at least N times the intake of all
> connected stations combined, with N ≥ 5.** Design load: the largest fleet
> size planned for the phase. That is 15 aircraft for the P5-13 soak.

N sets how long a map stays stale after an outage. For an outage of length T,
recovery takes `T / (N - 1)`:

| N | 60 s outage | 30 min outage |
|---|---|---|
| 1.5 (today, 1 source) | 120 s | 60 min |
| 2 | 60 s | 30 min |
| **5** | **15 s** | **7.5 min** |
| 10 | 6.7 s | 3.3 min |

N = 5 is proposed as the smallest value where a realistic outage recovers well
inside P7-01's escalation path. It is a judgement for a ruling, not a
measurement. At 15 SITL aircraft and ~116 records/s each, it means a drain of
at least about 8,700 records/s: 30 times today's.

### 2. What is reported when it is not met

A backlog that is not clearing must be visible as its own state, not left to
look like health. Today it looks like health: the relay is buffering
correctly, `data_is_lost` is correctly false, and the console shows an
ever-older fleet with nothing marked wrong.

This needs **no protocol change**. The Gateway already knows both things it
needs:

- **Lag.** Every stored record carries `recv_utc_ns`. So `now - recv_utc_ns` of
  the newest record the Gateway has stored is how far behind that station is.
- **Trend.** `status.queue_depth` (§8) arrives every second. Depth rising
  across consecutive `status` messages, while the session is open, means drain
  is below intake.

Proposed: a station link state **`lagging`**, beside `unreachable` and
`losing data`, entered when lag exceeds a configured threshold and depth is
rising. It carries `lag_s`, and it is published to the console like the
others. Like `unreachable`, it does not mean telemetry is lost, and the console
must say so. Where this state sits in the state machine, and its threshold,
belong to the Gateway task that implements it.

### 3. Follow-on tasks proposed

- **P1-13:** resolve bindings per batch, not per message. This is the measured
  bottleneck. Expected to raise the ceiling by a large factor. The factor is to
  be measured with the harness, not assumed.
- **P1-14:** the `lagging` station state above, with a test that produces it
  and one that clears it.

## After P1-13 (2026-09-28)

This section was added after the proposal above. It records the first
measurement against it; nothing above was changed.

Bindings are now read once per batch. Same harness and machine, 11 SITL
sources. This time the aircraft **were bound**, so `write` and `publish` ran
too. The earlier runs did not do that work, so the comparison favours the
old code.

| | Before (unbound) | After P1-13 (bound) |
|---|---|---|
| Intake | 1,281 /s | 1,197 /s |
| Max drain | 289 /s | **1,796 /s** |
| Drain / intake | 0.23x | **1.50x** |
| Baseline | saturated | steady, settled in 2 s |
| Drain stopped after the cut | 47.6 s | 1.0 s (141 records) |
| Keepalive reconnections | 15 | 0 |

The backlog from the 60 s outage (72,345 records) cleared to 769 within the
120 s recovery. That is about 2 s of recovery per second of outage.

Batch time is no longer dominated by one stage:

| Stage | Share | Calls | Per call |
|---|---|---|---|
| `store` (index, watermark) | 41% | 1,353 | ~29 ms |
| `process.resolve` | 36% | 1,353 | ~26 ms, one query per address in the batch |
| `process.write` | 11% | 1,349 | ~8 ms |
| `process.parse` | 7% | 254,315 | |
| `store.archive` (zstd + fsync) | 4% | 1,353 | |

At steady state the relay sends small batches, about 188 records each. The
remaining costs are therefore **fixed costs per batch**: a handful of database
round trips in `store` and one query per address in `resolve`. They are not
per-record costs any more. The proposed 5x is not met.

The obvious next steps are these, each to be measured before it is believed:

- cache bindings between batches, invalidated on rebinding;
- fold `store`'s round trips into fewer statements.

Free memory on the machine fell to 1.2 GiB by the end of this run, against
5.6 GiB in the runs the day before. The run is still valid, because neither
drop counter moved. But this machine is near its limit for 11 SITL vehicles.

## Reproducing

```
make up
# sim/sitl.env: SITL_QGC_PORT=14445 for the run, 14550 afterwards
wsl -d Ubuntu-24.04 -- bash -lc "cd /mnt/d/Projects/courier && ./sim/run_sitl.sh -n 11"
python -m gateway --tokens local/e2e/gateway.tokens --host 127.0.0.1 --port 8081
python -m tools.ingest_capacity --sources 11 --relay-queue local/capacity/relay-queue.sqlite3 \
    --settle-max-depth 2000 --json local/capacity/cap-11.json
python -m agent --config local/capacity/relay.toml    # gateway_url -> the proxy, :18081
```

Start the harness before the relay, because the relay must connect to the
harness's proxy.

## Ruling on the `lagging` threshold (2026-09-28)

The threshold is the Gateway's link timeout (`LINK_TIMEOUT_S`, 15 s by
default), not a separate number. Past that age a stored record can no longer
make a drone live (P1-05), so that is the moment the whole station's fleet
starts to read as link lost; `lagging` is what tells the pilot why. Implemented
in P1-14. The requirement N >= 5 above remains a proposal awaiting a ruling.

