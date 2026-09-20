#!/usr/bin/env bash
# Launch N ArduCopter SITL instances, each with a unique SYSID and its own UDP
# output port.
#
# The same MAVLink stream shape a real vehicle produces comes out of these
# ports, so every consumer in this repository — Gateway, integration tests,
# scenario runs — talks to SITL and hardware through one code path.
#
# Configuration lives in sim/sitl.env (copy sim/sitl.env.example). The home
# location has no built-in default on purpose: coordinates belong in
# configuration, and a stale default that silently puts the fleet somewhere
# else is precisely the failure this rule exists to prevent.
#
# Usage:  ./sim/run_sitl.sh -n 10
#         make sim N=10

set -euo pipefail

SIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${SIM_DIR}/out"
PID_FILE="${OUT_DIR}/sitl.pids"

# Metres per degree of longitude at the equator: 2*pi*a/360 for WGS84's
# semi-major axis a = 6378137 m. Scaled by cos(latitude) below.
#
# This is a spherical approximation: it omits the prime-vertical term and so
# runs about 0.16% short at mid latitudes (measured: 25.041 m for a requested
# 25 m at 41.7 N). That is fine for its only purpose, which is parking
# simulated vehicles far enough apart that they do not start stacked. Do not
# reuse it for anything that measures a real distance — that is what PostGIS
# geography is for.
METRES_PER_DEG_LON_EQUATOR=111319.49

die() { echo "run_sitl: $*" >&2; exit 1; }
note() { echo "run_sitl: $*" >&2; }

usage() {
  cat >&2 <<'USAGE'
Usage: run_sitl.sh [-n N]

  -n N   Number of SITL instances to launch (default 1).
  -h     This help.

Configuration is read from sim/sitl.env. SITL_HOME is required.
USAGE
}

instance_count=1
while getopts ":n:h" opt; do
  case "${opt}" in
    n) instance_count="${OPTARG}" ;;
    h) usage; exit 0 ;;
    :) die "option -${OPTARG} requires an argument" ;;
    \?) usage; die "unknown option -${OPTARG}" ;;
  esac
done

[[ "${instance_count}" =~ ^[0-9]+$ ]] || die "instance count must be an integer, got '${instance_count}'"
(( instance_count >= 1 )) || die "instance count must be at least 1"

# --- Configuration ----------------------------------------------------------

CONFIG_FILE="${SIM_DIR}/sitl.env"
if [[ -f "${CONFIG_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${CONFIG_FILE}"
  set +a
else
  note "no ${CONFIG_FILE}; falling back to the environment"
  note "create it with: cp sim/sitl.env.example sim/sitl.env"
fi

[[ -n "${SITL_HOME:-}" ]] || die "SITL_HOME is not set. Copy sim/sitl.env.example to sim/sitl.env and set your test area. Coordinates are never defaulted in code."

SITL_SPACING_M="${SITL_SPACING_M:-25}"
SITL_SYSID_BASE="${SITL_SYSID_BASE:-1}"
SITL_OUT_PORT_BASE="${SITL_OUT_PORT_BASE:-14560}"
SITL_QGC_PORT="${SITL_QGC_PORT:-14550}"
SITL_FRAME="${SITL_FRAME:-quad}"
SITL_SPEEDUP="${SITL_SPEEDUP:-1}"
SITL_TCP_PORT_BASE="${SITL_TCP_PORT_BASE:-5760}"
SITL_TCP_PORT_STRIDE="${SITL_TCP_PORT_STRIDE:-10}"
SITL_STREAMRATE="${SITL_STREAMRATE:-4}"

IFS=',' read -r home_lat home_lon home_alt_amsl_m home_heading_deg <<< "${SITL_HOME}"
for field in home_lat home_lon home_alt_amsl_m home_heading_deg; do
  [[ -n "${!field:-}" ]] || die "SITL_HOME must be 'lat,lon,alt_amsl_m,heading_deg', got '${SITL_HOME}'"
done
[[ "${home_lat}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || die "SITL_HOME latitude is not a number: '${home_lat}'"
[[ "${home_lon}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || die "SITL_HOME longitude is not a number: '${home_lon}'"
awk -v lat="${home_lat}" 'BEGIN{ exit (lat >= -90 && lat <= 90) ? 0 : 1 }' \
  || die "SITL_HOME latitude out of range: ${home_lat}"
awk -v lon="${home_lon}" 'BEGIN{ exit (lon >= -180 && lon <= 180) ? 0 : 1 }' \
  || die "SITL_HOME longitude out of range: ${home_lon}"

# The highest SYSID must stay below the ground-station reserved value.
last_sysid=$(( SITL_SYSID_BASE + instance_count - 1 ))
(( SITL_SYSID_BASE >= 1 )) || die "SITL_SYSID_BASE must be at least 1; 0 is reserved"
(( last_sysid <= 254 )) || die "SYSID range ${SITL_SYSID_BASE}..${last_sysid} exceeds 254"

# --- Locate sim_vehicle.py --------------------------------------------------

SIM_VEHICLE="${SIM_VEHICLE:-$(command -v sim_vehicle.py || true)}"
[[ -n "${SIM_VEHICLE}" ]] || die "sim_vehicle.py not found. Put ArduPilot's Tools/autotest on PATH or set SIM_VEHICLE in sim/sitl.env."
[[ -f "${SIM_VEHICLE}" ]] || die "SIM_VEHICLE is not a file: ${SIM_VEHICLE}"

# SITL is started headless and the UDP fan-out is done by an explicit MAVProxy
# per instance. sim_vehicle.py's own --out only takes effect when it starts
# MAVProxy itself, which also gives one interactive console per vehicle —
# unusable at ten. Driving MAVProxy directly keeps the ports explicit and the
# processes non-interactive.
MAVPROXY="${MAVPROXY:-$(command -v mavproxy.py || true)}"
[[ -n "${MAVPROXY}" ]] || die "mavproxy.py not found. Install MAVProxy or set MAVPROXY in sim/sitl.env."

# --- Launch -----------------------------------------------------------------

mkdir -p "${OUT_DIR}"
if [[ -s "${PID_FILE}" ]] && kill -0 "$(head -n1 "${PID_FILE}")" 2>/dev/null; then
  die "SITL already appears to be running. Run ./sim/stop_sitl.sh first."
fi
: > "${PID_FILE}"

# Wait for SITL's MAVLink TCP socket before attaching MAVProxy; attaching too
# early leaves a MAVProxy that never connects and a vehicle QGC never sees.
wait_for_port() {
  local port="$1" deadline=$(( SECONDS + 60 ))
  while (( SECONDS < deadline )); do
    if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
      exec 3<&- 3>&-
      return 0
    fi
    sleep 1
  done
  return 1
}

printf '%-4s %-6s %-8s %-8s %-9s %s\n' "INST" "SYSID" "TCP" "OUT" "PID" "HOME"

for (( i = 0; i < instance_count; i++ )); do
  sysid=$(( SITL_SYSID_BASE + i ))
  out_port=$(( SITL_OUT_PORT_BASE + i ))
  tcp_port=$(( SITL_TCP_PORT_BASE + i * SITL_TCP_PORT_STRIDE ))
  inst_dir="${OUT_DIR}/instance-${i}"
  mkdir -p "${inst_dir}"

  # Space instances due east of home so they do not overlap on the ground.
  inst_lon=$(awk -v lon="${home_lon}" -v lat="${home_lat}" \
                 -v spacing="${SITL_SPACING_M}" -v i="${i}" \
                 -v mpd="${METRES_PER_DEG_LON_EQUATOR}" \
    'BEGIN { printf "%.7f", lon + (i * spacing) / (mpd * cos(lat * 3.141592653589793 / 180)) }')
  inst_home="${home_lat},${inst_lon},${home_alt_amsl_m},${home_heading_deg}"

  # SYSID is set through a parameter file rather than relying on SITL's
  # instance-derived default, so the value is explicit and version-independent.
  param_file="${inst_dir}/sysid.parm"
  printf 'SYSID_THISMAV %d\n' "${sysid}" > "${param_file}"

  "${SIM_VEHICLE}" \
    --vehicle ArduCopter \
    --frame "${SITL_FRAME}" \
    --instance "${i}" \
    --custom-location "${inst_home}" \
    --speedup "${SITL_SPEEDUP}" \
    --add-param-file "${param_file}" \
    --no-rebuild \
    --no-mavproxy \
    > "${inst_dir}/sitl.log" 2>&1 &
  sitl_pid=$!
  echo "${sitl_pid}" >> "${PID_FILE}"

  if ! wait_for_port "${tcp_port}"; then
    die "instance ${i}: SITL did not open TCP ${tcp_port} within 60 s; see ${inst_dir}/sitl.log"
  fi

  out_args=(--out "udp:127.0.0.1:${out_port}")
  if [[ -n "${SITL_QGC_PORT}" ]]; then
    # Every instance also feeds one shared link, so a single QGC connection
    # shows the whole fleet; QGC separates the vehicles by SYSID.
    out_args+=(--out "udp:127.0.0.1:${SITL_QGC_PORT}")
  fi

  "${MAVPROXY}" \
    --master "tcp:127.0.0.1:${tcp_port}" \
    "${out_args[@]}" \
    --streamrate "${SITL_STREAMRATE}" \
    --state-basedir "${inst_dir}" \
    --aircraft "sitl-${i}" \
    --daemon \
    > "${inst_dir}/mavproxy.log" 2>&1 &
  mavproxy_pid=$!
  echo "${mavproxy_pid}" >> "${PID_FILE}"

  printf '%-4s %-6s %-8s %-8s %-9s %s\n' \
    "${i}" "${sysid}" "${tcp_port}" "${out_port}" "${sitl_pid}" "${inst_home}"
done

cat >&2 <<EOF

run_sitl: ${instance_count} instance(s) up; logs in ${OUT_DIR}/instance-*/
run_sitl: per-vehicle UDP ${SITL_OUT_PORT_BASE}..$(( SITL_OUT_PORT_BASE + instance_count - 1 ))${SITL_QGC_PORT:+, shared QGC link on ${SITL_QGC_PORT}}
run_sitl: SYSID ${SITL_SYSID_BASE}..${last_sysid}
run_sitl: stop with ./sim/stop_sitl.sh
EOF
