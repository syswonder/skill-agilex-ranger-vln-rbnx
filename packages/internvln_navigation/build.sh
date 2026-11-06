#!/usr/bin/env bash
set -euo pipefail

ROOT="${RBNX_PACKAGE_ROOT:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
DEPLOY_ROOT="${ROBONIX_DEPLOY_DIR:-$(cd "${ROOT}/../.." && pwd)}"
ROS_DISTRO="${ROS_DISTRO:-humble}"
ROS_SETUP="/opt/ros/${ROS_DISTRO}/setup.bash"
ROBONIX_IDL_ROOT="${ROOT}/rbnx-build/codegen/ros2_idl"
ROBONIX_IDL_SETUP="${ROBONIX_IDL_ROOT}/install/setup.bash"
BUILD_ROOT="${ROOT}/.build/ros"

if ! command -v rbnx >/dev/null 2>&1; then
  echo "rbnx is not available." >&2
  exit 1
fi
if [[ ! -r "${ROS_SETUP}" ]]; then
  echo "Missing ${ROS_SETUP}; ROS 2 Humble is required." >&2
  exit 1
fi
if [[ ! -r "${DEPLOY_ROOT}/scripts/build_robonix_ros2_overlay.sh" ]]; then
  echo "Missing shared Robonix ROS 2 overlay builder." >&2
  exit 1
fi

echo "[internvln_navigation/build] generating Robonix lifecycle and task bindings"
# --mcp turns capabilities/lib/internvln_navigation_control/srv/VlnTask.srv into
# the VlnTask_Request / VlnTask_Response dataclasses provider.py imports.
rbnx codegen -p "${ROOT}" --mcp --ros2
# shellcheck disable=SC1090
source "${DEPLOY_ROOT}/scripts/build_robonix_ros2_overlay.sh"
set +u
# shellcheck disable=SC1090
source "${ROS_SETUP}"
set -u

# Runtime deps come first: `rbnx build` installs and checks nothing, so a
# missing cv2 / cv_bridge would otherwise stay invisible until the node imports
# it at start time.  Checks against the live container and installs only what is
# absent; RBNX_SKIP_DEPS_CHECK=1 turns it off.
bash "${ROOT}/scripts/ensure_deps.sh"

robonix_build_ros2_overlay "${ROBONIX_IDL_ROOT}" --packages-select lifecycle
if [[ ! -r "${ROBONIX_IDL_SETUP}" ]]; then
  echo "Robonix lifecycle overlay did not produce ${ROBONIX_IDL_SETUP}" >&2
  exit 1
fi

set +u
# shellcheck disable=SC1090
source "${ROBONIX_IDL_SETUP}"
set -u
colcon --log-base "${BUILD_ROOT}/log" build \
  --base-paths "${ROOT}" \
  --packages-select internvln_navigation \
  --symlink-install \
  --build-base "${BUILD_ROOT}/build" \
  --install-base "${BUILD_ROOT}/install"
echo "[internvln_navigation/build] done"
