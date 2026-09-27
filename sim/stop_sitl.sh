#!/usr/bin/env bash
# Stop every SITL and MAVProxy process started by run_sitl.sh.
#
# Processes are terminated in reverse launch order (MAVProxy before its SITL)
# and given a chance to exit cleanly before being killed.

set -euo pipefail

SIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SIM_DIR}/out/sitl.pids"

if [[ ! -s "${PID_FILE}" ]]; then
  echo "stop_sitl: nothing to stop (${PID_FILE} absent or empty)" >&2
  exit 0
fi

mapfile -t pids < "${PID_FILE}"

# Each recorded pid is a process GROUP leader, because run_sitl.sh starts every
# instance under `setsid`. Signalling the group is what makes this work:
# sim_vehicle.py is a launcher that starts arducopter inside an xterm and then
# returns, and mavproxy --daemon forks, so in both cases the recorded pid is
# gone long before the thing it started.
#
# Signalling the pids alone left 5 arducopter processes running across two
# runs of 2 instances, while this script reported "signalled 0 process(es)" -
# a clean bill of health over a fleet that was still flying. A leftover
# simulator holds its TCP port, so the next run fails somewhere unrelated.
signal_group() {
  local pid="$1" sig="$2"
  # The leading dash makes this a process-group id. `kill -0 -- -pid` is how
  # the group's existence is probed.
  kill "-${sig}" -- "-${pid}" 2>/dev/null || return 1
  return 0
}

group_alive() {
  kill -0 -- "-$1" 2>/dev/null
}

stopped=0
for (( idx = ${#pids[@]} - 1; idx >= 0; idx-- )); do
  pid="${pids[idx]}"
  [[ "${pid}" =~ ^[0-9]+$ ]] || continue
  if group_alive "${pid}"; then
    signal_group "${pid}" TERM || true
    stopped=$(( stopped + 1 ))
  fi
done

# Give them a moment, then insist.
for _ in $(seq 1 10); do
  remaining=0
  for pid in "${pids[@]}"; do
    [[ "${pid}" =~ ^[0-9]+$ ]] || continue
    if group_alive "${pid}"; then
      remaining=$(( remaining + 1 ))
    fi
  done
  (( remaining == 0 )) && break
  sleep 1
done

killed=0
for pid in "${pids[@]}"; do
  [[ "${pid}" =~ ^[0-9]+$ ]] || continue
  # An explicit if, not `A && B || C`: in that form C also runs when A
  # succeeds and B fails, which is not what it reads as.
  if group_alive "${pid}"; then
    signal_group "${pid}" KILL || true
    killed=$(( killed + 1 ))
  fi
done

# SIGKILL is not synchronous: the process is gone when the kernel says so, not
# when kill returns. Checking immediately reported "2 SITL process(es) still
# running" for processes that had already been killed and were simply not yet
# reaped - a false alarm on a teardown that had in fact worked. A warning that
# cries wolf is worse than no warning, because it is the one people learn to
# scroll past.
if (( killed > 0 )); then
  for _ in $(seq 1 20); do
    pgrep -f 'bin/arducopter|mavproxy\.py' >/dev/null 2>&1 || break
    sleep 0.25
  done
fi

: > "${PID_FILE}"

# Verify rather than assume. A stop that reports success while a simulator is
# still running is worse than one that fails loudly, because the next run
# blames the wrong thing.
# `|| true` is load-bearing. `pgrep` exits 1 when nothing matches, which here
# is the success case, and `set -euo pipefail` turned that into a silent exit
# 1 before anything was printed. So a teardown that had worked perfectly
# reported failure with no message, and CI read it as a failed stop.
#
# The failure path was exercised often and worked; the success path had never
# run to completion. Same shape as the relay `gap` and the PARAM_VALUE offset:
# the branch that says "nothing is wrong" was the one nobody had watched.
leftover=$(pgrep -f 'bin/arducopter|mavproxy\.py' | wc -l) || true
if (( leftover > 0 )); then
  echo "stop_sitl: WARNING - ${leftover} SITL process(es) still running:" >&2
  pgrep -af 'bin/arducopter|mavproxy\.py' >&2 || true
  echo "stop_sitl: signalled ${stopped} group(s); some survived" >&2
  exit 1
fi

echo "stop_sitl: signalled ${stopped} group(s); nothing left running" >&2
