#!/usr/bin/env bash
# Offline validation gate for the InternVLA-N1 deployment.  No ROS graph, no
# camera, no network, no robot access -- safe to run while the navigation stack
# is live, and safe to run on a laptop.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== VLN primitive (internvln_navigation) ==="
bash "${ROOT}/packages/internvln_navigation/tests/run_offline_tests.sh"

echo
echo "=== manifest consistency ==="
PYTHONDONTWRITEBYTECODE=1 python3 "${ROOT}/scripts/validate_manifest_consistency.py"

echo
echo "offline validation OK"
