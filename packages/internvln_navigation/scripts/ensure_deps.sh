#!/usr/bin/env bash
# Ensure this package's runtime dependencies exist inside the container.
#
# Why this exists: `rbnx build` runs no rosdep and checks no dependencies -- the
# only thing it verifies is that rbnx itself and /opt/ros/<distro>/setup.bash
# are present.  The rbnx package template says the rest is the package's job
# ("append your own steps ... if your package needs them").  Without this, a
# missing cv2 or cv_bridge is invisible until the node imports it at start time,
# long after the build reported success.
#
# Check first, install only what is actually absent: a container that already
# has everything pays one `python3 -c "import ..."` per module and nothing else.
#
# The test is importability, not package identity -- what matters at runtime is
# that the module imports, whether it came from apt, `pip install --user` or the
# ROS image itself.
#
# Override: RBNX_SKIP_DEPS_CHECK=1 skips the whole check (air-gapped runs).
set -euo pipefail

ROS_DISTRO="${ROS_DISTRO:-humble}"

# "<python module>:<apt package>".  The apt name is used only for modules that
# are actually missing, and follows Ubuntu 22.04 / ROS 2 Humble naming.
DEPS=(
  "cv2:python3-opencv"
  "numpy:python3-numpy"
  "rclpy:ros-${ROS_DISTRO}-rclpy"
  "cv_bridge:ros-${ROS_DISTRO}-cv-bridge"
  "geometry_msgs:ros-${ROS_DISTRO}-geometry-msgs"
  "sensor_msgs:ros-${ROS_DISTRO}-sensor-msgs"
  "std_msgs:ros-${ROS_DISTRO}-std-msgs"
)

if [[ "${RBNX_SKIP_DEPS_CHECK:-}" == "1" ]]; then
  echo "[internvln_navigation/deps] skipped (RBNX_SKIP_DEPS_CHECK=1)"
  exit 0
fi

missing=()
for entry in "${DEPS[@]}"; do
  if ! python3 -c "import ${entry%%:*}" >/dev/null 2>&1; then
    missing+=("$entry")
  fi
done

if [[ "${#missing[@]}" -eq 0 ]]; then
  echo "[internvln_navigation/deps] all ${#DEPS[@]} runtime deps importable, nothing to install"
  exit 0
fi

packages=()
for entry in "${missing[@]}"; do
  packages+=("${entry##*:}")
  echo "[internvln_navigation/deps] missing: ${entry%%:*}"
done

# Root only, and deliberately no sudo fallback: inside the container this runs
# as root, so escalation is never needed -- and a build script that can sudo
# itself is one that can rewrite the host.  Not root means the environment is
# wrong, which is worth reporting rather than papering over.
if [[ "$(id -u)" -ne 0 ]]; then
  echo "[internvln_navigation/deps] not root (uid $(id -u)); cannot install." >&2
  echo "    inside the container this runs as root.  To fix the image instead:" >&2
  echo "      apt-get install -y ${packages[*]}" >&2
  exit 1
fi

echo "[internvln_navigation/deps] installing: ${packages[*]}"
apt-get update
if ! apt-get install -y --no-install-recommends "${packages[@]}"; then
  echo "[internvln_navigation/deps] install failed -- this container is probably offline." >&2
  echo "    install manually, or bake it into the image:" >&2
  echo "      apt-get install -y ${packages[*]}" >&2
  exit 1
fi

# Re-check.  apt can exit 0 without making the module importable -- a shadowing
# ~/.local copy, a mismatch between cv_bridge and python3-opencv, or an install
# into a different interpreter.  Failing here names the module; without this the
# failure would reappear as an ImportError inside the running node.
unresolved=()
for entry in "${missing[@]}"; do
  python3 -c "import ${entry%%:*}" >/dev/null 2>&1 || unresolved+=("${entry%%:*}")
done
if [[ "${#unresolved[@]}" -ne 0 ]]; then
  echo "[internvln_navigation/deps] still not importable after install: ${unresolved[*]}" >&2
  exit 1
fi

echo "[internvln_navigation/deps] done"
