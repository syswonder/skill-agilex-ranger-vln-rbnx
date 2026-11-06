"""Robonix lifecycle provider for the Ranger InternVLA-N1 ROS runtime.

This is the process Robonix starts (``python3 -m internvln_navigation.provider``).
It owns exactly one thing that Robonix cares about: a ROS 2 context, running on
a thread it can start and stop.  The HTTP call to the DualVLN model server, the
frame pipeline and the chassis guard all live inside that context and are
invisible from here.

Two capabilities are published:

    robonix/primitive/vln/driver   lifecycle (Robonix's own contract)
    robonix/primitive/vln/task     set / get / stop an instruction

``task`` keeps the expensive work off the RPC thread: an instruction is only
recorded in the shared runtime, and the ROS node picks it up on its own timer
and performs the HTTP round trip on the inference thread.  What ``set`` does do
is wait -- not by doing work itself, but by polling the runtime until the
episode it started ends.  That wait is the contract with Robonix: a plan node
must live as long as the task, or Pilot has a completed plan to replan from the
moment the task begins and will dispatch it again.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
import sys
from typing import Any, Callable

from robonix_api import Deferred, Err, Ok, Primitive


def _add_codegen_paths() -> None:
    package_root = Path(__file__).resolve().parent.parent
    for path in (
        package_root / "rbnx-build" / "codegen" / "proto_gen",
        package_root / "rbnx-build" / "codegen" / "robonix_mcp_types",
    ):
        if path.is_dir():
            sys.path.insert(0, str(path))


_add_codegen_paths()

from internvln_navigation_control_mcp import (  # noqa: E402
    VlnTask_Request,
    VlnTask_Response,
)

from .core import RuntimeConfig
from .vln_runtime import RUNTIME


provider = Primitive(id="internvln_navigation", namespace="robonix/primitive/vln")
TASK_CONTRACT = "robonix/primitive/vln/task"

_lock = threading.Lock()
_config: dict[str, Any] = {}
_deployment_metadata: dict[str, str] = {}
_active = False
_runtime_thread: threading.Thread | None = None
_runtime_stop: threading.Event | None = None
_runtime_exited: threading.Event | None = None
_runtime_errors: list[str] = []


def _default_runner(
    config: dict[str, Any],
    stop_event: threading.Event,
    ready_event: threading.Event,
    errors: list[str],
) -> None:
    from .ros_node import run_ros_runtime

    run_ros_runtime(config, stop_event, ready_event, errors)


_ros_runner: Callable[
    [dict[str, Any], threading.Event, threading.Event, list[str]], None
] = _default_runner


def _ros_runner_entry(
    config: dict[str, Any],
    stop_event: threading.Event,
    ready_event: threading.Event,
    errors: list[str],
    exited_event: threading.Event,
) -> None:
    """Run the replaceable ROS entrypoint and publish an atomic exit state."""

    global _active
    try:
        # Keep the four-argument runner interface stable for the real ROS
        # implementation and for lifecycle tests that replace it.
        _ros_runner(config, stop_event, ready_event, errors)
    finally:
        # Set this before taking the provider lock.  An activation that is
        # concurrently checking Thread.is_alive() can therefore observe that
        # the runner returned even while this finalizer waits for the lock.
        exited_event.set()
        with _lock:
            if (
                _runtime_thread is threading.current_thread()
                and _runtime_exited is exited_event
            ):
                _active = False


def _normalize_metadata(config: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("asset_manifest", "upstream_root"):
        if config.get(name) is None:
            continue
        value = str(config[name]).strip()
        if not value:
            raise ValueError(f"{name} must not be empty when provided")
        result[name] = value
    return result


@provider.on_init
def initialize(config: dict[str, Any]):
    global _config, _deployment_metadata
    if not isinstance(config, dict):
        return Err("InternVLA config must be a JSON object")
    with _lock:
        if _active or _runtime_thread is not None:
            return Err("cannot reinitialize while InternVLA is active")
    try:
        runtime = RuntimeConfig.from_mapping(config)
        metadata = _normalize_metadata(config)
    except (TypeError, ValueError) as error:
        return Err(str(error))
    parameters = runtime.as_ros_parameters()
    # Record the configured instruction so the capability's `get` operation can
    # report it before the ROS node has come up.
    RUNTIME.set_default(runtime.instruction)
    _config = parameters
    _deployment_metadata = metadata
    return Ok()


@provider.on_activate
def activate():
    global _active, _runtime_thread, _runtime_stop, _runtime_exited
    global _runtime_errors
    if not _config:
        return Err("InternVLA provider was not initialized")
    with _lock:
        # A non-None handle is an ownership reservation even before Thread.start
        # and after a timed-out stop.  Never create a second ROS publisher set
        # until the prior runtime has been observed fully exited and cleared.
        if _active or _runtime_thread is not None:
            return Deferred("InternVLA provider is already active")
        stop_event = threading.Event()
        ready_event = threading.Event()
        exited_event = threading.Event()
        errors: list[str] = []
        thread = threading.Thread(
            target=_ros_runner_entry,
            args=(
                dict(_config),
                stop_event,
                ready_event,
                errors,
                exited_event,
            ),
            name="internvln-ros",
            daemon=True,
        )
        _runtime_stop = stop_event
        _runtime_thread = thread
        _runtime_exited = exited_event
        _runtime_errors = errors
    try:
        thread.start()
    except RuntimeError as error:
        with _lock:
            if _runtime_thread is thread:
                _runtime_thread = None
                _runtime_stop = None
                _runtime_exited = None
                _runtime_errors = []
        return Err(f"InternVLA ROS runtime thread could not start: {error}")
    if not ready_event.wait(5.0):
        stop_event.set()
        thread.join(1.0)
        if not thread.is_alive():
            with _lock:
                if _runtime_thread is thread:
                    _runtime_thread = None
                    _runtime_stop = None
                    _runtime_exited = None
                    _runtime_errors = []
        return Err("InternVLA ROS runtime did not start within 5 seconds")

    failure_detail = ""
    with _lock:
        owns_runtime = (
            _runtime_thread is thread and _runtime_exited is exited_event
        )
        # Read liveness first, then the explicit exit signal.  If the runner
        # returns between these reads, its finalizer sets exited_event before
        # waiting for this lock, and activation fails instead of committing a
        # stale active=True state.
        alive = thread.is_alive()
        exited = exited_event.is_set()
        if not owns_runtime:
            failure_detail = "lifecycle changed during startup"
        elif errors:
            failure_detail = errors[-1]
        elif exited or not alive:
            failure_detail = "ROS runtime exited during startup"
        else:
            _active = True
            return Ok()

        if owns_runtime:
            _active = False

    stop_event.set()
    thread.join(1.0)
    if not thread.is_alive():
        with _lock:
            if _runtime_thread is thread:
                _runtime_thread = None
                _runtime_stop = None
                _runtime_exited = None
                _runtime_errors = []
    return Err(f"InternVLA ROS runtime failed: {failure_detail}")


def _stop_runtime() -> tuple[bool, str]:
    global _active, _runtime_thread, _runtime_stop, _runtime_exited
    global _runtime_errors
    with _lock:
        thread = _runtime_thread
        stop_event = _runtime_stop
        _active = False
    if stop_event is not None:
        stop_event.set()
    if thread is not None:
        thread.join(4.0)
        if thread.is_alive():
            return False, "InternVLA ROS runtime did not stop within 4 seconds"
    with _lock:
        if _runtime_thread is thread:
            _runtime_thread = None
            _runtime_stop = None
            _runtime_exited = None
            _runtime_errors = []
    return True, ""


@provider.on_deactivate
def deactivate():
    ok, detail = _stop_runtime()
    return Ok() if ok else Err(detail)


@provider.on_shutdown
def shutdown():
    ok, detail = _stop_runtime()
    return Ok() if ok else Err(detail)


# How often a waiting `set` re-reads the runtime.  The ROS node pumps at 10 Hz
# and the model answers about once a second, so nothing changes faster.
_AWAIT_POLL_S = 0.2


def _await_task_end(timeout_s: float) -> dict[str, Any]:
    """Block until the episode this call belongs to ends, or the bound expires.

    `set` used to answer "queued" the moment the instruction was recorded, so a
    Robonix plan node completed in the first millisecond of a task that then
    ran for minutes.  Pilot saw a completed plan, replanned, and dispatched the
    same instruction again -- the "it keeps triggering new internvln tasks"
    loop.  Waiting here makes one plan node mean one task: the node stays
    Running while the robot drives, and the result Pilot finally reads is the
    terminal state.  The bound (``set_block_timeout_s``) only exists so a
    wedged model cannot pin the call forever; hitting it reports the live
    state, never a fabricated completion.
    """

    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        status = RUNTIME.status()
        if status.get("terminal_kind") in {"finished", "stopped"}:
            return status
        if time.monotonic() >= deadline:
            return status
        time.sleep(_AWAIT_POLL_S)


@provider.mcp(TASK_CONTRACT)
def vln_task(request: VlnTask_Request) -> VlnTask_Response:
    """Set, read, or stop the navigation instruction.

    ``set`` records the instruction and then, when the runtime is bound and not
    gated, waits for that episode to end.  It is the task, so its plan node is
    the task: Pilot has no completed plan to replan from until the robot has
    actually finished, been stopped, or the wait ran out.  ``get`` never blocks
    and reports the same terminal states; ``stop`` ends the episode.
    """

    operation = str(request.operation or "").strip().lower()
    if operation in {"set", "instruction", "start"}:
        result = RUNTIME.set_instruction(request.instruction)
        status = RUNTIME.status()
        safety = status.get("safety") or {}
        blocked = bool(safety.get("blocked"))
        accepted = bool(result.get("accepted"))
        if result.get("already_finished"):
            # The upper layer re-dispatched a task it just saw finish.  Answer
            # with that task's own verdict instead of restarting it: this is
            # the other half of the "same instruction forever" loop.
            return VlnTask_Response(
                accepted=True,
                running=False,
                instruction=str(
                    status.get("finished_instruction") or request.instruction or ""
                ),
                status="finished",
                detail=str(result.get("detail", "")),
            )
        if not accepted or blocked or not bool(status.get("bound")):
            # Rejected, remembered-but-gated, or nothing is bound yet: there is
            # no episode to wait for, so answer immediately.  "held" is not a
            # failure -- the instruction starts by itself when the gate
            # reopens; "queued" would imply the model is about to be asked,
            # which is exactly what the gate is preventing.
            return VlnTask_Response(
                accepted=accepted,
                running=False,
                instruction=str(
                    status.get("pending_instruction")
                    or safety.get("held_instruction")
                    or request.instruction
                    or ""
                ),
                status=(
                    "held"
                    if accepted and blocked
                    else ("queued" if accepted else "rejected")
                ),
                detail=str(result.get("detail", "")),
            )
        status = _await_task_end(float(status.get("set_block_timeout_s") or 0.0))
        if status.get("terminal_kind") == "stopped":
            return VlnTask_Response(
                accepted=True,
                running=False,
                instruction=str(status.get("terminal_instruction") or ""),
                status="stopped",
                detail=(
                    "operator stop: the task was retired and the client "
                    "disarmed; no further model requests"
                ),
            )
        return _status_response()

    if operation in {"stop", "cancel", "halt"}:
        result = RUNTIME.stop()
        return VlnTask_Response(
            accepted=bool(result.get("accepted")),
            running=False,
            instruction="",
            status="stopping",
            detail=str(result.get("detail", "")),
        )

    if operation in {"get", "status", ""}:
        return _status_response()

    return VlnTask_Response(
        accepted=False,
        running=False,
        instruction="",
        status="rejected",
        detail=f"unknown operation {operation!r}; use set, get or stop",
    )


def _status_response() -> VlnTask_Response:
    """The `get` answer, also used as the terminal answer of a blocking `set`."""

    status = RUNTIME.status()
    client = status.get("client") or {}
    safety = status.get("safety") or {}
    blocked = bool(safety.get("blocked"))
    # `armed` is the difference between "thinking" and "nothing to think
    # about": with require_instruction the stack sits in the second state at
    # boot and sends the model no request at all.
    armed = bool(client.get("armed"))
    if status.get("finished"):
        # The model said STOP often enough to be believed and the runtime
        # has already retired the plan and disarmed the client.  Checked
        # before the gate: a finished task should read as done even if the
        # gate happens to be closed, because this is what tells the
        # executor to end the leaf rather than retry.
        applied = status.get("finished_instruction")
        return VlnTask_Response(
            accepted=True,
            running=False,
            instruction=str(applied or ""),
            status="finished",
            detail=(
                "model answered stop "
                f"{status.get('finished_stops')} times in a row over "
                f"{status.get('finished_after_s')}s (threshold "
                f"{status.get('finish_after_stops')} frames / "
                f">= {status.get('finish_after_stops_min_s')}s): task "
                "finished; plan retired and no further model requests "
                "until a new instruction. "
                f"applied={applied}, "
                f"frames={client.get('requests')}"
            ),
        )
    if blocked:
        # The gate is a local reflex: the plan has been retired and the
        # client disarmed, so the answer is "stopped", not "still working".
        # Reporting the model's last state here would make an obstacle look
        # like a slow server.
        nearest = safety.get("nearest_m")
        nearest_text = "n/a" if nearest is None else f"{float(nearest):.2f}m"
        stop = safety.get("stop_distance_m")
        stop_text = "n/a" if stop is None else f"{float(stop):.2f}m"
        return VlnTask_Response(
            accepted=True,
            running=False,
            instruction=str(safety.get("held_instruction") or ""),
            status="blocked",
            detail=(
                "execution halted by safety gate: "
                f"reason={safety.get('reason')}, nearest={nearest_text}, "
                f"stop={stop_text}, "
                f"blocked_for={safety.get('blocked_for_s')}s, "
                f"stops={safety.get('stops')}; no model requests are being "
                f"made; held={safety.get('held_instruction')}, "
                f"bound={status.get('bound')}, armed={armed}, "
                f"applied={status.get('last_applied_instruction')}"
            ),
        )
    return VlnTask_Response(
        accepted=True,
        running=bool(armed and client.get("state") == "running"),
        instruction=str(status.get("pending_instruction") or ""),
        status=(
            str(client.get("state") or "idle") if armed else "awaiting-instruction"
        ),
        detail=(
            f"bound={status.get('bound')}, "
            f"armed={armed}, "
            f"applied={status.get('last_applied_instruction')}, "
            f"episode={client.get('episode')}, "
            f"frames={client.get('requests')}, "
            f"failures={client.get('consecutive_failures')}"
        ),
    )


if __name__ == "__main__":
    provider.run()
