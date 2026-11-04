#!/usr/bin/env bash
set -euo pipefail

# Build the standalone Ranger InternVLA-N1 deployment inside the robonix
# container.  Mirrors robot-agilex-ranger_mini_v3/build.sh: rbnx is already on
# PATH in the container, so this is a thin wrapper over `rbnx build -f`.

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export ROBONIX_DEPLOY_DIR="$DEPLOY_DIR"

set +u
# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash
set -u

MANIFEST="${ROBONIX_MANIFEST:-$DEPLOY_DIR/robonix_manifest.yaml}"
exec rbnx build -f "$MANIFEST" "$@"
