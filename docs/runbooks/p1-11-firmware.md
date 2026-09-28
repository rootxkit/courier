# P1-11 flight software per airframe

P1-11's criterion is that *connecting QGC to an aircraft results in a recorded
flight software version for that `drone_id`, and a vehicle that never offers
one is visibly unknown rather than silently absent.*

The Gateway records `AUTOPILOT_VERSION` when it passes through the relay, and
never asks for it: Stage 0 is receive-only. `drone_firmware` in the telemetry
database gets one row per change. The console shows `fw <version> (<git hash>)`
per drone, or `fw unknown`.

## Result, 2026-09-28

Two ArduCopter SITL aircraft (SYSID 201, 202) ran headless and bound, with
MAVProxy fanning out to the relay on 14445, through the relay and the Gateway.

| Step | Observed |
|---|---|
| 70 s of traffic, nobody asks for the version | 10,034 records archived, **no** `AUTOPILOT_VERSION` among them, `drone_firmware` empty |
| request for SYSID 201 | reply `flight_sw_version 0x4080000`, custom `66c89850`; one row: `SITL-01`, `4.8.0-dev`, `66c89850`, Gateway logs `firmware recorded` |
| the same request again | same reply, still one row |
| SYSID 202, never asked | no row |

The console was not watched during the run. What it is sent is tested: no
`firmware` for a drone with no row, which the page renders as `fw unknown`.

`0x4080000` is major 4, minor 8, patch 0, type 0 (`FIRMWARE_VERSION_TYPE_DEV`),
which is what SITL built from ArduPilot master reports.

## MAVProxy does not stand in for QGC

MAVProxy did not request the version on connect: the first 70 s held
parameters, `STATUSTEXT` and FTP traffic, and no `AUTOPILOT_VERSION`. So a
script stood in for QGC's request. It listens on the vehicle's own MAVProxy
output (14560), sends `MAV_CMD_REQUEST_MESSAGE(AUTOPILOT_VERSION)` back through
it, and MAVProxy fans the reply out to every output, the relay's included.
That is the same path QGC's request takes: a ground station asks, and the
relay sees the answer.

**Not observed here:** QGC itself requesting the version on connect. That is
TASKS.md's premise, and it is confirmed or refuted on the first session with
the real aircraft and QGC (P1-01). If it does not hold, the aircraft stays
`fw unknown`, which is the visible failure the criterion asks for.

## A reply that arrives before the first HEARTBEAT is lost

A source is classified by its HEARTBEAT (P1-07). Anything a vehicle sends
before the Gateway has seen one is unclassified and not attributed, and that
includes a version reply. In this run each vehicle's parameters arrived about
0.8 s before its first HEARTBEAT. A ground station asks for the version after
it has seen a HEARTBEAT, so the reply follows one, but a relay attached after
that point misses both.

## Procedure

`local/capacity/run/14-firmware.bat` on the development machine:

1. `alembic upgrade head` on the telemetry database;
2. `SITL_QGC_PORT=14445`, bind SITL-01..11;
3. Gateway, then relay, then 2 SITL;
4. after 20 s, request the version from SYSID 201 through 14560, twice;
5. read `drone_firmware`, stop everything, restore `sim/sitl.env`, retire the
   fleet.
