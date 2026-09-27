# Development environment: SITL in WSL2

**Only SITL lives in WSL.** ArduPilot's `waf` build and `sim_vehicle.py` are
POSIX-native and are a separate fight on Windows, so they run inside
Ubuntu-24.04. Everything else — the repository, the Gateway, the relay agent,
the console, the test suite and the Docker stack — runs on Windows.

## Why the split, rather than moving everything into WSL

This document used to argue the opposite, and the argument was wrong for one
reason it did not consider: **the relay ships to a pilot's Windows laptop.**

The relay agent runs in the field, on a bare Windows machine, next to
QGroundControl, with no WSL and no container runtime. Developing it inside WSL
would mean the thing we test every day is not the thing we deploy — a different
socket stack, a different filesystem, a different process model. The Stage 0
guarantee is about what the relay does on a real laptop during a real flight,
and that is not a property you can establish on a platform you do not fly.

So the boundary follows deployment, not convenience:

| Runs where | What |
|------------|------|
| WSL (Ubuntu-24.04) | ArduPilot build, `sim_vehicle.py`, MAVProxy |
| Windows | repo, Gateway, relay agent, console, tests, Docker Desktop, QGC |

The cost is that SITL traffic crosses the WSL boundary. Mirrored networking
makes that cost approximately zero, which is why it is a prerequisite rather
than an optimisation.

## Topology

SITL never talks to the relay directly. The chain is the one a pilot flies:

```
SITL (WSL) --UDP 14550--> QGroundControl (Windows)
                              |
                              | QGC forwarding
                              v
                       relay agent --UDP 14445--> (its own socket)
                              |
                              | relay-v1 over WebSocket
                              v
                         Gateway :8081 --> TimescaleDB + NATS
                                                       |
                                                       v
                                              console :8000
```

Pointing SITL straight at 14445 would work and would prove nothing: it skips
QGC, which is the component that actually decides what a station receives.

## Setup

### 1. Install WSL2 with Ubuntu 24.04

```powershell
wsl --install -d Ubuntu-24.04
wsl --update
```

### 2. Enable mirrored networking

This is what makes `127.0.0.1` mean the same thing on both sides, so SITL in
WSL can reach QGC on Windows with no port forwarding and no IP lookup.

Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
firewall=true
dnsTunneling=true
autoProxy=true
```

Then from PowerShell:

```powershell
wsl --shutdown
```

**Verify it is actually active before relying on it.** From WSL, a service
listening on Windows loopback must be reachable:

```bash
# With `make up` running on Windows, this must print 000 (connection made,
# no HTTP response) rather than hanging or refusing.
curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 http://127.0.0.1:5433
```

If it is not active, stop and fix it. Do not work around it by looking up the
WSL IP: an environment where `127.0.0.1` means two different things is one
where every networking symptom has two possible causes, and the hours go into
telling them apart. Windows 10 cannot do mirrored networking at all; this
setup requires Windows 11.

### 3. Docker Desktop on Windows

The stack (`make up`) runs on the Windows side. WSL integration is **not**
needed, because nothing in WSL talks to the database.

One wrinkle that has cost real time: Docker publishes ports on IPv4 only,
while `localhost` on Windows resolves to `::1` first. Use `127.0.0.1`
explicitly in `.env`, never `localhost`, or the Gateway will log connection
timeouts in a loop while `docker compose ps` insists everything is healthy.

### 4. The repository stays on Windows

It is not cloned into WSL. WSL reaches it at `/mnt/d/Projects/courier`, which
is slow for builds but irrelevant here: nothing is *built* from that path. The
launcher scripts and generated parameter files are the only things WSL reads
there, and ArduPilot itself is built inside the WSL filesystem where it
belongs.

### 5. ArduPilot SITL, inside WSL

```bash
cd ~
git clone --recurse-submodules https://github.com/ArduPilot/ardupilot.git
cd ardupilot
git checkout "$ARDUPILOT_REF"
git submodule update --init --recursive
Tools/environment_install/install-prereqs-ubuntu.sh -y
. ~/.profile
./waf configure --board sitl
./waf copter
```

`ARDUPILOT_REF` is not a value this document owns. Take it from the `sitl` job
in `.github/workflows/ci.yml`, which is the single place it is declared, and
build the same one locally. A local build that drifts from CI turns "works
here, fails there" into a routine event, and the drift is silent: a SITL of a
different vintage still flies, still streams telemetry, and still passes
everything except the thing that changed between the two.

Check it before building, rather than assuming what it says:

```bash
grep ARDUPILOT_REF /mnt/d/Projects/courier/.github/workflows/ci.yml
```

MAVProxy goes in its own virtualenv so it cannot fight the system Python:

```bash
python3 -m venv ~/venv-ardupilot
~/venv-ardupilot/bin/pip install MAVProxy
```

The first ArduPilot build takes 10-20 minutes. Later builds are incremental.

### 6. Point the launcher at your paths

`sim/sitl.env` is gitignored; copy the template and edit:

```bash
cp sim/sitl.env.example sim/sitl.env
```

The two paths that are yours, not the repository's:

```bash
SIM_VEHICLE=/home/<you>/ardupilot/Tools/autotest/sim_vehicle.py
MAVPROXY=/home/<you>/venv-ardupilot/bin/mavproxy.py
```

### 7. QGroundControl on Windows

- **Application Settings → Comm Links → Add**, type UDP, listening port `14550`.
- Enable forwarding to `127.0.0.1:14445` so the relay agent receives what QGC
  receives.

Every SITL instance feeds the one shared link on 14550, so a single QGC
connection shows the whole fleet.

## Daily workflow

From **Windows**, driving WSL for the simulator only:

```powershell
wsl -d Ubuntu-24.04 -- bash -lc "cd /mnt/d/Projects/courier && ./sim/run_sitl.sh -n 10"
```

and to stop:

```powershell
wsl -d Ubuntu-24.04 -- bash -lc "cd /mnt/d/Projects/courier && ./sim/stop_sitl.sh"
```

`make sim N=10` and `make sim-stop` are the same commands and must be run from
*inside* WSL, because `sim_vehicle.py` lives there.

Everything else is an ordinary Windows shell:

```powershell
make up                     # stack
python -m gateway --tokens local/e2e/gateway.tokens --host 127.0.0.1 --port 8081
python -m agent --config local/e2e/relay.toml
make console                # the map on :8000
make test
```

## SYSIDs

Simulated vehicles use **201-210**, set by `SITL_SYSID_BASE` and deliberately
clear of the real aircraft at SYSID 1.

A collision here does not fail loudly. The Gateway would classify both aircraft
as one vehicle, bind them to a single `drone_id`, and interleave two tracks
into one — smoothly, with nothing in the data to say it had happened.

Two ArduPilot details the launcher handles, both of which cost a run before
they were understood:

- ArduPilot 4.6 renamed `SYSID_THISMAV` to `MAV_SYSID`. The launcher writes
  **both**, so one script works either side of that rename.
- SITL persists parameters in its emulated EEPROM, so a defaults file is
  ignored on the second run. The launcher passes `--wipe-eeprom`.

Without those, all ten vehicles report SYSID 1 and QGC shows one drone.

## Troubleshooting

**SITL traffic does not reach QGC.** Confirm mirrored networking is active with
the `curl` check in step 2, and that `wsl --shutdown` was run after editing
`.wslconfig`.

**All vehicles show as one drone.** The SYSID did not take. Check that
`sim/out/instance-N/sysid.parm` contains both `SYSID_THISMAV` and `MAV_SYSID`,
and that the launcher passed `--wipe-eeprom`.

**`stop_sitl.sh` says processes survived.** It verifies with `pgrep` rather
than trusting the signal, so this is usually real. Note that each instance is
two matches — an `xterm` wrapper and the `arducopter` binary — so ten vehicles
count as twenty.

**The Gateway logs connection timeouts while the stack is healthy.** `.env`
says `localhost` somewhere it should say `127.0.0.1`. See step 3.

**`make sim` refuses.** `SIM_VEHICLE` in `sim/sitl.env` does not point at a
real `sim_vehicle.py`, or the file is missing entirely. It is gitignored, so a
fresh clone has no copy.
