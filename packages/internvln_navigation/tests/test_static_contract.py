"""Structural invariants that must hold no matter how the code is refactored.

These tests read the source rather than running it.  They encode the safety
properties that are easy to break silently in a later edit: dry-run arming
nothing, the guard being the only writer of `/cmd_vel`, and the offline-testable
modules never growing a ROS import.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "internvln_navigation"
REPO = ROOT.parents[1]


def code_tokens(tree: ast.AST) -> list[str]:
    """Every identifier and non-docstring string literal in one module.

    Comments never reach the AST and docstrings are dropped on purpose: both are
    prose that is *allowed* to name the vendor chassis while explaining why this
    package must never touch it.  What is left is what the interpreter would
    actually execute, which is the only thing worth forbidding.
    """

    docstrings = {
        id(entry.value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        for entry in node.body[:1]
        if isinstance(entry, ast.Expr)
        and isinstance(entry.value, ast.Constant)
        and isinstance(entry.value.value, str)
    }

    tokens: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.alias):
            tokens.append(node.name)
        elif isinstance(node, ast.arg):
            tokens.append(node.arg)
        elif isinstance(node, ast.keyword):
            if node.arg:
                tokens.append(node.arg)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            tokens.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                tokens.append(node.value)
    return tokens


def module_tokens() -> str:
    return "\n".join(
        token
        for path in sorted(MODULE.glob("*.py"))
        for token in code_tokens(ast.parse(path.read_text(encoding="utf-8")))
    )


def is_live_gate(test: ast.expr) -> bool:
    """True for the node's own `self._config.mode == "live"` if-condition.

    Compared structurally, not by unparsed text: ``ast.unparse`` renders a string
    constant with single quotes, so a literal-text comparison matches nothing and
    the test passes vacuously.
    """

    return (
        isinstance(test, ast.Compare)
        and ast.unparse(test.left) == "self._config.mode"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "live"
    )


def publishers(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_publisher"
    ]


def twist_publishers(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in publishers(tree)
        if node.args and ast.unparse(node.args[0]) == "Twist"
    ]


class ConfigContractTests(unittest.TestCase):
    def test_the_packaged_defaults_are_exact(self) -> None:
        config = (ROOT / "config" / "internvln_navigation.yaml").read_text(
            encoding="utf-8"
        )
        for expected in (
            "mode: dry-run",
            "rgb_topic: /camera/color/image_raw",
            "command_topic: /ranger/vln/cmd_vel_raw",
            "instruction_topic: /ranger/vln/instruction",
            "status_topic: /ranger/vln/status",
            "server_url: http://127.0.0.1:5801",
            "model_input_mode: full_frame",
            "model_crop_size: 384",
            "dispatch_hz: 50.0",
            "plan_hold_s: 1.5",
            "max_vx: 0.15",
            "max_wz: 0.30",
            "nav_raw_topic: /ranger/vln/nav_cmd_vel_raw",
            "vln_raw_topic: /ranger/vln/cmd_vel_raw",
            "selected_output_topic: /cmd_vel_nav",
            "selected_source: vln",
            "guard_input_topic: /cmd_vel_nav",
            "guard_output_topic: /cmd_vel",
            "guard_turn_policy: clamp_yaw",
            "guard_min_turn_radius_m: 0.81",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, config)

    def test_the_config_never_points_the_server_url_at_a_single_endpoint(self) -> None:
        document = yaml.safe_load(
            (ROOT / "config" / "internvln_navigation.yaml").read_text(
                encoding="utf-8"
            )
        )
        url = document["internvln_navigation"]["ros__parameters"]["server_url"]
        # The client appends /eval_dual itself, so a base url carrying it would
        # be requested as /eval_dual/eval_dual.  Only the value is checked: the
        # surrounding comments name the route on purpose, to explain exactly
        # this.
        for endpoint in ("/health", "/task/start", "/eval_dual", "/task/report"):
            with self.subTest(endpoint=endpoint):
                self.assertNotIn(endpoint, url)

    def test_the_packaged_config_declares_exactly_the_runtime_fields(self) -> None:
        """A key the dataclass dropped, or one it gained, must not go unnoticed.

        The launch file turns this document straight into ROS parameters, so a
        stale key is silently ignored and a missing one silently takes a package
        default -- both of which read as "the manifest said so".
        """

        from dataclasses import fields

        from internvln_navigation.core import RuntimeConfig

        document = yaml.safe_load(
            (ROOT / "config" / "internvln_navigation.yaml").read_text(
                encoding="utf-8"
            )
        )
        declared = set(document["internvln_navigation"]["ros__parameters"])
        expected = {field.name for field in fields(RuntimeConfig)}
        self.assertEqual(declared - expected, set(), "config keys nothing reads")
        self.assertEqual(expected - declared, set(), "fields the config omits")


class DeletedRouteTests(unittest.TestCase):
    """The server publishes one route; nothing here may call the other three.

    ``/health``, ``/task/start`` and ``/task/report`` belong to an earlier
    revision of this package.  They exist in neither upstream's
    ``http_internvla_server.py`` nor the two local deployment documents, and
    every call to one of them is an HTTP 404 on the real robot.
    """

    DELETED = ("/health", "/task/start", "/task/report")

    def test_no_module_requests_a_route_the_server_does_not_have(self) -> None:
        for path in sorted(MODULE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for route in self.DELETED:
                with self.subTest(module=path.name, route=route):
                    self.assertNotIn(f'"{route}"', source)
                    self.assertNotIn(f"'{route}'", source)

    def test_the_client_knows_exactly_one_endpoint(self) -> None:
        source = (MODULE / "http_client.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('"/eval_dual"'), 1)


class DryRunArmingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_dry_run_creates_no_velocity_publisher(self) -> None:
        live_blocks = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.If) and is_live_gate(node.test)
        ]
        self.assertEqual(len(live_blocks), 1, "there must be exactly one live gate")

        live_twist = [
            node for node in twist_publishers(live_blocks[0])
        ]
        self.assertEqual(len(live_twist), 2, "raw + selected velocity publishers")

        # Every Twist publisher in the whole node must live inside that gate.
        all_twist = twist_publishers(self.tree)
        self.assertEqual(len(all_twist), 2)
        live_lines = {
            id(node) for node in ast.walk(live_blocks[0])
        }
        for node in all_twist:
            with self.subTest(line=node.lineno):
                self.assertIn(id(node), live_lines)

    def test_the_status_topic_is_a_string_and_publishes_in_both_modes(self) -> None:
        status = [
            node
            for node in publishers(self.tree)
            if node.args and ast.unparse(node.args[0]) == "String"
        ]
        self.assertEqual(len(status), 1)
        # It is created before the live gate, so dry-run can be observed too.
        self.assertLess(status[0].lineno, min(n.lineno for n in twist_publishers(self.tree)))

    def test_the_instruction_subscription_exists_outside_the_live_gate(self) -> None:
        subscriptions = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_subscription"
        ]
        instruction = [
            node
            for node in subscriptions
            if node.args and ast.unparse(node.args[1]) == "self._config.instruction_topic"
        ]
        self.assertEqual(len(instruction), 1)
        self.assertLess(
            instruction[0].lineno,
            min(n.lineno for n in twist_publishers(self.tree)),
        )

    def test_the_depth_subscription_exists_outside_the_live_gate(self) -> None:
        # The gate has to see depth in dry-run too, or its settings and its
        # decisions can never be checked before arming the chassis.
        subscriptions = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_subscription"
        ]
        depth = [
            node
            for node in subscriptions
            if node.args and ast.unparse(node.args[1]) == "self._config.depth_topic"
        ]
        self.assertEqual(len(depth), 1)
        self.assertLess(
            depth[0].lineno,
            min(n.lineno for n in twist_publishers(self.tree)),
        )


class CommandChainTests(unittest.TestCase):
    def test_the_chassis_guard_is_the_only_writer_of_cmd_vel(self) -> None:
        guard = (MODULE / "chassis_guard_node.py").read_text(encoding="utf-8")
        node = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        self.assertIn("self._config.guard_output_topic", guard)
        # The VLN node publishes only to its own raw topic and the mux output.
        self.assertNotIn("guard_output_topic", node)
        self.assertNotIn('"/cmd_vel"', node)

    def test_nothing_here_talks_to_the_chassis_directly(self) -> None:
        tokens = module_tokens()
        # Word boundaries matter: this package owns RangerChassisGuardNode and
        # registers it as "ranger_chassis_guard", and neither is a reference to
        # the vendor's ranger_chassis provider.
        for forbidden in (
            r"\branger_chassis\b",  # the vendor provider id and ROS node name
            r"\bRangerROSMessenger\b",  # its Twist callback
            r"\bcan_ranger\b",  # the CAN interface it owns
            r"\bpython[-_]can\b",
            r"\bsocketcan\b",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertIsNone(re.search(forbidden, tokens))

    def test_no_unitree_or_posture_control_surface_exists(self) -> None:
        tokens = module_tokens()
        for forbidden in (
            "SportClient",
            "StopMove",
            "/api/sport/request",
            "/lowcmd",
            "unitree_sdk",
            "StandUp",
            "StandDown",
            "RecoveryStand",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, tokens)


class ThreadingContractTests(unittest.TestCase):
    def test_the_instruction_ingress_never_calls_the_blocking_endpoint(self) -> None:
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        # The ROS callback that accepts an instruction may only queue it; the
        # blocking model step belongs to the inference worker thread.
        callbacks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_on_instruction"
        ]
        self.assertEqual(len(callbacks), 1)
        body = ast.unparse(callbacks[0])
        self.assertIn("RUNTIME.set_instruction", body)
        self.assertNotIn("_client", body)
        # ...and the one thing an operator with only `ros2 topic pub` can do to
        # halt the robot must be reachable from the same door.
        self.assertIn("STOP_PAYLOADS", body)
        self.assertIn("RUNTIME.stop", body)

    def test_only_the_worker_thread_performs_http_inference(self) -> None:
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        # The dispatch timer reads the plan store; it must never call evaluate.
        dispatch = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
        ]
        self.assertEqual(len(dispatch), 1)
        body = ast.unparse(dispatch[0])
        self.assertIn("self._plans.dispatch(", body)
        self.assertNotIn("evaluate(", body)

    def test_the_dispatch_gate_replaces_the_command_not_the_plan(self) -> None:
        # Two separate duties, both in the dispatch tick.  Zeroing the command
        # is the reflex and must happen on every blocked tick; retiring the task
        # (plan + client) happens once on the closing edge, through RUNTIME,
        # because disarming is what stops the model being asked again.  The
        # dispatch thread must never reach into the plan store itself.
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        dispatch = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
        ]
        self.assertEqual(len(dispatch), 1)
        body = ast.unparse(dispatch[0])
        self.assertIn("self._safety_gate.check(", body)
        # The stepper decides whether this tick may still drive the plan
        # (stop-and-go, and closed loop when odometry is fresh), and the safety
        # gate overrides whatever it decided.
        self.assertIn("self._stepper.apply(state, now=now, pose=pose)", body)
        # The pose must be looked up on every tick, with the staleness window
        # -- never cached across ticks.
        self.assertIn("self._latest_pose(now=now)", body)
        self.assertIn("self._step_gate.set()", body)
        self.assertIn("ZERO_COMMAND if decision.blocked else step_command", body)
        self.assertIn("self._plans.dispatch(", body)
        self.assertIn("RUNTIME.set_safety_state(", body)
        self.assertNotIn("self._plans.clear()", body)

    def test_the_frame_carries_the_pose_it_was_taken_in(self) -> None:
        # The frame keeps the pose it was taken in: it is the record of what
        # the model was looking at, and the frame a trajectory answer would
        # have to be expressed in.  It is deliberately *not* what a discrete
        # answer is measured from -- see the next test.
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        image = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_on_image"
        ]
        self.assertEqual(len(image), 1)
        self.assertIn("pose=pose", ast.unparse(image[0]))

    def test_the_worker_measures_from_the_pose_the_answer_lands_on(self) -> None:
        # The reference client re-reads its current odometry when the answer
        # arrives (homo_goal = self.homo_odom.copy()) and applies the model's
        # steps to that.  Measuring from the frame's pose instead puts the goal
        # one step in the past: it arrives already reached (the robot brakes)
        # and its heading is a second old (the robot steers back to it, which
        # is the S-shaped approach).
        worker = (MODULE / "worker.py").read_text(encoding="utf-8")
        self.assertIn("self._pose_source()", worker)
        self.assertNotIn("reference_pose=frame.pose", worker)
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        self.assertIn("pose_source=self._latest_pose", source)

    def test_the_dispatch_tick_servos_toward_the_plans_goal(self) -> None:
        # The control loop must drive from the newest pose to the plan's goal
        # and never block; asking the model is the worker's job, on its own
        # thread, so a request in flight does not stop the wheels.
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        dispatch = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
        ]
        self.assertEqual(len(dispatch), 1)
        body = ast.unparse(dispatch[0])
        self.assertIn("servo_command(", body)
        self.assertIn("state.goal", body)
        self.assertIn("self._config.servo_enabled", body)
        # And the stop-and-go fallback is still there, for when the servo is
        # off or there is no pose to servo from.
        self.assertIn("self._stepper.apply(state, now=now, pose=pose)", body)

    def test_a_plan_with_geometry_is_never_driven_blind(self) -> None:
        # With no fresh pose the servo holds; it does not fall back to the
        # timed execution that would drive the same distance without measuring.
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        self.assertIn("SERVO_WAIT_ODOM,", source)
        self.assertIn("ZERO_COMMAND, SERVO_WAIT_ODOM", source)
        # A trajectory answer is geometry too, even though no segment carries
        # its turn/distance: without this a trajectory plan with no pose fell
        # through to the open-loop velocity hold, which is the one thing the
        # servo exists to stop doing.
        self.assertIn("bool(state.waypoints) or any(", source)
        self.assertIn("pose is None and wants_geometry", source)

    def test_a_trajectory_answer_becomes_a_goal(self) -> None:
        # A held velocity cannot say "arrived".  Reducing the path to the point
        # it pursues is what gives a trajectory plan an arrival state and a
        # closed loop, and it is derived from the same pose a discrete answer
        # uses -- the one the answer landed on, not the frame's.
        core = (MODULE / "core.py").read_text(encoding="utf-8")
        self.assertIn("goal_from_waypoints(", core)
        self.assertIn("if goal is None and plan.waypoints:", core)
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        self.assertIn("lookahead_m=self._config.lookahead_m,", source)
        self.assertIn("trajectory_stop_m=self._config.trajectory_stop_m,", source)

    def test_the_worker_is_only_gated_when_the_servo_is_off(self) -> None:
        # Gating with the servo on would put the request back inside the
        # execution loop -- exactly the stutter being removed.  The gate is the
        # stop-and-go interlock, so it exists only when the servo is off.
        tree = ast.parse((MODULE / "ros_node.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "InferenceWorker"
        ]
        self.assertEqual(len(calls), 1)
        gate = [kw for kw in calls[0].keywords if kw.arg == "gate"]
        self.assertEqual(len(gate), 1)
        self.assertIn(
            "not self._config.servo_enabled", ast.unparse(gate[0].value)
        )

    def test_the_status_reports_the_gate_decision(self) -> None:
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        self.assertIn('"safety": {', source)
        self.assertIn("self._safety_decision.snapshot()", source)
        self.assertIn("self._config.depth_topic", source)

    def test_the_capability_reports_a_closed_gate_as_blocked(self) -> None:
        # "blocked" has to come from the gate, not from the model's state: a
        # held robot whose last model state was "running" is exactly the lie
        # this reports against.
        source = (MODULE / "provider.py").read_text(encoding="utf-8")
        self.assertIn('status="blocked"', source)
        self.assertIn('"held"', source)
        self.assertIn("if accepted and blocked", source)

    def test_a_sustained_outage_retires_the_running_plan(self) -> None:
        source = (MODULE / "ros_node.py").read_text(encoding="utf-8")
        self.assertIn("isinstance(error, ModelUnavailableError)", source)
        self.assertIn("self._plans.clear()", source)

    def test_the_offline_modules_never_import_ros(self) -> None:
        for name in (
            "core.py",
            "action_adapter.py",
            "http_client.py",
            "worker.py",
            "source_mux.py",
            "vln_runtime.py",
            "image_preprocess.py",
            "chassis_guard.py",
            "safety_gate.py",
        ):
            with self.subTest(module=name):
                source = (MODULE / name).read_text(encoding="utf-8")
                self.assertNotIn("import rclpy", source)
                self.assertNotIn("from rclpy", source)
                self.assertNotIn("import cv_bridge", source)


class ProviderIdentityTests(unittest.TestCase):
    def test_manifest_and_provider_identity_match(self) -> None:
        manifest = (ROOT / "package_manifest.yaml").read_text(encoding="utf-8")
        provider = (MODULE / "provider.py").read_text(encoding="utf-8")
        self.assertIn("name: robonix/primitive/vln/driver", manifest)
        self.assertIn("name: robonix/primitive/vln/task", manifest)
        self.assertIn(
            'Primitive(id="internvln_navigation", namespace="robonix/primitive/vln")',
            provider,
        )
        self.assertIn("@provider.mcp(TASK_CONTRACT)", provider)
        for callback in ("on_init", "on_activate", "on_deactivate", "on_shutdown"):
            self.assertIn(f"@provider.{callback}", provider)

    def test_the_task_contract_idl_is_shipped(self) -> None:
        srv = (
            ROOT
            / "capabilities"
            / "lib"
            / "internvln_navigation_control"
            / "srv"
            / "VlnTask.srv"
        ).read_text(encoding="utf-8")
        request, response = srv.split("---")
        for field in ("string operation", "string instruction"):
            self.assertIn(field, request)
        for field in ("string status", "string detail"):
            self.assertIn(field, response)
        # The server has no task registry, so there is no task id to carry.
        self.assertNotIn("task_id", response)

        contract = (ROOT / "capabilities" / "task.v1.toml").read_text(encoding="utf-8")
        self.assertIn('id = "robonix/primitive/vln/task"', contract)
        self.assertIn(
            'idl = "internvln_navigation_control/srv/VlnTask.srv"', contract
        )

    def test_build_generates_and_start_loads_mcp_bindings(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        start = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertRegex(build, r"codegen\s+-p\s+.*--mcp\s+--ros2")
        self.assertIn(
            "robonix_mcp_types/internvln_navigation_control_mcp.py", start
        )
        self.assertIn("codegen/robonix_mcp_types", start)
        self.assertIn("python3 -m internvln_navigation.provider", start)


def shell_code(text: str) -> str:
    """Only the lines a shell would execute.

    Same reasoning as `code_tokens`: prose is allowed to *name* a thing while
    explaining why the script must not do it -- a comment saying there is no
    escalation fallback is not an escalation.  Whole-line comments are dropped;
    inline comments are left in, so the scripts these tests scan must keep their
    comments on their own lines.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


class OperatorScriptTests(unittest.TestCase):
    """The commands the manual tells an operator to type must actually run.

    Both of these are documented as `scripts/<name>.sh ...` -- invoked directly,
    not through `bash` -- so a missing executable bit is a `Permission denied`
    on the robot, found at the worst possible moment.  That is exactly what
    `set_vln_instruction.sh` did until it was caught here, and the deployment's
    scripts are not all committed the same way (the rest are `bash`-invoked and
    carry no bit on purpose), so this cannot be left to `ls`.
    """

    DIRECTLY_INVOKED = ("set_vln_instruction.sh", "set_vln_mode.sh")

    def test_the_docs_still_invoke_them_that_way(self) -> None:
        # If a doc ever switches to `bash scripts/x.sh`, the file no longer has
        # to be executable and this test is asserting a fossil.
        for name in self.DIRECTLY_INVOKED:
            with self.subTest(script=name):
                mentioned = [
                    path.name
                    for path in (REPO / "docs").glob("*.md")
                    if f"scripts/{name}" in path.read_text(encoding="utf-8")
                ]
                self.assertTrue(mentioned, f"no doc references scripts/{name}")

    def test_they_are_executable(self) -> None:
        for name in self.DIRECTLY_INVOKED:
            with self.subTest(script=name):
                path = REPO / "scripts" / name
                self.assertTrue(path.is_file(), f"scripts/{name} is missing")
                self.assertTrue(
                    os.access(path, os.X_OK), f"scripts/{name} is not executable"
                )


class OfflineHygieneTests(unittest.TestCase):
    # A build or a start must never escalate or reconfigure the machine.  These
    # hold for every script in the package, the installer included.
    NEVER_ALLOWED = (
        r"\bsudo\b",
        r"\bnmcli\b",
        r"\bip\s+(?:addr|route|link)\b",
        r"\bsystemctl\b",
        r"\bdocker\b",
    )

    def test_build_and_start_do_not_install_or_change_networking(self) -> None:
        scripts = shell_code(
            "\n".join(
                path.read_text(encoding="utf-8")
                for path in (
                    ROOT / "build.sh",
                    ROOT / "start.sh",
                    ROOT / "tests" / "run_offline_tests.sh",
                )
            )
        )
        for forbidden in self.NEVER_ALLOWED + (r"\bapt(?:-get)?\b",):
            with self.subTest(forbidden=forbidden):
                self.assertIsNone(re.search(forbidden, scripts))

    def test_the_only_installer_is_ensure_deps(self) -> None:
        """Package installation lives in exactly one place, and that place
        installs only what is missing.

        `rbnx build` checks no dependencies and installs none, so the gap is
        closed by scripts/ensure_deps.sh, invoked from build.sh.  That is the one
        file allowed to name apt -- escalation, networking and docker stay banned
        there too, and the import probe must precede the install so a container
        that already has everything never touches the package manager.
        """
        installer = shell_code(
            (ROOT / "scripts" / "ensure_deps.sh").read_text(encoding="utf-8")
        )
        for forbidden in self.NEVER_ALLOWED:
            with self.subTest(forbidden=forbidden):
                self.assertIsNone(re.search(forbidden, installer))
        self.assertIn("apt-get install -y --no-install-recommends", installer)
        self.assertLess(
            installer.index('python3 -c "import'),
            installer.index("apt-get update"),
        )
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/ensure_deps.sh", build)

    def test_no_old_stack_residue_remains(self) -> None:
        sources = "\n".join(
            path.read_text(encoding="utf-8") for path in MODULE.glob("*.py")
        )
        for forbidden in (
            "RobotTrackHttpClient",
            "parse_inference_response",
            "FollowDistance",
            "RgbdDistance",
            "CameraFrameUploadWorker",
            "distance_runtime",
            "go2_robottrack",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, sources)


if __name__ == "__main__":
    unittest.main()
