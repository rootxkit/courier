# 001 — QGC MAVLink forwarding: what the link can and cannot do

- **Status:** PROPOSED — fill in from measured results, then mark ACCEPTED
- **Date:** _(date of the test)_
- **Task:** P1-00

Everything in Phase 1 and Phase 3 rests on this. Do not fill it in from
assumption; run `tools/mavlink_probe.py` and paste real output.

## Test environment

| | |
|---|---|
| QGroundControl version | _(Help → About, exact build)_ |
| Vehicle / autopilot | _(e.g. ArduCopter 4.5.x on Pixhawk 6C)_ |
| Radio link | _(SiK 915 MHz / RFD900x / USB direct)_ |
| Ground station OS | |
| Vehicle SYSID | |
| Test date | |

## Method

```
# QGC: Application Settings -> General -> MAVLink -> Enable MAVLink forwarding
#      Host: 127.0.0.1:14445

python tools/mavlink_probe.py listen --seconds 60 --json docs/decisions/001-listen.json
python tools/mavlink_probe.py roundtrip --param SYSID_THISMAV
```

Propellers removed. The aircraft does not need to fly, or even be on battery —
USB power is enough.

## Finding 1 — message inventory and rates

_(paste the `listen` output)_

```

```

| Message | Rate (Hz) | Needed by | Present |
|---|---|---|---|
| HEARTBEAT | | liveness, mode | |
| GLOBAL_POSITION_INT | | position, tracking | |
| SYS_STATUS | | battery percent | |
| BATTERY_STATUS | | energy budget | |
| GPS_RAW_INT | | fix quality, sat count | |
| VFR_HUD | | ground speed, climb | |
| MISSION_CURRENT | | progress inference (P3-06) | |
| MISSION_ITEM_REACHED | | waypoint completion (P3-06) | |
| STATUSTEXT | | FC messages, failsafe reasons | |
| EKF_STATUS_REPORT | | health alerting (P7-10) | |

Anything missing is raised with the `SR0_*` / `SR1_*` parameters on the relevant
telemetry port. Record which parameters were changed and to what:

```

```

## Finding 2 — is the channel bidirectional?

_(paste the `roundtrip` output)_

```

```

**Conclusion:** _(TELEMETRY-ONLY / BIDIRECTIONAL)_

If bidirectional, it is still not to be relied on. It is undocumented, varies by
QGC build, and would make the server able to affect flight through a path
nobody designed for that. The plan treats the channel as telemetry-only
regardless; this finding only records the observed behaviour.

## Finding 3 — bandwidth

| | |
|---|---|
| Observed throughput | _(KiB/s from the probe)_ |
| Radio link capacity | _(57.6 kbps ≈ 7 KiB/s for SiK default)_ |
| Headroom | |
| Max vehicles on one radio net at these rates | |

If headroom is thin, either reduce stream rates or give each vehicle its own USB
radio on a distinct `NETID`.

## Consequences

Fill in what this means for:

- **P1-01 (relay):** which messages to forward, expected data rate, buffer sizing
- **P3-06 (progress inference):** whether `MISSION_CURRENT` and
  `MISSION_ITEM_REACHED` actually arrive; if not, inference falls back to
  position proximity alone and is weaker
- **P3B timing:** if the manual loop is more painful than expected, or if
  bandwidth limits vehicle count sooner than planned, `mavlink-router` moves up
- **P5-14 (pilot delay):** telemetry latency is an input to the tolerable-delay
  calculation

## Decision

_(one paragraph: what we build on the basis of the above)_
