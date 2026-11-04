"""`scripts/set_vln_mode.sh` -- the one-word arming switch.

The switch is safety-critical, so it is tested as a black box: every case runs
the real script as a subprocess against a **temporary copy** of the manifest and
then reads that copy back.  The deployment's own manifest is hashed before and
after each case and must come back byte-identical -- a test suite that quietly
armed the robot would be worse than no test suite.

What is pinned here:

* the edit touches exactly one line (the `mode:` key) and nothing else;
* it is atomic in effect -- indentation, trailing comments and the file mode
  survive;
* `live` needs an explicit `--yes`, and is refused outright while the
  navigation stack is running (one CAN bus, one owner);
* an ambiguous manifest -- two `mode:` keys -- is a hard error rather than a
  guess, because `start.sh`'s grep would only ever see the first one;
* the script edits a file and nothing else: it must never grow the ability to
  boot, shut down or otherwise operate the stack.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "set_vln_mode.sh"
MANIFEST = REPO / "robonix_manifest.yaml"

MODE_LINE = re.compile(r"^(\s*)mode:(\s*)(dry-run|live)(\s*(?:#.*)?)$")


def run_script(manifest: Path, *args: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["ROBONIX_MANIFEST"] = str(manifest)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=60,
    )


class ModeSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="vln-mode-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.manifest = self.directory / "robonix_manifest.yaml"
        shutil.copy2(MANIFEST, self.manifest)
        self.original = self.manifest.read_text(encoding="utf-8").splitlines(True)
        self.deployment_manifest = MANIFEST.read_bytes()

    def tearDown(self) -> None:
        # The whole point: whatever the script did, it did to the copy.
        self.assertEqual(MANIFEST.read_bytes(), self.deployment_manifest)

    def lines(self) -> list[str]:
        return self.manifest.read_text(encoding="utf-8").splitlines(True)

    def mode_of(self, lines: list[str]) -> str:
        matches = [MODE_LINE.match(line.rstrip("\n")) for line in lines]
        found = [match for match in matches if match]
        self.assertEqual(len(found), 1, "the manifest must carry exactly one mode: key")
        return found[0].group(3)

    def test_dry_run_to_live_changes_one_line_and_nothing_else(self) -> None:
        self.assertEqual(self.mode_of(self.original), "dry-run")

        result = run_script(self.manifest, "live", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.lines()
        self.assertEqual(len(after), len(self.original))
        changed = [
            index
            for index, (before, now) in enumerate(zip(self.original, after))
            if before != now
        ]
        self.assertEqual(len(changed), 1, f"only the mode line may move: {changed}")
        self.assertEqual(self.mode_of(after), "live")
        self.assertEqual(
            after[changed[0]],
            self.original[changed[0]].replace("dry-run", "live"),
        )

    def test_live_to_dry_run_is_the_same_edit_backwards(self) -> None:
        self.assertEqual(run_script(self.manifest, "live", "--yes").returncode, 0)

        result = run_script(self.manifest, "dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mode_of(self.lines()), "dry-run")

    def test_asking_for_the_mode_it_is_already_in_is_not_an_error(self) -> None:
        result = run_script(self.manifest, "dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already dry-run", result.stdout)
        self.assertEqual(self.lines(), self.original)

    def test_live_without_yes_is_refused(self) -> None:
        result = run_script(self.manifest, "live")

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("--yes", result.stderr)
        self.assertEqual(self.lines(), self.original)

    def test_an_ambiguous_manifest_is_a_hard_error(self) -> None:
        self.manifest.write_text(
            "a:\n      mode: dry-run\nb:\n      mode: live\n", encoding="utf-8"
        )

        result = run_script(self.manifest, "live", "--yes")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("exactly one", result.stderr)
        # Untouched: guessing which of two keys is the switch is how a robot
        # gets armed by a typo.
        self.assertEqual(
            self.manifest.read_text(encoding="utf-8"),
            "a:\n      mode: dry-run\nb:\n      mode: live\n",
        )

    def test_indentation_comment_and_file_mode_survive(self) -> None:
        self.manifest.write_text(
            "name: x\nconfig:\n      mode: dry-run   # keep me\n"
            "  # mode: live <- commented out, not the switch\n",
            encoding="utf-8",
        )
        os.chmod(self.manifest, 0o644)

        result = run_script(self.manifest, "live", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.manifest.read_text(encoding="utf-8")
        self.assertIn("      mode: live   # keep me\n", text)
        self.assertIn("  # mode: live <- commented out, not the switch\n", text)
        # One real key, and the commented-out one is still commented out.
        self.assertEqual(self.mode_of(text.splitlines(True)), "live")
        self.assertEqual(os.stat(self.manifest).st_mode & 0o777, 0o644)

    def test_status_reports_the_mode_without_changing_it(self) -> None:
        result = run_script(self.manifest, "status")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run", result.stdout)
        self.assertEqual(self.lines(), self.original)

    def test_a_running_navigation_stack_blocks_arming(self) -> None:
        """Driven with a decoy process rather than the real chassis stack.

        The decoy is a *sibling* of the script, never an ancestor: the script
        skips its own process tree, because a wrapper whose command line merely
        quotes the pattern looks exactly like the navigation stack and would
        otherwise refuse to arm a perfectly idle robot.
        """
        if not shutil.which("pgrep") or not Path("/proc/self/status").exists():
            self.skipTest("pgrep and /proc are required")

        decoy = subprocess.Popen(
            ["bash", "-c", "exec -a ranger_base_node sleep 30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            result = run_script(self.manifest, "live", "--yes")
        finally:
            decoy.terminate()
            decoy.wait(timeout=10)

        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("navigation stack", result.stderr)
        self.assertEqual(self.lines(), self.original)

    def test_the_script_edits_a_file_and_operates_nothing(self) -> None:
        """It must stay a switch, not a launcher.

        `next_steps` is allowed to *print* the commands an operator should run;
        what it must never contain is the machinery to run them itself.
        """
        source = SCRIPT.read_text(encoding="utf-8")

        for forbidden in ("rbnx boot", "rbnx shutdown", "docker run", "docker rm"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
