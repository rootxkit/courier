# courier

Drone courier platform. A fleet of ArduPilot multirotors flies autonomous
delivery missions, dispatched by a central backend, supervised by human pilots,
with airspace deconfliction between aircraft.

At the current stage (Stage 0) the server is an **observer and a planner, never
a controller**: it ingests telemetry, plans and validates missions, reserves
airspace corridors and raises alerts, but every command to an aircraft is issued
by a human pilot through QGroundControl. See `docs/ARCHITECTURE.md` §2.

- System design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Work breakdown and task IDs: [`TASKS.md`](TASKS.md)
- Working agreements for this repo: [`CLAUDE.md`](CLAUDE.md)

## Layout

```
agent/         On-vehicle / ground-relay MAVLink agent (Python)
gateway/       MAVLink ingest + command dispatch service
api/           Core REST/WS API: orders, drones, pilots, billing
dispatch/      Assignment engine (filters, scoring, batch assignment)
airspace/      Corridor reservation, CPA, deconfliction
common/        Shared logging and configuration library
web-pilot/     Pilot / operator console (React)
app-customer/  Customer mobile app (React Native)
infra/         docker-compose, migrations, CI, deployment
sim/           SITL launch scripts and scenario definitions
docs/          Architecture, runbooks, decision records
```

## Getting started

Requires Python 3.12+, Docker with Compose v2, and GNU Make. On Windows use Git
Bash or WSL — the Makefile and the SITL scripts are POSIX shell.

```bash
make hooks           # install the commit-msg hook (do this first)
make venv            # create .venv and install the Python workspace
cp infra/.env.example .env
make up              # start PostGIS, TimescaleDB, Redis and NATS
make lint            # ruff + mypy --strict on the safety-relevant modules
make test            # pytest
make down            # stop the stack
```

SITL, for simulated vehicles (requires ArduPilot's `sim_vehicle.py` on `PATH`):

```bash
cp sim/sitl.env.example sim/sitl.env    # set SITL_HOME for your test area
make sim N=10                           # 10 vehicles, SYSID 1..10
make sim-stop
```

Run `make help` for the full target list.

## Conventions

Units are always explicit in names (`alt_agl_m`, `batt_pct`, `timeout_s`), all
timestamps are `TIMESTAMPTZ` in UTC, and all geometry is SRID 4326. Coordinates,
altitudes, battery thresholds and geofences live in configuration or the
database — never in code. The long form is in `CLAUDE.md`; read it before
committing.
