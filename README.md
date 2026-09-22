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

**The supported environment is WSL2 — follow
[`docs/DEV_SETUP_WSL.md`](docs/DEV_SETUP_WSL.md).**

The toolchain this project lives on is POSIX-native: ArduPilot's `waf` build,
`sim_vehicle.py`, `mavlink-router`, MAVProxy. Each is a separate fight on
Windows, and from Phase 1 onward they are used daily. QGroundControl stays on
Windows and connects over UDP; it does not care where the other end lives.

Once set up:

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

Probing a QGC forwarding link, to answer P1-00:

```bash
make probe                    # inventory message types and rates
make probe-roundtrip          # is the channel bidirectional?
```

Run `make help` for the full target list.

**`make` targets need a POSIX shell.** The `Makefile` declares
`SHELL := /bin/bash` because its recipes are POSIX shell, so run them from WSL
or Git Bash. A native Windows `make.exe` driven from PowerShell or `cmd` has no
`/bin/bash` to find. There is deliberately no Windows shell fallback in the
`Makefile`: WSL2 is the supported path, and the fallback would be dead code.

<details>
<summary><strong>Windows without make</strong></summary>

A stopgap while migrating to WSL2. Everything here is the raw command the
equivalent target wraps, in PowerShell. In Git Bash, swap `\` for `/`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'

git config core.hooksPath .githooks                                  # make hooks
docker compose -f infra\docker-compose.dev.yml up -d --wait          # make up
docker compose -f infra\docker-compose.dev.yml down                  # make down
.\.venv\Scripts\mypy                                                 # make lint
.\.venv\Scripts\ruff check .                                         #   ...
.\.venv\Scripts\ruff format --check .                                #   ...
.\.venv\Scripts\ruff format .                                        # make fmt
.\.venv\Scripts\pytest -m 'not sitl and not slow'                   # make test
.\.venv\Scripts\python tools\mavlink_probe.py listen                 # make probe
```

`make sim` has no Windows equivalent: it needs ArduPilot's `sim_vehicle.py`,
which is the main reason the supported environment is WSL2.

**Do not "fix" the `python3` fallback in the `Makefile` to match Linux.** When
no `.venv` is present the `Makefile` falls back to `python` on Windows and
`python3` elsewhere, and that asymmetry is deliberate. Windows ships a
`python3.exe` App Execution Alias that is not an interpreter: it prints a
Microsoft Store advertisement and exits non-zero. Anything invoking it reports a
confusing failure rather than a missing interpreter — this cost real debugging
time once already, when it silently swallowed the SITL launcher's child
processes and surfaced as a bogus "SITL did not open its TCP port" error.

</details>

## Conventions

Units are always explicit in names (`alt_agl_m`, `batt_pct`, `timeout_s`), all
timestamps are `TIMESTAMPTZ` in UTC, and all geometry is SRID 4326. Coordinates,
altitudes, battery thresholds and geofences live in configuration or the
database — never in code. The long form is in `CLAUDE.md`; read it before
committing.
