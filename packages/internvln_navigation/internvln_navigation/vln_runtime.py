"""Process-global handoff between the Robonix capability and the ROS node.

An MCP capability handler runs on a Robonix RPC thread and must return
promptly; the ROS node owns the client, the plan store and the executor and
must not be touched from another thread.  This module is the one place they
meet, and it exists so neither has to know about the other:

    Robonix RPC thread  --set_instruction()-->  pending slot
    ROS timer (10 Hz)   --pump()----------->   client.set_instruction()
                                                client.clear_instruction()  (on stop)
                                                plan_store.clear()          (on stop)
    ROS dispatch (50 Hz) --set_safety_state()-> client.clear_instruction()  (gate closes)
                                                plan_store.clear()          (gate closes)

The pattern is the same one the RobotTrack bridge used for its distance
capability: ``provider.py`` and ``ros_node.py`` import this single module
object, so both see the same pending instruction without any extra plumbing.

:meth:`VlnRuntime.pump` is idempotent and cheap -- it does nothing at all when
there is no pending work -- so the ROS node can call it on a fixed timer
without checking anything first.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol

from .core import ZERO_COMMAND


class InstructionSink(Protocol):
    """The part of the client the runtime drives from the ROS thread."""

    def set_instruction(self, instruction: str) -> None: ...

    def clear_instruction(self, note: str = ...) -> None: ...


class PlanSink(Protocol):
    """The part of the plan store the runtime drives on an operator stop."""

    def clear(self) -> None: ...


class VlnRuntime:
    """Mutable, thread-safe handoff for one process."""

    def __init__(self, *, default_instruction: str = "") -> None:
        self._lock = threading.Lock()
        self._default_instruction = str(default_instruction).strip()
        self._pending_instruction: str | None = None
        self._pending_stop = False
        self._client: InstructionSink | None = None
        self._plans: PlanSink | None = None
        self._last_applied: str | None = None
        self._last_result: dict[str, Any] = {
            "accepted": False,
            "detail": "runtime not started",
        }
        self._stop_count = 0
        # Model-completion latch.  A DualVLN answer of 0 is STOP ("task
        # done"); after ``finish_after_stops`` consecutive ones the runtime
        # does the same thing an operator stop does -- retire the plan and
        # disarm the client -- so an executor polling ``get`` sees the leaf
        # end instead of a robot that answers zero forever.
        self._finish_after_stops = 20
        self._finish_after_stops_min_s = 30.0
        # A finished task stays finished when the upper layer dispatches the
        # same words again within this window (Pilot replans as soon as a plan
        # completes -- that echo used to wipe `_finished` and start the robot
        # on the same walk again).  A longer gap, a different instruction, or
        # an operator stop starts a new task.
        self._finish_reissue_grace_s = 10.0
        # How long the capability's `set` may block waiting for the episode to
        # end.  0 = never block.
        self._set_block_timeout_s = 900.0
        self._consecutive_stops = 0
        self._stop_run_started_at: float | None = None
        self._pending_finish = False
        self._finished = False
        self._finished_instruction: str | None = None
        self._finished_at: float | None = None
        # The last episode's ending, readable by a blocked `set` call:
        # "finished" (the model said STOP) or "stopped" (operator).  Idle,
        # pending and running episodes have no terminal kind.
        self._terminal_kind: str | None = None
        self._terminal_at: float | None = None
        self._terminal_instruction: str | None = None
        # What the run looked like when it finished, for the capability's
        # report.  Kept because the live counters are reset at that point.
        self._finished_stops = 0
        self._finished_after_s = 0.0
        # Proximity gate mirror.  The ROS node owns the real gate; this copy is
        # what makes a closed gate visible to the capability and what holds a
        # new instruction back instead of letting it re-arm the model loop.
        self._safety_blocked = False
        self._safety_reason = ""
        self._safety_nearest_m: float | None = None
        self._safety_stop_distance_m: float | None = None
        self._safety_since: float | None = None
        self._safety_stops = 0
        self._held_instruction: str | None = None

    # ------------------------------------------------------------------ wiring

    def bind(
        self,
        *,
        client: InstructionSink | None = None,
        plans: PlanSink | None = None,
    ) -> None:
        """Attach the ROS-side objects.  Called once, from the ROS node."""

        with self._lock:
            if client is not None:
                self._client = client
            if plans is not None:
                self._plans = plans

    def unbind(self) -> None:
        with self._lock:
            self._client = None
            self._plans = None
            # The completion latch belonged to the node that is going away: a
            # restarted stack must not report a task it never ran as finished.
            self._pending_instruction = None
            self._pending_stop = False
            self._pending_finish = False
            self._consecutive_stops = 0
            self._stop_run_started_at = None
            self._finished = False
            self._finished_instruction = None
            self._finished_stops = 0
            self._finished_after_s = 0.0
        # The gate died with the node; keeping its last verdict would make a
        # restarted stack report "blocked" until the first new depth frame.
        self.reset_safety()

    @property
    def bound(self) -> bool:
        with self._lock:
            return self._client is not None

    @property
    def default_instruction(self) -> str:
        return self._default_instruction

    def set_default(self, instruction: str) -> None:
        """Record the configured start-up instruction for the status view.

        Nothing is queued here.  With ``require_instruction`` (the default) the
        client boots with no task in force, so this text is a *reference* for
        the status report and the capability's ``get`` -- it is never sent to
        the model until an operator issues it.
        """

        with self._lock:
            self._default_instruction = str(instruction).strip()

    def configure(
        self,
        *,
        finish_after_stops: int | None = None,
        finish_after_stops_min_s: float | None = None,
        finish_reissue_grace_s: float | None = None,
        set_block_timeout_s: float | None = None,
    ) -> None:
        """Apply the deployment's completion thresholds (called by the node)."""

        with self._lock:
            if finish_after_stops is not None:
                self._finish_after_stops = max(1, int(finish_after_stops))
            if finish_after_stops_min_s is not None:
                self._finish_after_stops_min_s = max(0.0, float(finish_after_stops_min_s))
            if finish_reissue_grace_s is not None:
                self._finish_reissue_grace_s = max(0.0, float(finish_reissue_grace_s))
            if set_block_timeout_s is not None:
                self._set_block_timeout_s = max(0.0, float(set_block_timeout_s))

    def note_plan(self, plan: Any) -> None:
        """Count consecutive model STOP answers.

        Called from the inference thread once a plan has been installed, so it
        only updates counters under the lock; the side effects (disarm, retire
        the plan) are carried out by :meth:`pump` on the ROS thread, exactly
        like an operator stop.  A non-STOP answer resets the run: a model that
        stops once and then keeps driving has not finished anything.
        """

        with self._lock:
            if self._finished or self._pending_finish:
                return
            if not bool(getattr(plan, "model_stop", False)):
                self._consecutive_stops = 0
                self._stop_run_started_at = None
                return

            now = time.monotonic()
            if self._stop_run_started_at is None:
                self._stop_run_started_at = now
            self._consecutive_stops += 1
            stuck_for = now - self._stop_run_started_at
            # Both halves are required.  A few zeros can happen at any point
            # (a bad frame, a goal that has just left the view); finishing on
            # those would end tasks the model would have driven out of.  Only
            # a long, unbroken run of STOP is the "answers zero forever"
            # loop this is here to end.
            if (
                self._consecutive_stops >= self._finish_after_stops
                and stuck_for >= self._finish_after_stops_min_s
            ):
                self._pending_finish = True
                self._last_result = {
                    "accepted": True,
                    "detail": (
                        "model returned stop "
                        f"{self._consecutive_stops} times in a row over "
                        f"{stuck_for:.1f}s (>= {self._finish_after_stops} and "
                        f">= {self._finish_after_stops_min_s:.0f}s): task "
                        "finished; plan retired and no further model requests"
                    ),
                }

    # ------------------------------------------------- operator-facing commands

    def set_instruction(self, instruction: str) -> dict[str, Any]:
        """Queue an instruction.  Called from the Robonix RPC thread."""

        wanted = str(instruction).strip()
        if not wanted or len(wanted) > 1000:
            result = {
                "accepted": False,
                "detail": "instruction must contain 1..1000 characters",
            }
        else:
            with self._lock:
                now = time.monotonic()
                client = self._client
                client_state = (
                    client.snapshot()
                    if client is not None and hasattr(client, "snapshot")
                    else {}
                )
                # Pilot replans the instant a plan completes, so the task it
                # just saw end is dispatched again with the same words.  That
                # is an echo of a finished task, not new work: acknowledging
                # it as `finished` leaves the latch alone, which is what stops
                # the robot from being walked into the same STOP loop forever.
                if (
                    self._finished
                    and self._finished_instruction == wanted
                    and self._finished_at is not None
                    and now - self._finished_at <= self._finish_reissue_grace_s
                ):
                    self._last_result = {
                        "accepted": True,
                        "already_finished": True,
                        "detail": (
                            "instruction already finished: the model answered "
                            "stop and the task was retired; not restarting it. "
                            "Send a different instruction, or stop and then "
                            "set this one, to run it again"
                        ),
                    }
                    result = dict(self._last_result)
                elif (
                    not self._finished
                    # A queued operator stop ends this episode; a set that
                    # arrives in that window is a new task, not a duplicate.
                    and not self._pending_stop
                    and self._pending_instruction is None
                    and self._last_applied == wanted
                    and bool(client_state.get("armed"))
                ):
                    # The same task is still running: a second set must not
                    # start a second episode (two writers to one plan store),
                    # it waits for the one that is already going.
                    self._last_result = {
                        "accepted": True,
                        "already_running": True,
                        "detail": (
                            "instruction already running: a second set does not "
                            "restart it; this call waits for the same episode"
                        ),
                    }
                    result = dict(self._last_result)
                else:
                    # A new instruction starts a new task: whatever the previous
                    # one finished or was about to finish no longer applies.
                    self._finished = False
                    self._finished_instruction = None
                    self._finished_at = None
                    self._finished_stops = 0
                    self._finished_after_s = 0.0
                    self._consecutive_stops = 0
                    self._stop_run_started_at = None
                    self._pending_finish = False
                    self._terminal_kind = None
                    self._terminal_at = None
                    self._terminal_instruction = None
                    if self._safety_blocked:
                        # The gate is closed: remember it, but do not arm the
                        # client.  pump() applies it once the gate reopens.
                        self._held_instruction = wanted
                        self._last_result = {
                            "accepted": True,
                            "detail": (
                                "instruction held: safety gate blocked "
                                f"({self._safety_reason}); it starts when the "
                                "gate reopens"
                            ),
                        }
                    else:
                        self._pending_instruction = wanted
                        self._last_result = {
                            "accepted": True,
                            "detail": f"instruction queued: {wanted}",
                        }
                    result = dict(self._last_result)
        self._record(result)
        return result

    def stop(self) -> dict[str, Any]:
        """Queue an operator stop.  Called from the Robonix RPC thread.

        Sets a flag rather than reaching into the plan store directly, because
        the store is owned by the ROS node's executor thread.  On the ROS side
        this retires the plan AND takes the task away from the client, so the
        robot holds still and the model is not asked again: a stop that only
        retired the plan would be undone by the very next frame's answer.
        """

        with self._lock:
            self._pending_stop = True
            self._pending_instruction = None
            # A stop supersedes anything the gate was holding for later, and
            # clears a completion the model was about to report.
            self._held_instruction = None
            self._pending_finish = False
            self._consecutive_stops = 0
            self._stop_run_started_at = None
            self._finished = False
            self._finished_instruction = None
            self._finished_at = None
            self._finished_stops = 0
            self._finished_after_s = 0.0
            # Terminal for anyone blocked waiting on this episode: a stop is
            # the end of the task whether or not the model ever said so.
            self._terminal_kind = "stopped"
            self._terminal_at = time.monotonic()
            self._terminal_instruction = self._last_applied
            self._stop_count += 1
            result = {"accepted": True, "detail": "stop queued"}
        self._record(result)
        return result

    def status(self) -> dict[str, Any]:
        """Read-only view for the capability's ``get`` operation."""

        with self._lock:
            since = self._safety_since
            return {
                "bound": self._client is not None,
                "default_instruction": self._default_instruction,
                "pending_instruction": self._pending_instruction,
                "last_applied_instruction": self._last_applied,
                "pending_stop": self._pending_stop,
                "stop_count": self._stop_count,
                "finished": self._finished,
                "finished_instruction": self._finished_instruction,
                "finished_at": self._finished_at,
                "terminal_kind": self._terminal_kind,
                "terminal_at": self._terminal_at,
                "terminal_instruction": self._terminal_instruction,
                "finish_reissue_grace_s": self._finish_reissue_grace_s,
                "set_block_timeout_s": self._set_block_timeout_s,
                "finished_stops": self._finished_stops,
                "finished_after_s": round(self._finished_after_s, 1),
                "consecutive_stops": self._consecutive_stops,
                "stop_run_for_s": (
                    None
                    if self._stop_run_started_at is None
                    else round(time.monotonic() - self._stop_run_started_at, 1)
                ),
                "finish_after_stops": self._finish_after_stops,
                "finish_after_stops_min_s": self._finish_after_stops_min_s,
                "safety": {
                    "blocked": self._safety_blocked,
                    "reason": self._safety_reason,
                    "nearest_m": self._safety_nearest_m,
                    "stop_distance_m": self._safety_stop_distance_m,
                    "blocked_for_s": (
                        None if since is None else round(time.monotonic() - since, 3)
                    ),
                    "stops": self._safety_stops,
                    "held_instruction": self._held_instruction,
                },
                "last_result": dict(self._last_result),
                "client": self._client.snapshot() if hasattr(self._client, "snapshot") else None,
            }

    # ------------------------------------------------------------- safety gate

    def set_safety_state(
        self,
        *,
        blocked: bool,
        reason: str,
        nearest_m: float | None = None,
        stop_distance_m: float | None = None,
    ) -> dict[str, Any] | None:
        """Mirror the proximity gate; carry out the stop when it closes.

        Called from the ROS dispatch thread on every tick, so it must be cheap
        and idempotent: it only acts when ``blocked`` changes.

        On the closing edge it does the two things a stop needs -- retire the
        plan and disarm the client -- for the same reason an operator stop does:
        zeroing the velocity alone would last exactly until the next model
        answer.  Disarming is also what stops the worker from asking the model
        again and again while the robot is held in front of an obstacle.

        On the opening edge it does *not* arm the client by itself; the plan
        store is empty, so the robot stays still until the next model answer.
        An instruction that arrived while the gate was closed is released here
        and applied by the next :meth:`pump`.

        Returns ``None`` when nothing changed, otherwise a small action record
        for the node to log.
        """

        with self._lock:
            changed = bool(blocked) != self._safety_blocked
            self._safety_reason = str(reason)
            self._safety_nearest_m = nearest_m
            if stop_distance_m is not None:
                self._safety_stop_distance_m = float(stop_distance_m)
            if not changed:
                return None

            self._safety_blocked = bool(blocked)
            client = self._client
            plans = self._plans
            nearest = "n/a" if nearest_m is None else f"{nearest_m:.2f}m"
            if blocked:
                self._safety_stops += 1
                self._safety_since = time.monotonic()
                # An instruction queued but not yet applied is not lost: it is
                # held, then released when the gate reopens.
                if self._pending_instruction is not None:
                    self._held_instruction = self._pending_instruction
                    self._pending_instruction = None
                detail = (
                    "execution halted by safety gate: "
                    f"reason={reason}, nearest={nearest}; plan retired and no "
                    "further model requests until the gate reopens"
                )
                operation = "safety-stop"
            else:
                self._safety_since = None
                resumed = self._held_instruction
                self._held_instruction = None
                if resumed is not None:
                    self._pending_instruction = resumed
                detail = "safety gate reopened"
                if resumed is not None:
                    detail += f"; held instruction released: {resumed}"
                operation = "safety-clear"
            self._last_result = {"accepted": True, "detail": detail}

        # Side effects outside the lock: the client and the plan store have
        # their own locks and never call back into this runtime.
        if blocked:
            if client is not None:
                client.clear_instruction(f"safety gate closed: {reason}")
            if plans is not None:
                plans.clear()
        return {"operation": operation, "detail": detail}

    def reset_safety(self) -> None:
        """Forget the gate state without touching the client or the plan store."""

        with self._lock:
            self._safety_blocked = False
            self._safety_reason = ""
            self._safety_nearest_m = None
            self._safety_stop_distance_m = None
            self._safety_since = None
            self._safety_stops = 0
            self._held_instruction = None

    # ------------------------------------------------------- ROS-thread driver

    def pump(self) -> dict[str, Any] | None:
        """Apply any pending operator action.  Call from the ROS timer.

        Returns ``None`` when there was nothing to do, which is the common case.
        """

        with self._lock:
            stopping = self._pending_stop
            finishing = self._pending_finish
            instruction = self._pending_instruction
            blocked = self._safety_blocked
            safety_reason = self._safety_reason
            client = self._client
            plans = self._plans
            self._pending_stop = False
            self._pending_finish = False
            self._pending_instruction = None

        if not stopping and instruction is None and not finishing:
            return None

        if not stopping and instruction is not None and blocked:
            # The gate closed between set_instruction() and this tick.  Arming
            # the client now would restart the model loop the gate just
            # stopped, so hold the instruction instead.
            with self._lock:
                self._held_instruction = instruction
            detail = (
                f"instruction held: safety gate blocked ({safety_reason})"
            )
            self._record({"accepted": True, "detail": detail})
            return {"operation": "hold", "instruction": instruction}

        if client is None:
            # Nothing is wired yet; put the work back so it is not lost while
            # the runtime is still starting up.
            with self._lock:
                if instruction is not None:
                    self._pending_instruction = instruction
                if stopping:
                    self._pending_stop = True
                if finishing:
                    self._pending_finish = True
            return None

        if stopping:
            # Both halves matter: the plan is retired so nothing keeps driving,
            # and the client is disarmed so no later frame re-asks the model and
            # drives off on the answer.
            client.clear_instruction("plan retired by operator stop")
            if plans is not None:
                plans.clear()
            self._record({"accepted": True, "detail": "plan retired by operator stop"})
            return {"operation": "stop", "detail": "plan retired"}

        if instruction is None:
            # Model completion.  Same two halves as an operator stop: retire
            # the plan so the zero is not merely the last plan, and disarm the
            # client so the next frame cannot re-ask the model and drive off
            # on the answer.  The capability then reports status="finished"
            # (running=False), which is what lets the executor end the leaf.
            client.clear_instruction("model returned stop: task complete")
            if plans is not None:
                plans.clear()
            now = time.monotonic()
            with self._lock:
                self._finished = True
                self._finished_instruction = self._last_applied
                self._finished_at = now
                self._finished_stops = self._consecutive_stops
                started = self._stop_run_started_at
                self._finished_after_s = 0.0 if started is None else now - started
                self._consecutive_stops = 0
                self._stop_run_started_at = None
                # Release a capability call that is waiting for this episode.
                self._terminal_kind = "finished"
                self._terminal_at = now
                self._terminal_instruction = self._finished_instruction
            detail = (
                "model returned stop: task finished; plan retired and no "
                "further model requests until a new instruction"
            )
            self._record({"accepted": True, "detail": detail})
            return {"operation": "finish", "detail": detail}

        client.set_instruction(instruction)
        with self._lock:
            self._last_applied = instruction
        self._record({"accepted": True, "detail": f"instruction applied: {instruction}"})
        return {"operation": "set", "instruction": instruction}

    # --------------------------------------------------------------- internals

    def _record(self, result: dict[str, Any]) -> None:
        with self._lock:
            self._last_result = dict(result)


# One shared instance per process.  ``provider.py`` imports it to satisfy the
# MCP capability; ``ros_node.py`` imports the same object to bind and pump it.
RUNTIME = VlnRuntime()


__all__ = ["RUNTIME", "InstructionSink", "PlanSink", "VlnRuntime", "ZERO_COMMAND"]
