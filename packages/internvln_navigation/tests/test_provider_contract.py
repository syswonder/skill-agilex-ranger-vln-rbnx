"""Robonix lifecycle and task-capability contract, with the ROS runtime stubbed."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]


class Result:
    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail


class FakePrimitive:
    def __init__(self, *, id: str, namespace: str) -> None:
        self.id = id
        self.namespace = namespace
        self.callbacks = {}

    def _decorator(self, name):
        def register(callback):
            self.callbacks[name] = callback
            return callback

        return register

    @property
    def on_init(self):
        return self._decorator("init")

    @property
    def on_activate(self):
        return self._decorator("activate")

    @property
    def on_deactivate(self):
        return self._decorator("deactivate")

    @property
    def on_shutdown(self):
        return self._decorator("shutdown")

    def mcp(self, contract_id):
        return self._decorator(f"mcp:{contract_id}")

    def run(self):
        raise AssertionError("provider.run must not be called by offline tests")


def load_provider_module():
    fake_api = types.ModuleType("robonix_api")
    fake_api.Primitive = FakePrimitive
    fake_api.Ok = lambda: Result("ok")
    fake_api.Err = lambda detail: Result("err", detail)
    fake_api.Deferred = lambda detail: Result("deferred", detail)
    sys.modules["robonix_api"] = fake_api

    # Stands in for rbnx-build/codegen/robonix_mcp_types/..._mcp.py, which only
    # exists after `rbnx codegen --mcp` has run.
    fake_mcp = types.ModuleType("internvln_navigation_control_mcp")

    class FakeMessage:
        def __init__(self, **fields):
            for name, value in fields.items():
                setattr(self, name, value)

    fake_mcp.VlnTask_Request = FakeMessage
    fake_mcp.VlnTask_Response = FakeMessage
    sys.modules["internvln_navigation_control_mcp"] = fake_mcp

    sys.modules.pop("internvln_navigation.provider", None)
    return importlib.import_module("internvln_navigation.provider")


def request_type(**fields):
    """Build a request against the stubbed generated message class."""

    return sys.modules["internvln_navigation_control_mcp"].VlnTask_Request(
        **fields
    )


class ProviderContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_provider_module()
        # Each test starts from a provider that owns nothing.
        self.module._active = False
        self.module._runtime_thread = None
        self.module._runtime_stop = None
        self.module._runtime_exited = None
        self.module._runtime_errors = []
        # The MCP handlers share one process-global runtime; a safety verdict,
        # a queue or a completion latch left by another test would leak into
        # this one.  unbind() clears all of them and also forgets the gate.
        self.module.RUNTIME.unbind()
        # Completion here is frame-count only; the 30 s floor is a runtime
        # test, not something a contract test should wait out.
        self.module.RUNTIME.configure(
            finish_after_stops=3, finish_after_stops_min_s=0.0
        )

    def test_live_lifecycle_starts_and_stops_ros_thread(self) -> None:
        self.assertEqual(self.module.provider.id, "internvln_navigation")
        self.assertEqual(self.module.provider.namespace, "robonix/primitive/vln")

        observed = {}

        def runner(config, stop_event, ready_event, errors):
            del errors
            observed.update(config)
            ready_event.set()
            stop_event.wait(2.0)

        self.module._ros_runner = runner
        init_result = self.module.initialize(
            {
                "mode": "live",
                "rgb_topic": "/camera/color/image_raw",
                "server_url": "http://10.0.0.5:5801",
                "instruction": "walk to the door",
                "source_mux": {
                    "nav_input_topic": "/ranger/vln/nav_cmd_vel_raw",
                    "vln_input_topic": "/ranger/vln/cmd_vel_raw",
                    "output_topic": "/cmd_vel_nav",
                    "selected_source": "vln",
                },
                "camera_name": "camera_435i",
                "asset_manifest": "/opt/models/assets.json",
                "upstream_root": "/opt/InternVLA-N1",
            }
        )
        self.assertEqual(init_result.kind, "ok")
        self.assertEqual(self.module.activate().kind, "ok")
        self.assertEqual(observed["mode"], "live")
        # Deployment metadata is consumed by the provider, not by the node: the
        # ROS parameters must stay exactly the RuntimeConfig surface.
        for leaked in ("asset_manifest", "upstream_root", "camera_name"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, observed)
        self.assertEqual(observed["vln_raw_topic"], "/ranger/vln/cmd_vel_raw")
        self.assertEqual(observed["instruction"], "walk to the door")
        self.assertEqual(observed["server_url"], "http://10.0.0.5:5801")
        self.assertEqual(self.module.deactivate().kind, "ok")

    def test_activate_rejects_runner_exit_after_liveness_check(self) -> None:
        module = self.module
        release_runner = threading.Event()
        runner_returned = threading.Event()

        def runner(config, stop_event, ready_event, errors):
            del config, stop_event, errors
            ready_event.set()
            release_runner.wait(2.0)
            runner_returned.set()

        real_thread_type = threading.Thread

        class StaleAliveThread(real_thread_type):
            """Expose the old check-then-commit race deterministically."""

            released = False

            def is_alive(self) -> bool:
                alive = super().is_alive()
                if alive and not self.released:
                    self.released = True
                    release_runner.set()
                    exited = module._runtime_exited
                    if exited is not None:
                        exited.wait(2.0)
                    # Simulate the stale True already returned by the old
                    # pre-lock liveness check after the runner has exited.
                    return True
                return alive

        module._ros_runner = runner
        self.assertEqual(module.initialize({"mode": "live"}).kind, "ok")

        with mock.patch.object(module.threading, "Thread", StaleAliveThread):
            result = module.activate()

        self.assertEqual(result.kind, "err")
        self.assertIn("exited during startup", result.detail)
        self.assertTrue(runner_returned.is_set())
        self.assertFalse(module._active)
        self.assertIsNone(module._runtime_thread)
        self.assertIsNone(module._runtime_stop)
        self.assertIsNone(module._runtime_exited)

    def test_shutdown_stops_active_runtime(self) -> None:
        stopped = threading.Event()

        def runner(config, stop_event, ready_event, errors):
            del config, errors
            ready_event.set()
            stop_event.wait(2.0)
            stopped.set()

        self.module._ros_runner = runner
        self.assertEqual(self.module.initialize({"mode": "live"}).kind, "ok")
        self.assertEqual(self.module.activate().kind, "ok")
        self.assertEqual(self.module.shutdown().kind, "ok")
        self.assertTrue(stopped.is_set())

    def test_stop_timeout_retains_ownership_and_blocks_second_runtime(self) -> None:
        module = self.module

        class StuckRuntime:
            def __init__(self) -> None:
                self.join_timeouts = []

            def join(self, timeout=None) -> None:
                self.join_timeouts.append(timeout)

            def is_alive(self) -> bool:
                return True

        runtime = StuckRuntime()
        stop_event = threading.Event()
        module._config = module.RuntimeConfig.from_mapping(
            {"mode": "live"}
        ).as_ros_parameters()
        module._runtime_thread = runtime
        module._runtime_stop = stop_event
        module._active = True

        stopped = module.deactivate()

        self.assertEqual(stopped.kind, "err")
        self.assertTrue(stop_event.is_set())
        self.assertEqual(runtime.join_timeouts, [4.0])
        self.assertIs(module._runtime_thread, runtime)
        self.assertIs(module._runtime_stop, stop_event)
        self.assertEqual(module.activate().kind, "deferred")

    def test_thread_start_failure_releases_unstarted_ownership(self) -> None:
        module = self.module

        class FailingThread:
            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs

            def start(self) -> None:
                raise RuntimeError("synthetic thread exhaustion")

        self.assertEqual(module.initialize({"mode": "live"}).kind, "ok")
        with mock.patch.object(module.threading, "Thread", FailingThread):
            result = module.activate()
        self.assertEqual(result.kind, "err")
        self.assertIn("could not start", result.detail)
        self.assertIsNone(module._runtime_thread)
        self.assertIsNone(module._runtime_stop)

    def test_default_initialization_remains_dry_run(self) -> None:
        self.assertEqual(self.module.initialize({}).kind, "ok")
        self.assertEqual(self.module._config["mode"], "dry-run")
        self.assertFalse(self.module.RUNTIME.bound)

    def test_initialization_rejects_a_camera_url_with_credentials(self) -> None:
        result = self.module.initialize(
            {"server_url": "http://user:secret@10.0.0.5:5801"}
        )
        self.assertEqual(result.kind, "err")
        self.assertIn("credentials", result.detail)

    def test_packaged_default_yaml_initializes_consistently(self) -> None:
        module = self.module
        document = yaml.safe_load(
            (ROOT / "config" / "internvln_navigation.yaml").read_text(
                encoding="utf-8"
            )
        )
        parameters = document["internvln_navigation"]["ros__parameters"]

        result = module.initialize(parameters)

        self.assertEqual(result.kind, "ok", result.detail)
        self.assertEqual(module._config["max_vx"], 0.15)
        self.assertEqual(module._config["plan_hold_s"], 1.5)
        self.assertEqual(module._config["selected_source"], "vln")
        self.assertEqual(module._config["mode"], "dry-run")

    def test_task_capability_is_registered_under_the_vln_namespace(self) -> None:
        self.assertIn(
            "mcp:robonix/primitive/vln/task",
            self.module.provider.callbacks,
        )

    def test_task_set_queues_when_the_runtime_is_not_bound(self) -> None:
        # Only an unbound (or gated/rejected) set answers without waiting: a
        # bound set blocks until the episode ends, which is what keeps one plan
        # node alive for one task.  Nothing here is bound to a ROS node.
        module = self.module
        self.assertEqual(module.initialize({}).kind, "ok")

        response = module.vln_task(
            request_type(operation="set", instruction="walk to the door")
        )

        self.assertTrue(response.accepted)
        self.assertEqual(response.status, "queued")
        self.assertEqual(response.instruction, "walk to the door")
        # The capability must not claim the model has started: nothing has been
        # asked of it yet, and the runtime is not bound to a client.
        self.assertFalse(response.running)
        self.assertFalse(hasattr(response, "task_id"))

    def test_a_bound_set_waits_until_the_episode_finishes(self) -> None:
        # One blocking `set` = one plan node that lives as long as the task.
        # If it returned early, Pilot would read the plan as done and dispatch
        # the same instruction again (the retrigger loop).
        module = self.module

        class _Client:
            def __init__(self) -> None:
                self.armed = False

            def set_instruction(self, instruction: str) -> None:
                del instruction
                self.armed = True

            def clear_instruction(self, note: str = "") -> None:
                del note
                self.armed = False

            def snapshot(self) -> dict:
                return {"armed": self.armed}

        class _Plans:
            def clear(self) -> None:
                pass

        class _Plan:
            model_stop = True

        module.RUNTIME.bind(client=_Client(), plans=_Plans())
        answer = {}

        def call() -> None:
            answer["response"] = module.vln_task(
                request_type(
                    operation="set", instruction="go to the chair and stop"
                )
            )

        worker = threading.Thread(target=call)
        worker.start()
        worker.join(0.5)
        self.assertTrue(worker.is_alive(), "set returned before the task ended")

        module.RUNTIME.pump()  # arm the client
        for _ in range(3):  # finish_after_stops=3, no time floor (setUp)
            module.RUNTIME.note_plan(_Plan())
        module.RUNTIME.pump()  # apply the completion

        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        response = answer["response"]
        self.assertEqual(response.status, "finished")
        self.assertFalse(response.running)

    def test_task_rejects_an_empty_instruction(self) -> None:
        module = self.module
        self.assertEqual(module.initialize({}).kind, "ok")
        response = module.vln_task(request_type(operation="set", instruction="  "))
        self.assertFalse(response.accepted)
        self.assertEqual(response.status, "rejected")
        self.assertIn("1..1000", response.detail)

    def test_task_stop_queues_and_get_reports_state(self) -> None:
        module = self.module
        self.assertEqual(module.initialize({}).kind, "ok")

        stopped = module.vln_task(request_type(operation="stop", instruction=""))
        self.assertTrue(stopped.accepted)
        self.assertEqual(stopped.status, "stopping")

        status = module.vln_task(request_type(operation="get", instruction=""))
        self.assertTrue(status.accepted)
        # Nothing bound means nothing is in force: the capability must say so
        # rather than imply the model is working on something.
        self.assertEqual(status.status, "awaiting-instruction")
        self.assertIn("bound=False", status.detail)
        self.assertIn("armed=False", status.detail)

    def test_task_get_reports_a_closed_gate_as_blocked(self) -> None:
        module = self.module
        self.assertEqual(module.initialize({}).kind, "ok")
        module.RUNTIME.set_safety_state(
            blocked=True, reason="obstacle", nearest_m=0.22, stop_distance_m=0.30
        )

        status = module.vln_task(request_type(operation="get", instruction=""))
        self.assertTrue(status.accepted)
        # The primitive is stopped, not slowly thinking: saying "running" here
        # is what made the robot look like it was ignoring a wall.
        self.assertFalse(status.running)
        self.assertEqual(status.status, "blocked")
        self.assertIn("reason=obstacle", status.detail)
        self.assertIn("nearest=0.22m", status.detail)
        self.assertIn("no model requests", status.detail)

    def test_task_set_while_blocked_is_held_and_not_started(self) -> None:
        module = self.module
        self.assertEqual(module.initialize({}).kind, "ok")
        module.RUNTIME.set_safety_state(blocked=True, reason="obstacle")

        response = module.vln_task(
            request_type(operation="set", instruction="walk to the door")
        )
        self.assertTrue(response.accepted)
        self.assertEqual(response.status, "held")
        self.assertFalse(response.running)
        self.assertEqual(response.instruction, "walk to the door")
        self.assertIn("held", response.detail)

        # Once the gate reopens the held task is what `get` reports again.
        module.RUNTIME.set_safety_state(blocked=False, reason="clear")
        status = module.vln_task(request_type(operation="get", instruction=""))
        self.assertEqual(status.status, "awaiting-instruction")
        self.assertEqual(status.instruction, "walk to the door")

    def test_task_get_reports_repeated_stops_as_finished(self) -> None:
        module = self.module
        self.assertEqual(module.initialize({}).kind, "ok")

        class Sink:
            def __init__(self) -> None:
                self.applied: list[str] = []

            def set_instruction(self, instruction: str) -> None:
                self.applied.append(instruction)

            def clear_instruction(self, note: str = "") -> None:
                self.cleared = note

        sink = Sink()
        module.RUNTIME.bind(client=sink)
        module.RUNTIME.set_instruction("go to the chair and stop.")
        module.RUNTIME.pump()
        for _ in range(3):
            module.RUNTIME.note_plan(types.SimpleNamespace(model_stop=True))
        module.RUNTIME.pump()

        status = module.vln_task(request_type(operation="get", instruction=""))
        self.assertTrue(status.accepted)
        # This is the signal an executor needs to end the leaf: not "running",
        # and not "awaiting an instruction" as if nothing had ever happened.
        self.assertFalse(status.running)
        self.assertEqual(status.status, "finished")
        self.assertEqual(status.instruction, "go to the chair and stop.")
        self.assertIn("finished", status.detail)
        self.assertIn("go to the chair and stop.", status.detail)

        # A new task lifts the latch and is reported as pending again.
        module.RUNTIME.set_instruction("go to the door and stop.")
        status = module.vln_task(request_type(operation="get", instruction=""))
        self.assertNotEqual(status.status, "finished")

    def test_task_rejects_an_unknown_operation(self) -> None:
        module = self.module
        self.assertEqual(module.initialize({}).kind, "ok")
        response = module.vln_task(request_type(operation="explode", instruction=""))
        self.assertFalse(response.accepted)
        self.assertIn("unknown operation", response.detail)


if __name__ == "__main__":
    unittest.main()
