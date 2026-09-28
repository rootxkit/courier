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
| 20:29:57 | still returning (see the last note below) | conflict cleared |

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
- **CPA is a straight-line prediction.** On the return, the conflict cleared
  at 20:29:57 while the aircraft were still heading for homes 25 m apart. The
  likely reason, not yet confirmed from the recorded tracks: slowing towards
  a stop moves the straight-line closest approach beyond 60 s. §7.2 specifies linear CPA; an aircraft that slows to hover near
  another is caught again only once relative velocity is near zero and the
  current distance decides. Worth a scenario of its own under P5-12.
