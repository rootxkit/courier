# Development environment: WSL2

The toolchain this project depends on — ArduPilot's `waf` build, `sim_vehicle.py`,
`mavlink-router`, MAVProxy — is POSIX-native. Each one is a separate fight on
Windows, and from P1 onward they are used daily. Running the whole development
environment inside WSL2 removes that friction permanently.

QGroundControl stays on Windows. It connects over UDP and does not care where
the other end lives.

## Why not split (SITL in WSL, everything else on Windows)

It works, but the cost is paid every day: SITL sends UDP across the WSL boundary
to a Gateway on the Windows side, tests run in two places, paths do not match,
and `make` has to be installed separately on Windows. Each issue is small. The
sum is not.

`make` on Windows carries one more wrinkle worth naming. The `Makefile` declares
`SHELL := /bin/bash` because its recipes are POSIX shell, so the targets need
Git Bash or WSL — a native `make.exe` driven from PowerShell or `cmd` has no
`/bin/bash` to find. The alternative is a Windows shell fallback inside the
`Makefile`, which would be dead code the moment this migration lands. One more
small thing that disappears entirely rather than being worked around.

The exception is if you are on Windows 10, where mirrored networking is
unavailable — there the split is genuinely painful, which is an argument for
moving everything into WSL rather than against it.

## Setup

### 1. Install WSL2 with Ubuntu 24.04

```powershell
wsl --install -d Ubuntu-24.04
wsl --update
```

### 2. Enable mirrored networking (Windows 11 only)

This is the step that makes the Windows/WSL boundary disappear for networking.
With it, `127.0.0.1` means the same thing on both sides, and no port forwarding
or IP lookup is needed.

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

Reopen the WSL terminal. Verify:

```bash
# From WSL, a service listening on Windows localhost is now reachable directly
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5432 || true
```

On Windows 10, omit this section. You will instead need the WSL IP
(`ip addr show eth0`) wherever this document says `127.0.0.1`, and SITL must
bind `0.0.0.0` rather than loopback.

### 3. Docker

Install Docker Desktop on Windows and enable WSL integration:
**Settings → Resources → WSL Integration → Ubuntu-24.04**.

The Docker daemon runs once, on the Windows side, and `docker` works from inside
WSL against the same daemon. Do not install a second Docker inside WSL.

### 4. Clone into the WSL filesystem, not `/mnt/c`

This matters more than it looks. Files under `/mnt/c` are accessed through a
translation layer, and builds there run several times slower with unreliable
file-watching.

```bash
mkdir -p ~/projects && cd ~/projects
git clone <repo-url> courier
cd courier
```

If the repo currently lives at `D:\Projects\courier`, move it rather than
symlinking. Reach it from Windows Explorer at `\\wsl$\Ubuntu-24.04\home\<you>\projects\courier`.

### 5. Toolchain

```bash
sudo apt update
sudo apt install -y build-essential git python3.12 python3.12-venv \
                    python3-pip make pkg-config

cd ~/projects/courier
make hooks
make venv
make up
make test
```

`make` is already present. The `ezwinports` install is no longer needed.

### 6. ArduPilot SITL

```bash
cd ~
git clone --recurse-submodules https://github.com/ArduPilot/ardupilot.git
cd ardupilot
Tools/environment_install/install-prereqs-ubuntu.sh -y
. ~/.profile
./waf configure --board sitl
./waf copter
```

Add to `~/.bashrc`:

```bash
export PATH="$HOME/ardupilot/Tools/autotest:$PATH"
export PATH="/usr/lib/ccache:$PATH"
```

The first build takes 10-20 minutes. Subsequent builds are incremental.

Verify:

```bash
cd ~/projects/courier
make sim N=1
```

### 7. Connect QGroundControl on Windows

SITL's default output reaches QGC automatically under mirrored networking. If
QGC does not auto-connect, add a UDP link manually:

- **Application Settings → Comm Links → Add**
- Type UDP, listening port `14550`

For multiple vehicles, each SITL instance gets its own port
(`14550`, `14560`, …) per the launcher. QGC handles several vehicles at once as
long as each has a distinct `SYSID_THISMAV`.

## Daily workflow

```bash
# WSL terminal
cd ~/projects/courier
make up          # stack
make sim N=3     # vehicles
make test        # tests
```

QGC and your editor stay on Windows. VS Code with the WSL extension opens the
project natively inside WSL — use **Remote-WSL: Open Folder**, not a `\\wsl$`
path.

## Troubleshooting

**SITL traffic does not reach QGC.** Check mirrored networking is active
(`cat /mnt/c/Users/<you>/.wslconfig` from WSL, then confirm `wsl --shutdown` was
run). On Windows 10, bind SITL to `0.0.0.0` and point QGC at the WSL IP.

**Docker commands fail in WSL.** WSL integration is not enabled for this
distribution in Docker Desktop settings.

**Builds are slow, file watching misses changes.** The repo is under `/mnt/c`.
Move it into the WSL filesystem.

**`make sim` refuses.** `sim_vehicle.py` is not on `PATH`. Check step 6 and open
a fresh shell.
