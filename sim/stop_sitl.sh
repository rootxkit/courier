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

stopped=0
for (( idx = ${#pids[@]} - 1; idx >= 0; idx-- )); do
  pid="${pids[idx]}"
  [[ "${pid}" =~ ^[0-9]+$ ]] || continue
  if kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    stopped=$(( stopped + 1 ))
  fi
done

# Give them a moment, then insist.
for _ in $(seq 1 10); do
  remaining=0
  for pid in "${pids[@]}"; do
    [[ "${pid}" =~ ^[0-9]+$ ]] || continue
    kill -0 "${pid}" 2>/dev/null && remaining=$(( remaining + 1 ))
  done
  (( remaining == 0 )) && break
  sleep 1
done

for pid in "${pids[@]}"; do
  [[ "${pid}" =~ ^[0-9]+$ ]] || continue
  # An explicit if, not `A && B || C`: in that form C also runs when A
  # succeeds and B fails, which is not what it reads as.
  if kill -0 "${pid}" 2>/dev/null; then
    kill -9 "${pid}" 2>/dev/null || true
  fi
done

: > "${PID_FILE}"
echo "stop_sitl: signalled ${stopped} process(es)" >&2
