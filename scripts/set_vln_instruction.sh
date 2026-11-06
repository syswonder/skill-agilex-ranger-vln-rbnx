#!/usr/bin/env bash
# Send one navigation instruction to the running stack, or stop it.
#
# Run INSIDE the container:
#
#     source /opt/ros/humble/setup.bash
#     export RMW_IMPLEMENTATION=rmw_zenoh_cpp
#     scripts/set_vln_instruction.sh "go to the chair and stop"
#     scripts/set_vln_instruction.sh stop
#
# This is the ROS door onto the same handoff the robonix/primitive/vln/task
# capability uses.  It only QUEUES: the blocking POST /eval_dual happens on the
# inference thread, so this returns immediately even when the model server is
# slow or unreachable.  Watch /ranger/vln/status (or scripts/watch_vln_chain.py)
# to see it actually take effect.
#
# "stop" is reserved: it retires the running plan instead of becoming a goal.
set -euo pipefail

TOPIC="${VLN_INSTRUCTION_TOPIC:-/ranger/vln/instruction}"
STATUS_TOPIC="${VLN_STATUS_TOPIC:-/ranger/vln/status}"

usage() {
  echo "usage: $0 <instruction>|stop" >&2
  echo "  e.g. $0 \"go to the chair and stop\"" >&2
  echo "       $0 stop" >&2
}

[[ $# -eq 1 ]] || { usage; exit 2; }
[[ -n "${1// }" ]] || { usage; exit 2; }

# The payload is embedded in a YAML message literal, so a stray quote or brace
# would be parsed as message syntax instead of text.  Reject rather than mangle.
case "$1" in
  *[\"\'\{\}\\]*)
    echo "instruction must not contain quotes, braces or backslashes" >&2
    exit 2
    ;;
esac

if ! ros2 topic list 2>/dev/null | grep -qx "$TOPIC"; then
  echo "$TOPIC does not exist: the VLN node is not running" >&2
  exit 1
fi

ros2 topic pub --once "$TOPIC" std_msgs/msg/String "{data: '$1'}"
echo
echo "queued. current status:"
ros2 topic echo --once "$STATUS_TOPIC" std_msgs/msg/String 2>/dev/null || \
  echo "  (no status published yet)"
