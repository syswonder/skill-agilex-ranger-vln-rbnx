from __future__ import annotations

import unittest

from internvln_navigation.vln_runtime import VlnRuntime


class FakeClient:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.cleared: list[str] = []
        self.armed = False

    def set_instruction(self, instruction: str) -> None:
        self.instructions.append(instruction)
        self.armed = True

    def clear_instruction(self, note: str = "") -> None:
        self.cleared.append(note)
        self.armed = False

    def snapshot(self) -> dict:
        # The real client's shape, minimally: the runtime asks whether the
        # episode is armed to tell "running" from "waiting".
        return {"armed": self.armed}


class FakePlan:
    def __init__(self, model_stop: bool) -> None:
        self.model_stop = model_stop


class FakePlans:
    def __init__(self) -> None:
        self.clears = 0

    def clear(self) -> None:
        self.clears += 1


class VlnRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = VlnRuntime(default_instruction="walk forward")

    def test_pump_is_a_no_op_when_nothing_is_pending(self) -> None:
        self.assertIsNone(self.runtime.pump())

    def test_an_instruction_is_queued_then_applied_by_the_ros_thread(self) -> None:
        client = FakeClient()
        self.runtime.bind(client=client, plans=FakePlans())

        queued = self.runtime.set_instruction("walk to the door")
        self.assertTrue(queued["accepted"])
        # The RPC handler must not have touched the client yet.
        self.assertEqual(client.instructions, [])

        applied = self.runtime.pump()
        self.assertEqual(applied, {"operation": "set", "instruction": "walk to the door"})
        self.assertEqual(client.instructions, ["walk to the door"])
        # Drained: a second pump does nothing.
        self.assertIsNone(self.runtime.pump())

    def test_only_the_latest_instruction_survives_a_burst(self) -> None:
        client = FakeClient()
        self.runtime.bind(client=client)
        self.runtime.set_instruction("first")
        self.runtime.set_instruction("second")
        self.runtime.set_instruction("third")
        self.runtime.pump()
        self.assertEqual(client.instructions, ["third"])

    def test_pending_work_is_requeued_while_the_runtime_is_unbound(self) -> None:
        queued = self.runtime.set_instruction("walk to the door")
        self.assertTrue(queued["accepted"])
        # The node has not come up yet, so the instruction must not be lost.
        self.assertIsNone(self.runtime.pump())

        client = FakeClient()
        self.runtime.bind(client=client)
        self.assertIsNotNone(self.runtime.pump())
        self.assertEqual(client.instructions, ["walk to the door"])

    def test_stop_retires_the_plan_and_takes_the_task_off_the_client(self) -> None:
        client = FakeClient()
        plans = FakePlans()
        self.runtime.bind(client=client, plans=plans)

        stopped = self.runtime.stop()
        self.assertTrue(stopped["accepted"])
        # The plan store belongs to the ROS executor thread; the RPC thread only
        # queues the request.
        self.assertEqual(plans.clears, 0)

        applied = self.runtime.pump()
        self.assertIsNotNone(applied)
        assert applied is not None
        self.assertEqual(applied["operation"], "stop")
        self.assertEqual(plans.clears, 1)
        self.assertEqual(client.instructions, [])
        # Retiring the plan is not enough -- the disarmed client is what stops
        # the next frame from re-asking the model and driving off again.
        self.assertEqual(len(client.cleared), 1)

    def test_stop_wins_over_a_queued_instruction(self) -> None:
        client = FakeClient()
        plans = FakePlans()
        self.runtime.bind(client=client, plans=plans)

        self.runtime.set_instruction("walk to the door")
        self.runtime.stop()
        applied = self.runtime.pump()
        assert applied is not None
        self.assertEqual(applied["operation"], "stop")
        self.assertEqual(plans.clears, 1)
        self.assertEqual(client.instructions, [])
        self.assertEqual(len(client.cleared), 1)

    def test_an_empty_or_oversized_instruction_is_rejected(self) -> None:
        self.assertFalse(self.runtime.set_instruction("   ")["accepted"])
        self.assertFalse(self.runtime.set_instruction("x" * 1001)["accepted"])
        self.assertIsNone(self.runtime.pump())

    def test_unbind_detaches_both_sinks(self) -> None:
        client = FakeClient()
        plans = FakePlans()
        self.runtime.bind(client=client, plans=plans)
        self.assertTrue(self.runtime.bound)

        self.runtime.unbind()
        self.assertFalse(self.runtime.bound)

        self.runtime.set_instruction("walk")
        self.assertIsNone(self.runtime.pump())
        self.assertEqual(client.instructions, [])

    def test_status_reports_the_last_applied_instruction(self) -> None:
        self.runtime.bind(client=FakeClient(), plans=FakePlans())
        self.runtime.set_instruction("walk to the door")
        self.runtime.pump()

        status = self.runtime.status()
        self.assertTrue(status["bound"])
        self.assertEqual(status["last_applied_instruction"], "walk to the door")
        self.assertIsNone(status["pending_instruction"])
        self.assertEqual(status["default_instruction"], "walk forward")

    def test_set_default_only_changes_the_reported_default(self) -> None:
        self.runtime.set_default("stand still")
        self.assertEqual(self.runtime.default_instruction, "stand still")
        self.assertIsNone(self.runtime.pump())

    def _finish_the_task(self, instruction: str = "go to the chair and stop.") -> None:
        self.runtime.configure(finish_after_stops=2, finish_after_stops_min_s=0.0)
        self.runtime.set_instruction(instruction)
        self.runtime.pump()
        for _ in range(2):
            self.runtime.note_plan(FakePlan(model_stop=True))
        self.runtime.pump()
        self.assertTrue(self.runtime.status()["finished"])

    def test_a_repeated_set_of_a_finished_task_does_not_restart_it(self) -> None:
        # Pilot replans the moment a plan completes and dispatches the same
        # instruction again.  That echo used to wipe the completion latch and
        # start the robot on the identical walk -- forever.
        client = FakeClient()
        self.runtime.bind(client=client, plans=FakePlans())
        self._finish_the_task()
        applied = list(client.instructions)

        echo = self.runtime.set_instruction("go to the chair and stop.")
        self.assertTrue(echo["accepted"])
        self.assertTrue(echo["already_finished"])
        self.assertIsNone(self.runtime.pump())
        self.assertEqual(client.instructions, applied)
        self.assertTrue(self.runtime.status()["finished"])

    def test_a_different_instruction_after_a_finish_starts_a_new_task(self) -> None:
        client = FakeClient()
        self.runtime.bind(client=client, plans=FakePlans())
        self._finish_the_task()

        started = self.runtime.set_instruction("go to the door and stop.")
        self.assertFalse(started.get("already_finished"))
        self.runtime.pump()
        self.assertEqual(client.instructions[-1], "go to the door and stop.")
        self.assertFalse(self.runtime.status()["finished"])

    def test_an_identical_set_while_running_is_not_a_second_episode(self) -> None:
        client = FakeClient()
        self.runtime.bind(client=client, plans=FakePlans())
        self.runtime.set_instruction("go to the chair and stop.")
        self.runtime.pump()

        again = self.runtime.set_instruction("go to the chair and stop.")
        self.assertTrue(again["accepted"])
        self.assertTrue(again["already_running"])
        # Nothing is queued, so the ROS thread has nothing to apply.
        self.assertIsNone(self.runtime.pump())
        self.assertEqual(client.instructions, ["go to the chair and stop."])

    def test_an_operator_stop_is_terminal_for_a_waiting_caller(self) -> None:
        client = FakeClient()
        self.runtime.bind(client=client, plans=FakePlans())
        self.runtime.set_instruction("go to the chair and stop.")
        self.runtime.pump()

        self.runtime.stop()
        status = self.runtime.status()
        self.assertEqual(status["terminal_kind"], "stopped")
        self.assertEqual(status["terminal_instruction"], "go to the chair and stop.")
        # The stop is applied on the ROS thread, which retires the plan before
        # the next instruction can be armed.
        self.runtime.pump()
        # And a stop-then-set of the same words is a genuine re-run, not an
        # echo: the operator asked for it.
        restarted = self.runtime.set_instruction("go to the chair and stop.")
        self.assertFalse(restarted.get("already_finished"))
        self.assertFalse(restarted.get("already_running"))


class SafetyGateRuntimeTests(unittest.TestCase):
    """A closed proximity gate is a stop, not just a zero velocity."""

    def setUp(self) -> None:
        self.runtime = VlnRuntime()
        self.client = FakeClient()
        self.plans = FakePlans()
        self.runtime.bind(client=self.client, plans=self.plans)

    def test_a_closed_gate_halts_the_task_and_disarms_the_client(self) -> None:
        self.runtime.set_instruction("walk to the door")
        self.assertIsNotNone(self.runtime.pump())
        self.assertEqual(self.client.instructions, ["walk to the door"])

        action = self.runtime.set_safety_state(
            blocked=True, reason="obstacle", nearest_m=0.22, stop_distance_m=0.30
        )
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action["operation"], "safety-stop")
        # Both halves of a stop: the plan stops driving, and the client stops
        # asking the model, which is what ends the "keep requesting VLN" loop.
        self.assertEqual(self.plans.clears, 1)
        self.assertEqual(self.client.cleared, ["safety gate closed: obstacle"])

        status = self.runtime.status()
        self.assertTrue(status["safety"]["blocked"])
        self.assertEqual(status["safety"]["reason"], "obstacle")
        self.assertEqual(status["safety"]["nearest_m"], 0.22)
        self.assertEqual(status["safety"]["stops"], 1)
        # Nothing is left for the ROS timer to arm.
        self.assertIsNone(self.runtime.pump())

    def test_repeated_blocked_ticks_retire_the_task_once(self) -> None:
        self.assertIsNotNone(
            self.runtime.set_safety_state(blocked=True, reason="obstacle")
        )
        self.assertIsNone(
            self.runtime.set_safety_state(
                blocked=True, reason="obstacle", nearest_m=0.55
            )
        )
        self.assertEqual(self.plans.clears, 1)
        self.assertEqual(len(self.client.cleared), 1)
        # The distance keeps being refreshed even without a transition.
        self.assertEqual(self.runtime.status()["safety"]["nearest_m"], 0.55)

    def test_an_instruction_issued_while_blocked_is_held_not_armed(self) -> None:
        self.runtime.set_safety_state(blocked=True, reason="obstacle")

        queued = self.runtime.set_instruction("walk to the door")
        self.assertTrue(queued["accepted"])
        self.assertIn("held", queued["detail"])
        self.assertIsNone(self.runtime.pump())
        self.assertEqual(self.client.instructions, [])
        status = self.runtime.status()
        self.assertIsNone(status["pending_instruction"])
        self.assertEqual(status["safety"]["held_instruction"], "walk to the door")

        # Reopening releases it; the next pump arms the client again.
        cleared = self.runtime.set_safety_state(blocked=False, reason="clear")
        self.assertIsNotNone(cleared)
        applied = self.runtime.pump()
        assert applied is not None
        self.assertEqual(applied["operation"], "set")
        self.assertEqual(self.client.instructions, ["walk to the door"])

    def test_a_queued_instruction_is_held_when_the_gate_closes_first(self) -> None:
        # The operator queues a task, then the gate closes before the ROS timer
        # runs.  The pending task must be held, not applied.
        self.runtime.set_instruction("walk to the door")
        self.runtime.set_safety_state(blocked=True, reason="obstacle")
        self.assertIsNone(self.runtime.pump())
        self.assertEqual(self.client.instructions, [])
        self.assertEqual(
            self.runtime.status()["safety"]["held_instruction"], "walk to the door"
        )

    def test_pump_cannot_arm_a_task_after_the_gate_closes(self) -> None:
        # Directly inject the race window: pending work exists and the gate
        # closed without going through the hold path.
        self.runtime._pending_instruction = "walk to the door"
        self.runtime._safety_blocked = True
        self.runtime._safety_reason = "obstacle"

        held = self.runtime.pump()
        assert held is not None
        self.assertEqual(held["operation"], "hold")
        self.assertEqual(self.client.instructions, [])

    def test_an_operator_stop_drops_a_held_instruction(self) -> None:
        self.runtime.set_safety_state(blocked=True, reason="obstacle")
        self.runtime.set_instruction("walk to the door")

        self.runtime.stop()
        applied = self.runtime.pump()
        assert applied is not None
        self.assertEqual(applied["operation"], "stop")
        self.assertIsNone(self.runtime.status()["safety"]["held_instruction"])

        self.runtime.set_safety_state(blocked=False, reason="clear")
        self.assertIsNone(self.runtime.pump())
        self.assertEqual(self.client.instructions, [])

    def test_unbind_forgets_the_gate_verdict(self) -> None:
        self.runtime.set_safety_state(blocked=True, reason="no_depth")
        self.assertTrue(self.runtime.status()["safety"]["blocked"])
        self.runtime.unbind()
        self.assertFalse(self.runtime.status()["safety"]["blocked"])


class ModelCompletionTests(unittest.TestCase):
    """Repeated model STOP answers end the task, like an operator stop."""

    def setUp(self) -> None:
        self.runtime = VlnRuntime()
        self.client = FakeClient()
        self.plans = FakePlans()
        self.runtime.bind(client=self.client, plans=self.plans)
        # Counting only: the time floor has its own test, and a real 30 s
        # sleep in a unit test is not a test.
        self.runtime.configure(finish_after_stops=3, finish_after_stops_min_s=0.0)

    def _stop_run(self, count: int) -> None:
        for _ in range(count):
            self.runtime.note_plan(FakePlan(True))

    def test_repeated_stops_finish_the_task(self) -> None:
        self._stop_run(2)
        # Below the threshold nothing happens: a couple of STOP answers could
        # still be followed by a driving frame.
        self.assertIsNone(self.runtime.pump())
        self.assertEqual(self.plans.clears, 0)

        self._stop_run(1)
        finished = self.runtime.pump()
        assert finished is not None
        self.assertEqual(finished["operation"], "finish")
        # Same two halves as an operator stop: no plan keeps driving, and the
        # client is disarmed so the next frame cannot re-ask the model.
        self.assertEqual(self.plans.clears, 1)
        self.assertEqual(len(self.client.cleared), 1)
        status = self.runtime.status()
        self.assertTrue(status["finished"])
        self.assertEqual(status["consecutive_stops"], 0)
        self.assertIsNone(status["stop_run_for_s"])
        self.assertEqual(status["finished_stops"], 3)
        # Nothing left to apply on the next tick.
        self.assertIsNone(self.runtime.pump())

    def test_a_driving_frame_breaks_the_run(self) -> None:
        self._stop_run(2)
        self.runtime.note_plan(FakePlan(False))
        self.assertEqual(self.runtime.status()["consecutive_stops"], 0)
        self._stop_run(2)
        self.assertIsNone(self.runtime.pump())
        self.assertFalse(self.runtime.status()["finished"])

    def test_a_plan_without_the_flag_is_not_a_stop(self) -> None:
        self.runtime.note_plan(object())
        self.assertEqual(self.runtime.status()["consecutive_stops"], 0)

    def test_a_stop_burst_without_enough_time_does_not_finish(self) -> None:
        # The frames alone are not the criterion: three zeros in a row over a
        # couple of seconds can happen anywhere and must not end a task.
        self.runtime.configure(finish_after_stops=3, finish_after_stops_min_s=30.0)
        self._stop_run(3)
        self.assertIsNone(self.runtime.pump())
        self.assertFalse(self.runtime.status()["finished"])
        self.assertEqual(self.plans.clears, 0)

        # The model is still saying STOP long after the count was reached, so
        # the run is now a stuck loop and does finish.
        self.runtime._stop_run_started_at -= 31.0
        self._stop_run(1)
        finished = self.runtime.pump()
        assert finished is not None
        self.assertEqual(finished["operation"], "finish")
        self.assertGreaterEqual(self.runtime.status()["finished_after_s"], 30.0)

    def test_the_thresholds_are_configurable(self) -> None:
        self.runtime.configure(finish_after_stops=1, finish_after_stops_min_s=0.0)
        status = self.runtime.status()
        self.assertEqual(status["finish_after_stops"], 1)
        self.assertEqual(status["finish_after_stops_min_s"], 0.0)
        self._stop_run(1)
        finished = self.runtime.pump()
        assert finished is not None
        self.assertEqual(finished["operation"], "finish")

    def test_a_new_instruction_starts_a_new_task(self) -> None:
        self._stop_run(3)
        self.runtime.pump()
        self.assertTrue(self.runtime.status()["finished"])

        self.runtime.set_instruction("go to the door and stop")
        status = self.runtime.status()
        self.assertFalse(status["finished"])
        self.assertIsNone(status["finished_instruction"])
        applied = self.runtime.pump()
        assert applied is not None
        self.assertEqual(applied["operation"], "set")
        self.assertEqual(self.client.instructions, ["go to the door and stop"])

        # Counting starts over: one STOP in the new task is not completion.
        self._stop_run(1)
        self.assertEqual(self.runtime.status()["consecutive_stops"], 1)

    def test_an_operator_stop_clears_a_pending_finish(self) -> None:
        self._stop_run(3)
        self.runtime.stop()
        applied = self.runtime.pump()
        assert applied is not None
        self.assertEqual(applied["operation"], "stop")
        self.assertFalse(self.runtime.status()["finished"])

    def test_stop_answers_after_the_finish_are_ignored(self) -> None:
        self._stop_run(3)
        self.runtime.pump()
        self._stop_run(3)
        # The client is disarmed, so nothing should be arriving anyway; if a
        # frame does, it must not re-trigger completion or reset the latch.
        self.assertIsNone(self.runtime.pump())
        self.assertTrue(self.runtime.status()["finished"])

    def test_a_finish_waits_for_the_ros_side_to_bind(self) -> None:
        runtime = VlnRuntime()
        runtime.configure(finish_after_stops=3, finish_after_stops_min_s=0.0)
        for _ in range(3):
            runtime.note_plan(FakePlan(True))
        # Nothing is wired yet; the request must be put back, not dropped.
        self.assertIsNone(runtime.pump())

        client = FakeClient()
        runtime.bind(client=client, plans=FakePlans())
        finished = runtime.pump()
        assert finished is not None
        self.assertEqual(finished["operation"], "finish")
        self.assertEqual(len(client.cleared), 1)

    def test_a_closed_gate_does_not_swallow_the_finish(self) -> None:
        self.runtime.set_safety_state(blocked=True, reason="obstacle")
        self._stop_run(3)
        finished = self.runtime.pump()
        assert finished is not None
        self.assertEqual(finished["operation"], "finish")
        self.assertTrue(self.runtime.status()["finished"])

    def test_unbind_forgets_the_completion(self) -> None:
        self._stop_run(3)
        self.runtime.pump()
        self.assertTrue(self.runtime.status()["finished"])
        self.runtime.unbind()
        self.assertFalse(self.runtime.status()["finished"])


if __name__ == "__main__":
    unittest.main()
