#!/usr/bin/env bash
set -euo pipefail

# Start the Ranger InternVLA-N1 stack in the robonix container.
#
# The stack is small on purpose: robot_description + front camera + Ranger
# chassis + the VLN primitive, on top of the Robonix core services.  The
# DualVLN model itself is NOT here -- it is an HTTP service on a compute host,
# and this script refuses to arm the stack without it.
#
# SAFETY:  this stack owns the can_ranger bus and /cmd_vel.  It MUST NOT run at
# the same time as the navigation stack.  Confirm the navigation stack is
# stopped (`rbnx shutdown -f .../robonix_manifest.no-arm.yaml` in that repo)
# before starting this one.

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$DEPLOY_DIR/.run"
LOG_DIR="$DEPLOY_DIR/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"

# Machine-local overrides live outside the manifest and Git.
if [[ -f "$DEPLOY_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$DEPLOY_DIR/.env"
  set +a
fi

export PATH="$HOME/.cargo/bin:$PATH"
export ROBONIX_DEPLOY_DIR="$DEPLOY_DIR"
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_zenoh_cpp}"

set +u
# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash
set -u

router_pid=""
MANIFEST="${ROBONIX_MANIFEST:-$DEPLOY_DIR/robonix_manifest.yaml}"
stack_started=0

can_ready() {
  local iface="$1"
  local bitrate="$2"
  local detail
  detail="$(ip -details link show "$iface" 2>/dev/null)" || return 1
  [[ "${detail%%$'\n'*}" == *"state UP"* ]] && [[ "$detail" == *"bitrate $bitrate"* ]]
}

prepare_ranger_can() {
  local iface="${RANGER_CAN_INTERFACE:-can_ranger}"
  local bitrate="${RANGER_CAN_BITRATE:-500000}"
  # grep, not ripgrep: every other check in this repo uses grep, and a missing
  # rg would make this return 0 -- silently skipping CAN setup.
  grep -Eq '^[[:space:]]*- name:[[:space:]]+ranger_chassis([[:space:]]|$)' "$MANIFEST" || return 0
  can_ready "$iface" "$bitrate" && return 0

  echo "configuring Ranger CAN $iface at $bitrate bps" >&2
  local -a elevate=()
  if [[ "$EUID" -ne 0 ]]; then
    elevate=(sudo)
  fi
  "${elevate[@]}" ip link set "$iface" down
  "${elevate[@]}" ip link set "$iface" type can bitrate "$bitrate"
  "${elevate[@]}" ip link set "$iface" up
  can_ready "$iface" "$bitrate" || {
    echo "Ranger CAN $iface is not UP at $bitrate bps" >&2
    exit 1
  }
}

# The manifest carries exactly one `mode:` key -- the VLN primitive's -- so a
# plain grep is the whole rule.  Anything other than `live` means dry-run, which
# is also what an unparseable manifest resolves to: fail closed.
stack_mode() {
  # grep, not ripgrep: if rg were absent this would fall through to dry-run and
  # the live pre-flight below would silently stop refusing.
  if grep -Eq '^[[:space:]]*mode:[[:space:]]+live([[:space:]]|$)' "$MANIFEST"; then
    printf 'live'
  else
    printf 'dry-run'
  fi
}

# A missing or endpoint-suffixed base url is a configuration error, not a
# reachability result, so it is checked at top level.  It must NOT live inside
# the probe below: that one runs in a command substitution, where `exit 1` would
# only leave the subshell and the stack would boot anyway.
require_internvla_server_url() {
  local url="${INTERNVLA_SERVER_URL:-}"
  if [[ -z "$url" ]]; then
    echo "INTERNVLA_SERVER_URL is not set; copy .env.example to .env and point" >&2
    echo "it at the DualVLN compute host, e.g. http://127.0.0.1:5801" >&2
    exit 1
  fi
  case "${url%/}" in
    */eval_dual)
      echo "INTERNVLA_SERVER_URL must be the server BASE url, not an endpoint:" >&2
      echo "  got $url" >&2
      echo "  the client appends /eval_dual itself" >&2
      exit 1
      ;;
    */health | */task/start | */task/report)
      echo "INTERNVLA_SERVER_URL names a route this server does not have:" >&2
      echo "  got $url" >&2
      echo "  the server publishes exactly one route, POST /eval_dual" >&2
      exit 1
      ;;
  esac
}

# The remote model is the one dependency this deployment cannot provide for
# itself, and its absence is invisible until the robot is already armed: the
# node would come up healthy, subscribe to the camera and then fail every
# evaluation.  Probe it here instead.
#
# The server publishes exactly one route -- POST /eval_dual -- and no liveness
# route at all, so there is nothing to GET.  Asking for /eval_dual with the
# wrong method is the strongest cheap check available: Werkzeug answers 405
# "Allow: POST, OPTIONS", which proves that route is registered, on the host we
# think, without running an inference.  A bare TCP connect would prove none of
# that, and a 200 would only mean some other service is on the port.
#
# This speaks http.client, not urllib, on purpose.  The robot's own client
# (internvln_navigation.http_client.HttpTransport) opens a direct socket and
# ignores http_proxy, so a probe that honoured a proxy variable could refuse to
# arm a perfectly reachable deployment -- or wave through one the client cannot
# reach.  Same wire path, or the check means nothing.
check_internvla_server() {
  python3 - "${INTERNVLA_SERVER_URL%/}" <<'PY'
import sys
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit

parsed = urlsplit(sys.argv[1] + "/eval_dual")
if parsed.scheme not in ("http", "https"):
    print(f"unsupported scheme {parsed.scheme!r}: use http:// or https://")
    raise SystemExit(1)
connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
try:
    connection = connection_type(parsed.hostname, parsed.port, timeout=5.0)
    try:
        connection.request(
            "GET", parsed.path or "/", headers={"Accept": "application/json"}
        )
        response = connection.getresponse()
        status = int(response.status)
        allow = response.getheader("Allow") or ""
        body = response.read(512).decode("utf-8", "replace")
    finally:
        connection.close()
except Exception as error:  # noqa: BLE001 - reported, never raised further
    # Printed, not raised: the caller captures stdout to build the message.
    print(f"unreachable: {type(error).__name__}: {error}")
    raise SystemExit(1)
if 200 <= status < 300:
    print(f"GET /eval_dual answered {status}; POST-only server expected")
elif status == 405 and "POST" in allow.upper():
    print(f"POST /eval_dual is registered (GET -> 405, Allow: {allow.strip()})")
else:
    print(f"GET /eval_dual returned HTTP {status}: {body.strip()[:200]}")
    raise SystemExit(1)
PY
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "$stack_started" == "1" && -f "$DEPLOY_DIR/rbnx-boot/state.json" ]]; then
    rbnx shutdown -f "$MANIFEST" || true
  fi
  if [[ -n "$router_pid" ]]; then
    kill -TERM "$router_pid" 2>/dev/null || true
    wait "$router_pid" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

MODE="$(stack_mode)"
require_internvla_server_url

if server_detail="$(check_internvla_server)"; then
  echo "DualVLN server ${INTERNVLA_SERVER_URL}: $server_detail"
elif [[ "$MODE" == "live" ]]; then
  echo "DualVLN server ${INTERNVLA_SERVER_URL} is NOT reachable (${server_detail})." >&2
  echo "Refusing to arm (mode: live) with no model to ask: every evaluation" >&2
  echo "would fail and the robot would simply sit still, which looks like a" >&2
  echo "policy problem rather than a network one." >&2
  exit 1
else
  echo "DualVLN server ${INTERNVLA_SERVER_URL} is NOT reachable (${server_detail});" >&2
  echo "dry-run continues, but no frame will produce a usable action." >&2
fi

if [[ "$MODE" == "live" ]]; then
  echo "WARNING: mode is live -- the chassis guard will be started and this" >&2
  echo "stack WILL drive the wheels. Keep a physical remote in hand." >&2
fi

prepare_ranger_can

if [[ "$RMW_IMPLEMENTATION" == "rmw_zenoh_cpp" ]]; then
  router_bin="/opt/ros/humble/lib/rmw_zenoh_cpp/rmw_zenohd"
  [[ -x "$router_bin" ]] || {
    echo "missing $router_bin; install ros-humble-rmw-zenoh-cpp" >&2
    exit 1
  }
  if ! python3 - <<'PY'
import socket
try:
    with socket.create_connection(("127.0.0.1", 7447), timeout=0.25):
        pass
except OSError:
    raise SystemExit(1)
PY
  then
    "$router_bin" >"$LOG_DIR/rmw_zenohd.log" 2>&1 &
    router_pid=$!
    echo "$router_pid" >"$RUN_DIR/rmw_zenohd.pid"
    for _ in $(seq 1 40); do
      if python3 - <<'PY'
import socket
try:
    with socket.create_connection(("127.0.0.1", 7447), timeout=0.25):
        pass
except OSError:
    raise SystemExit(1)
PY
      then
        break
      fi
      if ! kill -0 "$router_pid" 2>/dev/null; then
        tail -80 "$LOG_DIR/rmw_zenohd.log" >&2 || true
        exit 1
      fi
      sleep 0.25
    done
  fi
fi

stack_started=1
rbnx boot --no-update-check -f "$MANIFEST" "$@"
