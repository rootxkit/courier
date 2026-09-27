# Architecture

## 1. Overview

Five services plus three clients.

```
                     ┌─────────────────────────────────────┐
                     │            Clients                  │
                     │  customer app │ pilot console │ admin│
                     └───────┬─────────────┬───────────────┘
                             │ REST / WS   │
                     ┌───────▼─────────────▼───────┐
                     │        Core API             │
                     │  orders, drones, pilots      │
                     └───┬──────────────────┬──────┘
                         │                  │
              ┌──────────▼───────┐  ┌───────▼──────────┐
              │    Dispatch      │  │    Airspace      │
              │  assignment      │◄─┤  corridors, CPA  │
              └──────────┬───────┘  └───────┬──────────┘
                         │                  │
                     ┌───▼──────────────────▼───┐
                     │        Gateway           │
                     │  MAVLink in / commands out│
                     └───────────┬──────────────┘
                                 │ UDP over WireGuard
                     ┌───────────▼──────────────┐
                     │        Agent             │
                     │  ground relay or onboard │
                     └───────────┬──────────────┘
                                 │ UART / UDP
                     ┌───────────▼──────────────┐
                     │  ArduPilot flight ctrl   │
                     └──────────────────────────┘
```

Shared infrastructure: PostgreSQL+PostGIS (state), TimescaleDB (telemetry),
Redis (live drone state, TTL 15s), NATS (internal pub/sub).

## 2. Link topology

The Gateway must not know or care how MAVLink reaches it. It receives MAVLink
over UDP, identifies vehicles by `SYSID_THISMAV`, and never assumes it can send
anything back. This makes each stage below a configuration change rather than a
rewrite.

### Stage 0 — QGC forwarding (current)

Nothing is installed on the aircraft. QGroundControl runs on the ground station
and forwards the MAVLink stream it already receives.

```
Drone (ArduPilot) ──915 MHz──▶ Ground station PC
                                │
                                ├── QGroundControl (pilot UI, mission upload)
                                │     └── MAVLink Forwarding ──▶ 127.0.0.1:14445
                                │
                                └── relay process ──TLS──▶ Gateway
```

QGC setting: **Application Settings → General → MAVLink → Enable MAVLink
forwarding**, target `127.0.0.1:14445`.

**This link is telemetry-only.** QGC's forwarding is designed to fan out the
vehicle stream to observers. Whether commands injected back on that socket reach
the vehicle varies by version and is not a documented guarantee, so the Gateway
must never depend on it. All mission upload, mode changes, and arming happen
through QGC, performed by the pilot.

**Why a relay process and not a direct UDP forward to the server:**

- Raw UDP to a public endpoint has no authentication — anyone who finds the port
  can inject fake vehicle state.
- NAT means the server can never initiate anything, and UDP gives no delivery
  signal, so an internet dropout silently discards telemetry.
- The relay buffers during dropouts and replays on reconnect, so the flight
  record has no holes.

The relay is roughly 100 lines: read UDP on 14445, frame, authenticate, send
over TLS WebSocket with a disk-backed queue. It runs on the same PC as QGC.

A WireGuard tunnel plus direct UDP is an acceptable day-one shortcut, but it
solves only the authentication problem, not the buffering one.

**Constraints inherited from this topology**

| Constraint | Consequence |
|---|---|
| Range 1-3 km urban, 5-15 km open (SiK); more with RFD900x | Single-base operation only |
| Pilot must remain at the ground station | No distributed dispatch yet |
| Two failure points (radio, ground PC) | Telemetry gaps are expected, not exceptional |
| No onboard computer | No tactical avoidance independent of the FC |
| Shared radio bandwidth | 2-3 vehicles per 57.6 kbps link, realistically |

Stream rates must be budgeted. With one vehicle, position at 4 Hz is fine. With
three on one radio net, drop to 2 Hz position and 1 Hz for everything else, or
give each vehicle its own USB radio on a distinct `NETID`.

### Stage 1 — ground relay with command capability

Replace QGC forwarding with `mavlink-router` on the same PC. Still nothing on
the aircraft.

```
Drone ──915 MHz──▶ Ground PC
                    │ mavlink-router
                    ├──▶ QGroundControl (local)
                    ├──▶ Gateway (bidirectional)
                    └──▶ tlog
```

This is what unlocks server-initiated mission upload and automatic deconfliction
commands. It is a config file, not a project. Move here when the manual loop
becomes the bottleneck.

### Stage 2 — onboard link

```
Drone ── UART ──▶ RPi Zero 2 W + SIM7600 LTE ── WireGuard ──▶ Gateway
```

Adds unlimited range wherever there is LTE, telemetry that survives the pilot
driving away, store-and-forward on the aircraft itself, onboard tactical
deconfliction independent of the server, and camera-based delivery confirmation.
About 60 g and $80-120. A Jetson is only needed if computer vision is added.

**Backup links (from Stage 2 onward)**
- 915 MHz radio to the pilot station — independent of the internet.
- LoRa / ESP-NOW peer broadcast at 1 Hz for last-resort mutual awareness.

## 3. Failure domains

The system is designed so that each layer degrades independently.

| Failure                | Consequence                                        |
|------------------------|----------------------------------------------------|
| Server down            | Flight is unaffected — the mission is on the FC and the pilot has QGC. Tracking and dispatch stop |
| Internet down at ground PC | Relay buffers; flight and pilot control unaffected |
| QGC or ground PC down  | **Pilot loses control surface.** FC continues the mission or triggers RTL per `FS_OPTIONS`. This is the most serious Stage 0 failure and is why `FS_OPTIONS` must be correct before any real flight |
| Radio link loss        | ArduPilot failsafe per `FS_OPTIONS`; server sees telemetry stop |
| GPS loss               | LOITER on barometer, pilot alert, land after 20 s  |
| Battery below reserve  | Pilot alerted, redirects to base, order reassigned |

At Stage 0 the server is an observer and a planner, never a controller. Nothing
in the flight path depends on it being up. That is a real safety advantage of
starting here and should not be given away casually when moving to Stage 1.

Rule: **every safety behaviour must have a fallback that lives on the flight
controller itself**, because the flight controller is the only component that
cannot be disconnected from the airframe.

## 4. Data model

```sql
bases(id, name, geom POINT, capacity, charging_slots)

drones(id, serial, model, sysid, status, max_payload_g, max_range_m,
       battery_capacity_wh, cruise_speed_ms, avg_power_w,
       home_base_id, current_pilot_id, firmware_version)

pilots(id, name, license_ref, status, max_concurrent_drones)

drone_state(drone_id, ts, geom POINT, alt_amsl_m, alt_above_home_m,
            heading_deg, vx_ms, vy_ms, vz_ms, batt_pct, batt_voltage, mode,
            gps_fix_type, sat_count, link_quality)      -- hypertable

orders(id, customer_id, pickup_geom, dropoff_geom, weight_g,
       dimensions_mm, status, assigned_drone_id, price,
       created_at, promised_eta, delivered_at)

missions(id, order_id, drone_id, waypoints JSONB,
         corridor GEOMETRY(Polygon,4326), alt_min_m, alt_max_m,
         t_start, t_end, status, energy_budget_wh)

airspace_zones(id, name, geom POLYGON, min_alt_m, max_alt_m,
               type)   -- no_fly | restricted | corridor | base

events(id, ts, actor_type, actor_id, entity_type, entity_id,
       event_type, payload JSONB)   -- append-only audit
```

Indexes that matter: GiST on every geometry column, `drone_state(drone_id, ts
DESC)`, and a partial index on `drones(status) WHERE status = 'IDLE'`.

**There is deliberately no `alt_agl_m`.** Nothing in the telemetry carries
height above ground. `GLOBAL_POSITION_INT.relative_alt` is documented by
MAVLink as *"Altitude above home"*, which equals AGL only while the terrain
under the aircraft is at home's elevation; `GPS_RAW_INT.alt` and `VFR_HUD.alt`
are both MSL, and `GPS_RAW_INT.alt_ellipsoid` is a third datum again.

The column is absent rather than nullable. A nullable `alt_agl_m` that is
always null is an invitation to fill it from `relative_alt`, and the resulting
error is smooth, plausible and silent — an aircraft over rising ground reads as
higher above it than it is. AGL returns when P5-00 provides a terrain source,
and not before.

## 5. Order state machine

```
CREATED ──▶ ASSIGNED ──▶ EN_ROUTE_PICKUP ──▶ LOADING
   │                                             │
   │                                             ▼
   ├──▶ CANCELLED                        EN_ROUTE_DROPOFF
   │                                             │
   │                                             ▼
   └──▶ FAILED ◀──── RETURNING ◀──────────  DELIVERING
                                                 │
                                                 ▼
                                             COMPLETED
```

Implemented as an explicit state machine with a transition table, not scattered
conditionals. Every transition writes to `events`. Illegal transitions raise.

## 6. Dispatch algorithm

**Step 1 — hard filters.** A drone is eligible only if all hold:

```
status = IDLE
max_payload_g >= order.weight_g
telemetry fresher than 20 s
energy budget satisfied (see below)
pilot available (if regulation requires 1:1)
wind at route altitude below threshold
```

**Step 2 — energy budget.** This is the single most important guard in the
system.

```
dist_total = d(drone → pickup) + d(pickup → dropoff)
           + d(dropoff → nearest base)

required_wh = (dist_total / cruise_speed_ms) * avg_power_w / 3600
              * wind_factor
              * 1.35            # 35% reserve, non-negotiable

eligible if required_wh <= battery_capacity_wh * (batt_pct / 100)
```

Never relax the reserve to make an assignment succeed.

**Step 3 — scoring.** Among survivors of the filters:

```
score = 1.0 * eta_pickup_min
      + 0.8 * (100 - battery_after_mission_pct) / 10
      + 1.5 * airspace_conflict_penalty
      + 0.3 * base_imbalance_penalty
```

Weights live in config so they can be tuned without a deploy.

**Step 4 — batch assignment.** Pending orders are collected on a 5-second tick
and assigned globally with the Hungarian algorithm
(`scipy.optimize.linear_sum_assignment`) rather than greedily. Greedy assignment
degrades average wait time by 15-25% under load.

## 7. Airspace and deconfliction

Three independent layers. No single layer is trusted alone.

### 7.1 Strategic — before takeoff

Each mission reserves a 4D corridor: route buffered by ±40 m, an altitude band,
and a time window. Before approval:

```sql
SELECT id FROM missions
WHERE status IN ('PLANNED','ACTIVE')
  AND ST_Intersects(corridor, :new_corridor)
  AND tstzrange(t_start, t_end) && tstzrange(:ts, :te)
  AND numrange(alt_min_m, alt_max_m) && numrange(:amin, :amax);
```

Resolution order: different altitude layer → delay departure 60-120 s → reroute.

**Semicircular altitude rule** — prevents head-on encounters structurally.

**The layers are AMSL, not AGL.** The rule works by guaranteeing that two
aircraft on opposing tracks are at different heights, and that guarantee only
holds if both measure height from the *same datum*. AGL does not provide one:
two aircraft 15 m apart in AGL over terrain that differs by 15 m are at the
same height, and two aircraft in the same AGL layer over sloping ground are
not. Either way the error is smooth, plausible and unsignalled — and it lands
in §7.2's `d_alt < 20 m` test, which is the last check before an alert.

A layer set is derived from a **reference elevation** for the operating area,
normally the base's elevation AMSL:

```
track 000°-179°  →  reference + 60, +90, +120 m AMSL
track 180°-359°  →  reference + 75, +105, +135 m AMSL
```

Every aircraft sharing an operating area must use the same reference. Two
stations with different base elevations do not share a layer set, and missions
that cross between them are deconflicted in AMSL directly rather than by layer.

**Layers are valid only over terrain within a bounded range of the
reference.** Fixed-AMSL layers mean ground clearance varies with the terrain
underneath: at `reference + 60 m` over ground that rises 40 m above the
reference, clearance is 20 m. So the usable terrain range follows from the
lowest layer and whatever minimum clearance applies:

```
max_terrain_rise_m = lowest_layer_offset_m - minimum_clearance_m
```

Both inputs are configuration, not constants in code — minimum clearance is a
regulatory figure and is not encoded here. Checking an operating area against
this bound needs terrain elevation, which is **P5-00**; until that exists, the
bound is stated and unenforced, and that is a known gap rather than an
oversight.

### 7.2 Tactical — in flight, server side

On each telemetry tick, find neighbours within 800 m and compute closest point of
approach:

```
rel_pos = p2 - p1
rel_vel = v2 - v1
t_cpa   = -(rel_pos · rel_vel) / |rel_vel|²        # if |rel_vel| > 0
d_cpa   = |rel_pos + rel_vel * t_cpa|
```

Alert when `t_cpa < 30 s` AND `d_cpa_horizontal < 60 m` AND `d_alt < 20 m`.

**Resolution must be deterministic** so that both aircraft derive the same
answer from the same data:

- Lower `drone_id` maintains course and altitude.
- Higher `drone_id` descends 20 m, or enters LOITER for 45 s if descent is
  unavailable.
- Both log a `STATUSTEXT` and an `events` row.

Never resolve by "whoever the server contacts first" — that is not reproducible
and not auditable.

**At Stage 0 the resolution is advisory.** The server computes the identical
answer but issues it as a pilot instruction rather than a command: a critical
alert naming both aircraft, the time to closest approach, and the prescribed
action. The pilot executes it in QGC. The deterministic rule matters just as
much here — two pilots looking at two consoles must be told compatible things.

Because the pilot is in the loop, the alert threshold at Stage 0 is wider:
`t_cpa < 60 s` instead of 30 s, to allow human reaction time. Tighten it when
the command channel exists.

### 7.3 Onboard — last line

- ArduPilot `FENCE_*` polygon uploaded with every mission, enforced by the FC.
- `AVOID_*` parameters plus proximity sensor if fitted.
- 1 Hz peer broadcast over LoRa/ESP-NOW: `{sysid, lat, lon, alt, vx, vy, vz, ts}`.
  Fully independent of the server and the internet. The same transmitter can
  serve Remote ID duty where that is mandated.

## 8. Mission delivery

### Stage 0 — plan handoff (current)

The server does not command the aircraft. It plans, validates, deconflicts, and
hands the pilot a file.

```
order → dispatch selects drone → mission generator builds waypoints
      → airspace reserves corridor → validation
      → export .plan → pilot downloads → pilot loads in QGC → pilot flies
      → server infers progress from telemetry
```

The exported artifact is a QGroundControl `.plan` file: JSON with `fileType:
"Plan"`, a `mission` block of `MAV_CMD` items, a `geoFence` block carrying the
approved corridor as a polygon, and `rallyPoints` for the nearest bases. The
pilot opens it in QGC and uploads it to the vehicle. Generating a real `.plan`
rather than a coordinate list means the same generator works unchanged when
Stage 1 arrives — only the transport swaps out.

**Progress inference.** Without a command channel, order state is derived from
telemetry rather than from acknowledgements:

| Signal | Inference |
|---|---|
| `MISSION_CURRENT` sequence number | which leg the aircraft is on |
| `MISSION_ITEM_REACHED` | waypoint completion |
| Mode transition to AUTO, armed, altitude rising | mission started |
| Position within 30 m of pickup, descending | arriving at pickup |
| Disarmed at dropoff | delivery attempt |

Inference is never trusted for anything irreversible. Payload release and
delivery completion require the pilot to confirm in the console. The inference
exists to keep the customer's tracking view live and to detect deviation, not to
drive the state machine unattended.

**Deviation detection.** The server holds the approved mission, so it can
compare actual track against planned track continuously. Deviation over 50 m, or
an altitude outside the approved band, raises a pilot alert. This works fine
without a command channel and is one of the most useful things the server does
at this stage.

### Stage 1 — direct command channel (deferred)

When `mavlink-router` replaces QGC forwarding, the same generator output is
uploaded by the server instead of by the pilot.

Commands are never fire-and-forget:

```
enqueue(command, idempotency_key)
  → send over MAVLink
  → await COMMAND_ACK / MISSION_ACK with timeout
  → retry up to 3 times with backoff
  → on exhaustion: mark FAILED, alert pilot, do not silently continue
```

Mission upload follows the MAVLink mission protocol properly:
`MISSION_COUNT` → `MISSION_REQUEST_INT` → `MISSION_ITEM_INT` → `MISSION_ACK`,
with a read-back verification pass. Re-uploading the same mission with the same
idempotency key is a no-op.

## 9. Pilot supervision model

One pilot supervises 1-3 drones, by exception rather than by continuous
watching. The console surfaces alerts; the pilot acts on alerts.

Alert types: battery below 30%, GPS fix loss, link loss over 10 s, deconfliction
event, wind above threshold, deviation from route over 50 m, EKF variance,
mission upload failure.

Takeover: one button switches the vehicle to GUIDED or LOITER and gives the pilot
a virtual joystick plus a WebRTC video feed. Every intervention is recorded in
`events` with pilot ID and timestamp.

## 10. Open decisions

Record the outcome of each in `docs/decisions/` as it is made.

- Gateway in Python vs Go — start Python, revisit if ingest exceeds ~50 drones.
- Payload release mechanism: servo latch vs winch.
- Whether regulation forces 1:1 pilot-to-drone, which changes dispatch and unit
  economics significantly.
- Weather data source and the exact wind threshold per airframe.
