# CLAUDE.md

Project instructions for Claude Code. Read this before doing anything in this repo.

## Project

Drone courier platform. Fleet of ArduPilot multirotors flying autonomous delivery
missions, dispatched by a central backend, supervised by human pilots, with
airspace deconfliction between drones.

Read `docs/ARCHITECTURE.md` for the system design and `TASKS.md` for the work
breakdown. Every task has an ID (e.g. `P1-03`). Always reference the task ID in
the commit message.

## Hard rules

1. **Safety-critical code is not optional.** Anything that uploads a mission,
   changes flight mode, arms a vehicle, or releases a payload must have a test
   against SITL before it is considered done. No exceptions.
2. **Never ship code that talks to real hardware without a SITL path.** The same
   code path must work against `sim_vehicle.py`.
3. **Never hardcode coordinates, altitudes, battery thresholds, or geofences.**
   All of these live in config or the database.
4. **Do not invent MAVLink message fields.** Check `pymavlink` message
   definitions before using a field name.
5. If a task is ambiguous, stop and ask. Do not guess on anything touching
   flight behaviour.

## Language

- All code, comments, docstrings, commit messages, PR descriptions, log messages,
  and variable names: **English only**.
- User-facing strings go through i18n from day one (`en`, `ka`). Never hardcode
  display text.

## Git conventions

**Commit messages must contain no AI attribution of any kind.** No
`Co-Authored-By: Claude`, no "Generated with Claude Code", no session URL
trailer, no emoji footer. A `commit-msg` hook in `.githooks/` strips them as a
safety net, but do not write them in the first place.

Format — Conventional Commits with the task ID:

```
<type>(<scope>): <subject>   [<TASK-ID>]

<optional body: what changed and why, wrapped at 72 chars>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`.
Scopes: `agent`, `gateway`, `api`, `dispatch`, `airspace`, `pilot`, `app`,
`infra`, `db`.

Examples:

```
feat(gateway): parse GLOBAL_POSITION_INT into telemetry stream   [P1-02]
fix(dispatch): include return-to-base leg in energy budget       [P4-05]
test(airspace): CPA detection for head-on converging tracks      [P5-07]
```

Rules:
- One logical change per commit. Do not batch unrelated work.
- Subject line imperative mood, no trailing period, max 72 chars.
- Never commit secrets, `.env` files, `.bin` logs, or SITL artifacts.
- Never `git push --force` on `main`.
- Branch naming: `feat/P4-05-energy-budget`, `fix/P1-02-heartbeat-timeout`.

## Repository layout

```
agent/        On-vehicle / ground-relay MAVLink agent (Python)
gateway/      MAVLink ingest + command dispatch service
api/          Core REST/WS API: orders, drones, pilots, billing
dispatch/     Assignment engine (filters, scoring, batch assignment)
airspace/     Corridor reservation, CPA, deconfliction
web-pilot/    Pilot / operator console (React)
app-customer/ Customer mobile app
infra/        docker-compose, migrations, CI, deployment
sim/          SITL launch scripts and scenario definitions
docs/         Architecture, runbooks, decision records
```

## Tech stack

| Layer            | Choice                          |
|------------------|---------------------------------|
| Agent            | Python 3.12, pymavlink, MAVSDK  |
| Gateway          | Python 3.12 asyncio             |
| API              | FastAPI + SQLAlchemy 2.x        |
| Relational DB    | PostgreSQL 16 + PostGIS 3.4     |
| Telemetry store  | TimescaleDB hypertable          |
| Cache / state    | Redis 7                         |
| Message bus      | NATS                            |
| Pilot console    | React + TypeScript + MapLibre   |
| Customer app     | React Native                    |
| Containers       | Docker Compose (dev), k8s later |

Do not add a dependency without a one-line justification in the commit body.

## Code standards

**Python**
- `ruff` for lint and format, `mypy --strict` on `gateway/`, `dispatch/`,
  `airspace/`. These three are safety-relevant; type errors are build failures.
- `pytest`, with `pytest-asyncio`. Target 80% coverage on the three modules
  above, best-effort elsewhere.
- No bare `except:`. Log with structured context (`drone_id`, `mission_id`).
- All units explicit in names: `alt_m`, `dist_m`, `batt_pct`, `speed_ms`,
  `timeout_s`. Never an unqualified `alt` or `dist`.

**TypeScript**
- `eslint` + `prettier`, `strict: true`.
- API types generated from the OpenAPI schema. Never hand-write them.

**Database**
- All schema changes via Alembic migrations. Never edit a table by hand.
- All geometry columns `SRID 4326`. Distance math on `geography`, not `geometry`.
- Timestamps `TIMESTAMPTZ`, always UTC. Convert at the display layer only.

## Coordinate and unit conventions

- Latitude/longitude: WGS84 decimal degrees. MAVLink sends `int32` at 1e7 scale —
  convert at the parser boundary, never deeper in the stack.
- Altitude: store both AMSL and AGL. **Always state which in the field name.**
  Mission planning uses AGL. Never mix them in the same calculation.
- Speed m/s, distance metres, battery percent 0-100 and watt-hours separately.
- Headings degrees true, 0-359.

## Testing

- `make sim N=<n>` launches n SITL instances with unique SYSIDs.
- Integration tests run against SITL in CI, not against hardware.
- Scenario tests live in `sim/scenarios/` as YAML: drones, orders, wind, expected
  outcome. Deconfliction work is validated by scenarios, not unit tests alone.

## What not to do

- Do not build a feature that is not in `TASKS.md`. Propose it, get it added.
- Do not refactor unrelated code while doing a task.
- Do not skip the energy-reserve check to make a test pass.
- Do not add retry loops around commands that have side effects without
  idempotency keys.
