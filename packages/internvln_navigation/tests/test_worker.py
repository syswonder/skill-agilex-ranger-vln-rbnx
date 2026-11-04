from __future__ import annotations

import threading
import time
import unittest

from internvln_navigation.core import (
    InferencePlan,
    LatestFrameMailbox,
    PlanStore,
    VelocityCommand,
)
from internvln_navigation.odometry import Pose
from internvln_navigation.worker import InferenceWorker


class BlockingEvaluator:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sequences: list[int] = []

    def evaluate(self, frame):
        self.sequences.append(frame.sequence)
        if len(self.sequences) == 1:
            self.entered.set()
            self.release.wait(1.0)
        return InferencePlan(VelocityCommand(frame.sequence * 0.01, 0.0), None)


class WorkerTests(unittest.TestCase):
    def test_busy_worker_skips_intermediate_frames_and_takes_latest(self) -> None:
        mailbox = LatestFrameMailbox()
        plans = PlanStore()
        evaluator = BlockingEvaluator()
        worker = InferenceWorker(mailbox, evaluator, plans)
        worker.start()
        try:
            mailbox.put(b"one", source_timestamp=1.0)
            self.assertTrue(evaluator.entered.wait(1.0))
            mailbox.put(b"two", source_timestamp=2.0)
            mailbox.put(b"three", source_timestamp=3.0)
            evaluator.release.set()
            deadline = time.monotonic() + 1.0
            while len(evaluator.sequences) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(evaluator.sequences[:2], [1, 3])
            self.assertNotIn(2, evaluator.sequences)
        finally:
            evaluator.release.set()
            self.assertTrue(worker.stop())

    def test_a_closed_gate_holds_the_worker_until_it_reopens(self) -> None:
        mailbox = LatestFrameMailbox()
        plans = PlanStore()
        evaluator = BlockingEvaluator()  # first call blocks until released
        gate = threading.Event()
        worker = InferenceWorker(mailbox, evaluator, plans, gate=gate)
        worker.start()
        try:
            gate.clear()  # robot is executing a step
            mailbox.put(b"moving", source_timestamp=1.0)
            time.sleep(0.2)
            self.assertEqual(evaluator.sequences, [])
            gate.set()
            # A live camera keeps publishing; feed it after the reopen.  Which
            # of these frames it lands on is a race with the resync, so only
            # "something published after the reopen" is asserted.
            deadline = time.monotonic() + 1.0
            stamp = 2.0
            while not evaluator.entered.is_set() and time.monotonic() < deadline:
                mailbox.put(b"settled", source_timestamp=stamp)
                stamp += 1.0
                time.sleep(0.02)
            self.assertTrue(evaluator.entered.is_set())
            self.assertGreater(evaluator.sequences[0], 1)
        finally:
            evaluator.release.set()
            self.assertTrue(worker.stop())

    def test_reopening_the_gate_skips_frames_captured_while_it_was_closed(self) -> None:
        # A frame taken during the settle window still shows the robot
        # rolling; the worker must wait for one captured after it stopped.
        mailbox = LatestFrameMailbox()
        plans = PlanStore()
        evaluator = BlockingEvaluator()
        gate = threading.Event()
        gate.clear()
        worker = InferenceWorker(mailbox, evaluator, plans, gate=gate)
        worker.start()
        try:
            mailbox.put(b"moving-1", source_timestamp=1.0)
            mailbox.put(b"moving-2", source_timestamp=2.0)
            gate.set()
            time.sleep(0.2)
            # Nothing new has been published since the reopen, so the two
            # pre-gate frames must not be evaluated.
            self.assertEqual(evaluator.sequences, [])
            mailbox.put(b"settled", source_timestamp=3.0)
            self.assertTrue(evaluator.entered.wait(1.0))
            self.assertEqual(evaluator.sequences, [3])
        finally:
            evaluator.release.set()
            self.assertTrue(worker.stop())

    def test_the_frame_pose_reaches_the_plan_store(self) -> None:
        # The hand-eye anchor: the goal is measured from the pose the frame was
        # taken in, so the worker has to hand that pose over with the plan.
        mailbox = LatestFrameMailbox()
        plans = PlanStore()
        evaluator = BlockingEvaluator()
        worker = InferenceWorker(mailbox, evaluator, plans)
        worker.start()
        try:
            pose = Pose(x=3.0, y=4.0, yaw=0.0)
            mailbox.put(b"frame", source_timestamp=1.0, pose=pose)
            self.assertTrue(evaluator.entered.wait(1.0))
            evaluator.release.set()
            deadline = time.monotonic() + 1.0
            while plans.dispatch().reference_pose is None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(plans.dispatch().reference_pose, pose)
        finally:
            evaluator.release.set()
            self.assertTrue(worker.stop())


if __name__ == "__main__":
    unittest.main()
