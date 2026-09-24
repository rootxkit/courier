# Local end-to-end run: one aircraft on USB

Bringing up the whole Stage 0 chain on one machine, against a real aircraft
connected over USB with QGroundControl forwarding.

```
aircraft --USB--> QGC --UDP 14445--> relay --ws--> Gateway --> TimescaleDB
                                                          \--> NATS --> console
```

## What this proves, and what it does not

It proves the chain carries real MAVLink from an aircraft to a browser: parsed,
classified, bound to a `drone_id`, converted to SI, stored and published.

**It does not close P1-08.** That criterion is ten SITL vehicles moving in the
browser under 500 ms end to end, and it needs P0-08 (a WSL2 environment that
can build ArduPilot). This is a *single-aircraft hardware check* and should be
recorded as one.

It also does not exercise flight: an aircraft on a bench has no GPS fix, does
not arm, and does not move. See "What healthy looks like indoors" below, which
matters because several fields look alarming and are not.

## Prerequisites

- The dev stack: `make up`
- `.env` at the repository root: `cp infra/.env.example .env`
- QGroundControl running, with **Application Settings -> General -> MAVLink ->
  Enable MAVLink forwarding** ticked and pointed at `localhost:14445`
- An aircraft registered and bound (below)

### Use `127.0.0.1`, not `localhost`, in `.env` on Windows

Measured 2026-09-24 on this machine: Docker Desktop publishes container ports
on **IPv4 only** (`0.0.0.0:4222`), while `localhost` resolves to `::1` first.
Every connection then hangs until it times out, and the error says nothing
about IPv6:

| target | IPv4 `127.0.0.1` | IPv6 `::1` |
|---|---|---|
| NATS 4222 | connects in 1 ms | times out |
| TimescaleDB 5433 | connects in 15 ms | times out |
| Redis 6379 | connects in 1 ms | times out |

`infra/.env.example` ships `localhost` because CI runs on Linux, where this
does not happen. On Windows, rewrite the three URLs to `127.0.0.1` after
copying. The symptom otherwise is a Gateway that logs `nats: encountered
error ... TimeoutError` in a loop while `docker compose ps` says every service
is healthy — because they are.

## One-time: register the aircraft

Creates the `known_drones` identity and the `source_bindings` row that maps the
MAVLink address to it. Without a binding the telemetry is archived and marked
*unclaimed*, never written to `drone_state`.

```bash
python tools/register_aircraft.py \
    --label hexa-01 --station tbilisi-base-1 --sysid 1 --compid 1
```

`tools/register_aircraft.py` is a placeholder. **P2-05 replaces it**, and must
project into `known_drones` whenever a drone is registered or retired.

The binding starts *now* by default and is not retroactive, so telemetry
captured before it stays unclaimed. `--from` moves the start back; the
exclusion constraint refuses it if the address was bound to another airframe
over that interval.

## Runtime configuration

Kept in `local/`, which is gitignored in full.

```
local/e2e/gateway.tokens    station_id: token   (the Gateway's side)
local/e2e/relay.token       the token alone     (the relay's side)
local/e2e/relay.toml        station id, gateway URL, UDP port, queue
local/e2e/archive           raw archive segments
```

The token is a shared secret and belongs in neither the repository nor a
command line. Spec §12 question 1 still owns where station tokens really live.

## Start order

The order matters: each process depends on the one before it being up.

**1. The stack.** Everything else needs the database and the bus.

```bash
make up
```

**2. The Gateway.** Binds the relay-v1 endpoint. Start it before the relay so
the relay's first connection succeeds rather than entering backoff.

```bash
python -m gateway --tokens local/e2e/gateway.tokens --host 127.0.0.1 --port 8081
```

Wait for `gateway listening`.

**3. The relay.** Binds UDP 14445 **exclusively**, so nothing else may hold it
— including `tools/mavlink_probe.py`. Stop the probe before starting the relay.

```bash
python -m agent --config local/e2e/relay.toml
```

Wait for `uplink established`.

**4. The console.**

```bash
make console
```

Then open **http://127.0.0.1:8000**.

## Ports

| Port | Process | Notes |
|---|---|---|
| 14445/udp | relay intake | QGC forwards here. Bound exclusively |
| 8081/tcp | Gateway | relay-v1 WebSocket, `/relay/v1` |
| 8000/tcp | console | map page and `/ws/telemetry` |
| 4222/tcp | NATS | from the stack |
| 5433/tcp | TimescaleDB | from the stack |
| 6379/tcp | Redis | from the stack |

## Checking it works, without opening a browser

Is the aircraft forwarding at all — run this **before** the relay, since both
bind 14445 exclusively:

```bash
python tools/mavlink_probe.py listen --seconds 12
```

Are rows arriving:

```sql
SELECT count(*) FROM drone_state;
SELECT ts, heading_deg, mode, armed, gps_fix_type FROM drone_state
  ORDER BY ts DESC LIMIT 1;
```

The row rate should be the rate of `GLOBAL_POSITION_INT` and nothing else —
a row is emitted on position and carries whatever else has accumulated.
Measured 3.0 rows/s against a 3 Hz stream.

## What healthy looks like indoors

On a bench, several fields look wrong and are not:

| Field | Value | Why |
|---|---|---|
| `lat_deg`, `lon_deg` | `0.0` | No GPS fix indoors. MAVLink cannot say "position unknown" in `GLOBAL_POSITION_INT`, so the autopilot sends zeroes. They are stored rather than discarded, because 0,0 is a real place an aircraft could be |
| `gps_fix_type` | `1` | No fix |
| `sat_count` | `0` | No satellites |
| `batt_pct` | `0.0` | On USB power with no battery attached |
| `armed` | `false` | Correct on a bench |
| `heading_deg` | real | The compass works indoors. **Rotate the aircraft and this follows** |

The marker therefore plots at 0°N 0°E and the map centres there. That is the
data being honest, not the map being broken.

### The first second after each connection

The Gateway logs `unclaimed source ... the source has never sent a HEARTBEAT`
once per address at startup. `HEARTBEAT` arrives at 1 Hz while position
arrives at 3 Hz, so for under a second the source has not said what it is.
Those records are archived and deliberately **not** written to `drone_state` —
classification gates resolution, and the absence of a judgement is not a
judgement. It resolves as soon as the first heartbeat lands and is not
repeated.

## Clean shutdown

Stop in reverse order: console, relay, Gateway, then the stack. Each responds
to Ctrl-C.

If they were started in the background, on Windows:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'api\.console|-m agent|-m gateway' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

```bash
make down
```

### Confirm nothing is left listening

Not optional. A relay still holding UDP 14445 makes the next run fail with
`PortInUseError`, and a Gateway still bound to 8081 makes the next one fail to
start.

```bash
netstat -ano | grep -E ":(14445|8081|8000) "
```

Expect no `LISTENING` lines. `TIME_WAIT` entries are fine and drain on their
own.

## Leftover state between runs

- `local/e2e/relay-queue.sqlite3` — the relay's durable queue. Keeping it
  resumes the same epoch and replays anything unacknowledged, which is the
  design working. Delete it to start a fresh epoch.
- `local/e2e/archive` — raw segments, hourly, zstd. Never deleted
  automatically outside the retention sweep.
- `drone_state` and `source_bindings` persist in the database. The binding is
  one-time; do not re-register between runs.
