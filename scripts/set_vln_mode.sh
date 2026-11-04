#!/usr/bin/env bash
# Switch the InternVLA-N1 stack between dry-run and live -- in the one place
# that actually decides it, without hand-editing YAML.
#
# `mode` is the single arming switch:
#   * `start.sh` greps robonix_manifest.yaml for it to decide whether to REFUSE
#     arming with an unreachable model;
#   * robonix passes the manifest's `config:` block to the provider, which builds
#     RuntimeConfig from it -- and that is what gates the Twist publishers and
#     the chassis guard.
#
# The packaged config/internvln_navigation.yaml carries a `mode:` key too, and
# this script deliberately does NOT touch it.  The deployment path never reads
# it (the package's own start.sh execs the provider with no parameter file), so
# flipping it would change nothing and only leave the two files disagreeing.
# It stays `dry-run`: that is the correct fail-safe default for the one path
# that does read it (launch/internvln_navigation.launch.py).
#
# Arming is a file edit plus a restart, by design -- a running stack does not
# pick this up.  This script performs the EDIT ONLY.  It never starts or stops
# anything, and it never touches the navigation stack.
#
# Usage:
#   scripts/set_vln_mode.sh                       # show current mode + what is running
#   scripts/set_vln_mode.sh status
#   scripts/set_vln_mode.sh dry-run               # safe direction; no confirmation
#   scripts/set_vln_mode.sh live --yes            # WILL drive the wheels
#
# Exit codes:
#   0  changed, or already in the requested mode
#   1  error (manifest missing/unreadable/ambiguous)
#   2  usage
#   3  refused: `live` without --yes
#   4  refused: the navigation stack still owns can_ranger
#
# ROBONIX_MANIFEST overrides the manifest path (same variable start.sh/stop.sh
# use), which is also how the tests drive it against a temporary copy.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="${ROBONIX_MANIFEST:-$DEPLOY_DIR/robonix_manifest.yaml}"
BOOT_STATE="$DEPLOY_DIR/rbnx-boot/state.json"

ACTION=""
CONFIRMED=0

usage() {
  cat >&2 <<'EOF'
usage: set_vln_mode.sh [dry-run|live|status] [--yes]

  dry-run   arm nothing: no velocity publisher, no chassis guard (default state)
  live      arm: the guard starts and this stack WILL drive the wheels
  status    print the current mode and what is running (default when no action)

  --yes     required for `live`; there is no confirmation prompt on a robot
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    dry-run | live | status) ACTION="$1" ;;
    --yes | -y) CONFIRMED=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if [[ ! -f "$MANIFEST" ]]; then
  echo "manifest not found: $MANIFEST" >&2
  echo "set ROBONIX_MANIFEST, or run this from the deployment directory" >&2
  exit 1
fi

# An ambiguous manifest is refused before anything else, including `status`.
# start.sh greps for `mode: live` and silently obeys whichever key comes first,
# so a file that says two things is a configuration error, not something to
# interpret -- and reporting "the mode is live" for such a file would be a lie.
assert_unambiguous_mode() {
  python3 - "$MANIFEST" <<'PY'
import re
import sys

pattern = re.compile(r"^(\s*)mode:(\s*)(dry-run|live)(\s*(?:#.*)?)$")
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    hits = [
        index
        for index, line in enumerate(handle.read().splitlines())
        if pattern.match(line)
    ]
if len(hits) != 1:
    print(
        f"expected exactly one `mode:` key in {path}, found {len(hits)}; "
        "fix the file by hand rather than guessing which one is the switch",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

if ! assert_unambiguous_mode; then
  exit 1
fi

# Same regex start.sh uses, so the two can never disagree about the mode.  A
# manifest with no `mode: live` line -- including one that is unparseable --
# reads as dry-run: fail closed.
current_mode() {
  if grep -Eq '^[[:space:]]*mode:[[:space:]]+live([[:space:]]|$)' "$MANIFEST"; then
    printf 'live'
  else
    printf 'dry-run'
  fi
}

# The navigation stack and this stack both own can_ranger and /cmd_vel.  Two
# owners on one bus is unpredictable motion, so arming is refused while it runs.
#
# `pgrep -f` matches whole command lines, so a shell whose command line merely
# *mentions* these words -- `bash -c 'pgrep -f ranger_base_node'`, any wrapper
# that quotes the pattern -- looks exactly like the navigation stack.  This
# script's own process and its ancestors are therefore skipped: a real chassis
# process is never an ancestor of the thing deciding whether to arm.  Refusing
# to arm on a false positive is the safe direction, but it makes the check
# useless the moment it cries wolf.
nav_stack_pids() {
  local pid=$$ ancestor matched line out=""
  declare -A skip=()

  while [[ "$pid" =~ ^[0-9]+$ && "$pid" != "0" && "$pid" != "1" ]]; do
    skip["$pid"]=1
    ancestor="$(awk '/^PPid:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)"
    [[ -n "$ancestor" ]] || break
    pid="$ancestor"
  done

  while read -r matched line; do
    [[ -n "${skip[$matched]:-}" ]] && continue
    out+="$matched $line"$'\n'
  done < <(pgrep -af 'ranger_base_node|velocity_guard' 2>/dev/null || true)

  printf '%s' "$out"
}

stack_is_booted() { [[ -f "$BOOT_STATE" ]]; }

print_status() {
  local mode
  mode="$(current_mode)"
  echo "manifest : $MANIFEST"
  echo "mode     : $mode"
  if [[ "$mode" == "live" ]]; then
    echo "           ARMED -- the guard starts and the wheels can turn"
  else
    echo "           dry-run -- no velocity publisher exists, nothing can move"
  fi
  if stack_is_booted; then
    echo "stack    : booted (rbnx-boot/state.json present)"
    if [[ -n "$(nav_stack_pids)" ]]; then
      echo "nav stack: RUNNING -- two owners on can_ranger:"
      nav_stack_pids | sed 's/^/           /'
    else
      echo "nav stack: not detected"
    fi
  else
    echo "stack    : not booted"
    if [[ -n "$(nav_stack_pids)" ]]; then
      echo "nav stack: RUNNING"
      nav_stack_pids | sed 's/^/           /'
    fi
  fi
}

# Rewrite exactly one `mode:` line, atomically, keeping its indentation and any
# trailing comment.  Anything other than exactly one match is a hard error: a
# second `mode:` key would mean the file says two things and start.sh's grep
# only sees the first.
apply_mode() {
  local target="$1"
  python3 - "$MANIFEST" "$target" <<'PY'
import os
import re
import stat
import sys
import tempfile

path, target = sys.argv[1], sys.argv[2]
pattern = re.compile(r"^(\s*)mode:(\s*)(dry-run|live)(\s*(?:#.*)?)$")

with open(path, encoding="utf-8") as handle:
    lines = handle.read().splitlines(keepends=True)

hits = [i for i, line in enumerate(lines) if pattern.match(line.rstrip("\n"))]
if len(hits) != 1:
    print(
        f"expected exactly one `mode:` key in {path}, found {len(hits)}; "
        "fix the file by hand rather than guessing which one is the switch",
        file=sys.stderr,
    )
    raise SystemExit(1)

index = hits[0]
match = pattern.match(lines[index].rstrip("\n"))
old, indent, gap, comment = match.group(3), match.group(1), match.group(2), match.group(4)
if old == target:
    print(f"unchanged:{old}")
    raise SystemExit(0)

newline = "\n" if lines[index].endswith("\n") else ""
lines[index] = f"{indent}mode:{gap}{target}{comment}{newline}"

# Atomic, and the mode bits are carried over: mkstemp creates 0600, and the
# manifest is normally 0644 in a shared checkout.
handle, temporary = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
try:
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write("".join(lines))
    os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
    os.replace(temporary, path)
except BaseException:
    os.unlink(temporary)
    raise

print(f"changed:{old}->{target}")
PY
}

next_steps() {
  local mode="$1"
  echo
  echo "next:"
  if stack_is_booted; then
    echo "  bash stop.sh                            # a running stack ignores this edit"
    echo "  bash start.sh"
  else
    echo "  bash start.sh"
  fi
  if [[ "$mode" == "live" ]]; then
    echo
    echo "  before that: navigation stack stopped, can_ranger free, 10 m clear"
    echo "               ahead, physical remote in hand."
    echo "  start.sh must print the 'WARNING: mode is live' line -- if it does not,"
    echo "  you are still in dry-run."
    echo "  confirm with: python3 scripts/validate_manifest_consistency.py"
    echo "                (expect: command mode is 'live' (ARMED))"
  else
    echo "  confirm with: python3 scripts/validate_manifest_consistency.py"
    echo "                (expect: command mode is 'dry-run' (no velocity is published))"
  fi
}

case "$ACTION" in
  "" | status)
    print_status
    exit 0
    ;;
esac

if [[ "$ACTION" == "$(current_mode)" ]]; then
  echo "already $ACTION; nothing to change"
  print_status
  exit 0
fi

if [[ "$ACTION" == "live" ]]; then
  if [[ "$CONFIRMED" -ne 1 ]]; then
    echo "refusing to arm without --yes." >&2
    echo "live starts the chassis guard and this stack WILL drive the wheels;" >&2
    echo "there is no avoidance, no localisation and no software e-stop in it." >&2
    echo "re-run as: scripts/set_vln_mode.sh live --yes" >&2
    exit 3
  fi
  # Fail-open, but never silently: this check is an extra guard on top of the
  # documented procedure (stop the navigation stack first), so an unanswerable
  # question must be said out loud rather than read as "nothing is running".
  if ! command -v pgrep >/dev/null 2>&1; then
    echo "note: pgrep is not installed, so the navigation-stack check could not" >&2
    echo "      run.  Confirm by hand that nothing else owns can_ranger." >&2
  elif [[ -n "$(nav_stack_pids)" ]]; then
    echo "refusing to arm: the navigation stack is still running." >&2
    nav_stack_pids >&2
    echo "both stacks own can_ranger and /cmd_vel -- stop it first." >&2
    exit 4
  fi
  echo "WARNING: arming. One word in the manifest is all that stands between a"
  echo "         dry-run and a 1.4-tonne chassis."
fi

result="$(apply_mode "$ACTION")"
echo "$result"
echo
grep -n -E '^[[:space:]]*mode:[[:space:]]+(dry-run|live)' "$MANIFEST" | sed 's/^/  /'
next_steps "$ACTION"
