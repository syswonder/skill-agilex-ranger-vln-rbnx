#!/usr/bin/env bash
set -uo pipefail

# Stop the Ranger InternVLA-N1 stack: `rbnx shutdown -f <manifest>`.
#
# Unlike the reference follow profile there is no local sidecar to leave behind:
# the DualVLN model runs on a separate compute host, so stopping this stack does
# not touch it, and the chassis guard is a node inside the provider's own
# executor rather than a second process -- so it cannot outlive the provider and
# keep publishing zeros on /cmd_vel.

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${ROBONIX_MANIFEST:-$DEPLOY_DIR/robonix_manifest.yaml}"
export ROBONIX_DEPLOY_DIR="$DEPLOY_DIR"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if command -v rbnx >/dev/null 2>&1 && [[ -f "$DEPLOY_DIR/rbnx-boot/state.json" ]]; then
  rbnx shutdown -f "$MANIFEST" "$@"
else
  # No state file, but that does not mean nothing is running: a boot that died
  # at its first component (e.g. "atlas: 50051 already in use") leaves the
  # previous stack alive and its state.json already gone, and from then on
  # `rbnx shutdown` has nothing to work from.  This used to end in "nothing to
  # stop" next to a full stack holding can_ranger -- so sweep the boot parents
  # (they cascade to their own children; TERM first, KILL only if that is
  # ignored) and then say what is left.
  mapfile -t boot_parents < <(pgrep -f 'rbnx boot .*-f .*robonix_manifest' || true)
  if [[ ${#boot_parents[@]} -eq 0 ]]; then
    echo "no Robonix boot state; nothing to stop"
  else
    echo "no Robonix boot state, but a stack is still running (pid ${boot_parents[*]}); stopping it"
    kill -TERM "${boot_parents[@]}" 2>/dev/null || true
    for _ in $(seq 1 20); do
      sleep 0.5
      pgrep -f 'rbnx boot .*-f .*robonix_manifest' >/dev/null || break
    done
    if pgrep -f 'rbnx boot .*-f .*robonix_manifest' >/dev/null; then
      echo "rbnx boot ignored SIGTERM; escalating to SIGKILL" >&2
      pkill -KILL -f 'rbnx boot .*-f .*robonix_manifest' || true
      sleep 1
    fi
  fi
fi

# `rbnx shutdown` has left ROS nodes behind before now.  Two things matter after
# it returns: nothing may still hold /cmd_vel, and can_ranger must be free for
# the navigation stack.  Report both rather than assuming.
if pgrep -af 'internvln_navigation\.(provider|ros_node|chassis_guard_node)' >/dev/null; then
  echo "WARNING: an internvln process survived rbnx shutdown:" >&2
  pgrep -af 'internvln_navigation\.(provider|ros_node|chassis_guard_node)' >&2
  echo "kill it before starting the navigation stack" >&2
fi
