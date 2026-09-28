# Airspace monitor: conflicts and zone incursions, live

`python -m airspace` follows the Gateway's `telemetry.*`, and for **armed**
aircraft raises:

- a **critical conflict** alert when a pair's closest point of approach
  (`ARCHITECTURE.md` §7.2) is within `t_cpa_max_s`, closer than
  `d_horizontal_min_m` horizontally and `d_vertical_min_m` vertically;
- a **zone** alert when an aircraft is inside a `no_fly` (critical) or
  `restricted` (warning) zone of `airspace_zones`, within its AMSL band.

Thresholds are the single row of `airspace_policy` in the relational
database, seeded with the Stage 0 values: 60 s, 60 m, 20 m, 800 m radius.
Each alert is published on `alert.<key>`, written to `events` when raised
and when cleared, and republished every second while active so the console's
numbers are current. The console lists active alerts, sounds a tone for an
unacknowledged critical one, and shows alerts raised before it was opened.

## Running it

```
make migrate-relational          # airspace_policy, airspace_zones
python -m airspace               # needs DATABASE_URL, NATS_URL (.env)
```

The Gateway and the console must be running for anything to reach it or be
seen. Zones are re-read every minute.

## Result, 2026-09-29

Two ArduCopter SITL aircraft (SYSID 201, 202; homes 25 m apart), commanded by
a harness in GUIDED at 30 m, with a 120 m square restricted zone 300 m north
of home. From the monitor's log, in UTC:

| Time | What the aircraft did | Alert |
|---|---|---|
| 20:26:34 | armed and climbing, 25 m apart | conflict raised (25 m < 60 m, zero relative velocity) |
| 20:27:07 | separated, 59.4 m and opening | conflict cleared |
| 20:27:26 | SITL-01 enters the zone | zone warning raised |
| 20:27:51 | head-on, 589 m apart, closing | conflict raised: CPA 2.8 m in 57.2 s |
| 20:28:00 | SITL-01 leaves the zone | zone warning cleared |
| 20:28:27 | passed each other, 58.3 m and opening | conflict cleared |
| 20:28:46 | SITL-02 enters the zone | zone warning raised |
| 20:29:25 | both returning home, converging | conflict raised: CPA 42.7 m in 57.7 s |
| 20:29:32 | SITL-02 leaves the zone | zone warning cleared |
| 20:29:57 | SITL-02 stopped, 204 m from SITL-01 (see the last note below) | conflict cleared |

16 `events` rows: one per aircraft per transition. The console showed the
head-on alert with both labels and a critical badge.

## What the run showed that tests had not

- **The first run raised nothing, correctly.** The harness believed both
  aircraft armed; the recorded `drone_state` showed them disarmed on the
  ground throughout, so the monitor had nothing to alert on. The harness now
  checks every step against the vehicle's own messages.
- **A console opened after an alert showed its numbers from the moment of
  raising** ("in 57 s" long after). Active alerts are now republished every
  second.
- **CPA is a straight-line prediction, and the clear on the return was
  correct.** Read from the recorded tracks with the P10-03 replay on
  2026-09-29 (positions and finite-difference velocities from `drone_state`):
  SITL-01 was stationary from 20:29:15 on, 0.0 m/s. SITL-02 flew towards it at
  10 m/s from 20:29:21, which projected in a straight line to a closest
  approach of about 42 m - hence the alert at 20:29:25. It decelerated from
  20:29:51 and stopped at 20:29:56, 204 m from SITL-01, and the pair stayed
  205 m apart until the recording ended at 20:30:30. The conflict cleared
  at 20:29:57, a second after the stop. So nothing was missed: the alert was
  the conservative side of a linear prediction, which cannot know an
  aircraft will stop short. The earlier note here guessed the opposite
  (that deceleration hid a real conflict); the record does not support it.
  The case where slowing *does* hide a conflict - an aircraft slowing to
  hover near another - is still worth a scenario of its own under P5-12.
