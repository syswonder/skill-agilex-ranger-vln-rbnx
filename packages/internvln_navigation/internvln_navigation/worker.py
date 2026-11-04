"""Latest-frame inference worker without ROS dependencies."""

from __future__ import annotations

import threading
from typing import Callable, Protocol

from .core import EncodedFrame, InferencePlan, LatestFrameMailbox, PlanStore
from .odometry import Pose


class Evaluator(Protocol):
    def evaluate(self, frame: EncodedFrame) -> InferencePlan:
        ...


class InferenceWorker:
    """Run one blocking inference at a time and always pick the newest next frame."""

    def __init__(
        self,
        mailbox: LatestFrameMailbox,
        evaluator: Evaluator,
        plans: PlanStore,
        *,
        on_plan: Callable[[EncodedFrame, InferencePlan], None] | None = None,
        on_error: Callable[[EncodedFrame, Exception], None] | None = None,
        gate: threading.Event | None = None,
        pose_source: Callable[[], Pose | None] | None = None,
    ) -> None:
        self._mailbox = mailbox
        self._evaluator = evaluator
        self._plans = plans
        self._on_plan = on_plan
        self._on_error = on_error
        # Where the answer's steps are measured from.  The reference client
        # re-reads its *current* odometry the moment an answer arrives and
        # applies the actions to that (http_internvla_client.py:313,
        # ``homo_goal = self.homo_odom.copy()``), not to the pose stamped on
        # the frame.  Measuring from the frame's pose instead puts the goal one
        # step in the past: the chassis has already covered that step while the
        # request was in flight, so the goal arrives already reached (the robot
        # brakes) and its heading is a heading from a second ago (the robot
        # steers back to it, which is the S-shaped approach).  ``None`` keeps
        # the frame's pose for callers without odometry.
        self._pose_source = pose_source
        # Lockstep gate.  When it is cleared the robot is executing a step (or
        # settling), and asking the model now would mean asking about a pose
        # the robot is no longer in by the time the answer is used.  None keeps
        # the free-running behaviour.
        self._gate = gate
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="internvln-inference",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> bool:
        self._stop_event.set()
        close = getattr(self._evaluator, "close", None)
        if callable(close):
            close()
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, float(timeout_s)))
        return not thread.is_alive()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _wait_for_gate(self) -> tuple[bool, int | None]:
        """Block while the robot is moving.  ``(False, _)`` means "stop now".

        On a reopen the second element is the newest sequence already in the
        mailbox.  The worker resumes *after* it, because frames captured
        during the settle window still show the robot rolling and the whole
        point of the gate is to send a frame taken at rest.
        """

        gate = self._gate
        if gate is None or gate.is_set():
            return True, None
        while not self._stop_event.is_set():
            if gate.wait(0.1):
                latest = self._mailbox.latest()
                return True, None if latest is None else latest.sequence
        return False, None

    def _run(self) -> None:
        last_sequence = 0
        while not self._stop_event.is_set():
            open_ok, resync = self._wait_for_gate()
            if not open_ok:
                break
            if resync is not None:
                last_sequence = resync
            frame = self._mailbox.wait_after(
                last_sequence,
                timeout_s=0.1,
                stop_event=self._stop_event,
            )
            if frame is None:
                continue
            last_sequence = frame.sequence
            try:
                plan = self._evaluator.evaluate(frame)
                # The answer's steps are measured from where the robot is *now*
                # -- that is when they are about to be carried out, and it is
                # what the reference client does.  The frame's own pose stays
                # the fallback for callers with no odometry.
                reference_pose = (
                    self._pose_source() if self._pose_source is not None else frame.pose
                )
                self._plans.update(plan, reference_pose=reference_pose)
                if self._on_plan is not None:
                    self._on_plan(frame, plan)
            except Exception as error:  # keep serving later camera frames
                if self._on_error is not None:
                    self._on_error(frame, error)
