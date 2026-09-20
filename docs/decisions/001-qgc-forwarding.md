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
| Probe commit (`git rev-parse --short HEAD`) | |

## Method

```
# QGC: Application Settings -> General -> MAVLink -> Enable MAVLink forwarding
#      Host: 127.0.0.1:14445

python tools/mavlink_probe.py listen --seconds 60 --json docs/decisions/001-listen.json
python tools/mavlink_probe.py roundtrip --param SYSID_THISMAV
```

Propellers removed. The aircraft does not need to fly, or even be on battery —
USB power is enough.

## Tool provenance — which probe produced these numbers

**Any roundtrip result produced before commit `c05ee6e` is void.** Record the
probe commit in the table above so a later reader can tell which implementation
the findings came from.

The first implementation of `roundtrip` read `param_id` at PARAM_VALUE payload
offset 4. It is at offset 8: MAVLink orders payload fields by descending type
size, so `param_value` (4 bytes), `param_count` (2) and `param_index` (2)
precede it. The old slice returned the count and index bytes instead of a name,
so the comparison against the requested parameter never matched under any
circumstances.

That version could therefore only ever print `TELEMETRY-ONLY`. It was not
capable of detecting a bidirectional link, and it would not have failed or
warned while being incapable of it — it returned the answer we expected, which
is why the defect survived review. **An expected result is not evidence.** The
only way to tell that version's output from a real measurement is the commit it
came from, which is why the table records one.

The same version also sliced payload bounds backwards from the end of the frame
(`frame[10:-2]`), which silently consumes signature bytes on a signed MAVLink v2
frame.

Scope of the damage, for the avoidance of doubt:

- **`roundtrip` results are void.** Both defects lived in `cmd_roundtrip`.
- **`listen` results are unaffected.** `cmd_listen` only calls `split_frames`
  and `decode_header`. `split_frames` accounted for the 13-byte signature block
  correctly from the first version, and `decode_header` reads fixed header
  offsets that signing does not move. Neither ever sliced a payload.

Fixed in `c05ee6e`, with the offset now pinned by a test that builds frames with
pymavlink rather than asserting against hand-written bytes
(`tools/tests/test_mavlink_probe.py`).

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
