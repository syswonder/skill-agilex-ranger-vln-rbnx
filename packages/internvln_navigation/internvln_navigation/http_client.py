"""Standard-library client for the InternVLA-N1 DualVLN real-robot server.

The server is upstream InternVLA-N1's
``scripts/realworld/http_internvla_server.py``, and it exposes exactly one
route:

    POST /eval_dual    multipart: json={"reset": bool, "instruction": str}
                       plus one JPEG part named "image"
                       -> {"discrete_action": [...]}  or
                          {"trajectory": [...], "pixel_goal": [...]}

There is no liveness route, no task registry and no client id.  The only state
on the server is the frame history System 2 samples from (``--num_history``),
and ``reset`` is the only control over it: ``true`` clears that history and
starts a fresh episode, ``false`` appends to it.  A navigation instruction is
therefore not "started" anywhere -- it is simply repeated on every frame, and
changing it means sending ``reset: true`` once.

So this client owns a one-bit state machine -- "must the next request clear the
history?" -- and turns it into the single method the inference worker already
knows how to drive:

    evaluate(frame: EncodedFrame) -> InferencePlan

That is the whole point of this class.  :mod:`internvln_navigation.worker` and
its ``Evaluator`` Protocol are unchanged from the RobotTrack bridge, so nothing
above this file can tell which model sits behind it.

Threading contract, which the worker relies on:

  * ``evaluate`` is called from the single inference-worker thread and may block
    for as long as the model takes.
  * ``set_instruction`` is called from a ROS callback thread and must never
    block.  It only records intent and bumps a generation counter; the next
    ``/eval_dual`` happens on the worker thread at the top of the next
    ``evaluate``.
  * Results belonging to a superseded generation are discarded rather than
    executed, so a stale plan can never be driven after the operator has
    already changed their mind.
"""

from __future__ import annotations

from http.client import HTTPConnection, HTTPSConnection
import json
import threading
import time
from typing import Any, Callable, Mapping, NoReturn
from urllib.parse import urlsplit
import uuid

from .action_adapter import parse_plan
from .core import (
    CONTROL_DT,
    EncodedFrame,
    InferencePlan,
    ModelUnavailableError,
    ProtocolError,
    RuntimeConfig,
    VelocityCommand,
    ZERO_COMMAND,
)


# The single documented multipart field name for the RGB frame.  Both local
# deployment documents, and the reference client `vln_query.py`, agree on
# "image"; a request carrying anything else is answered with HTTP 400.  It is
# a constant rather than a negotiated candidate because the server has exactly
# one implementation and there is nothing to negotiate with.
IMAGE_FIELD = "image"
IMAGE_FILENAME = "rgb"

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"


# (method, url, body, headers, timeout_s) -> (status_code, body_bytes)
Transport = Callable[[str, str, bytes | None, Mapping[str, str], float], tuple[int, bytes]]


def build_multipart_body(
    metadata: Mapping[str, Any],
    jpeg: bytes,
    *,
    image_field: str = IMAGE_FIELD,
    image_filename: str = IMAGE_FILENAME,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Build the DualVLN multipart body: one ``json`` text part, one JPEG part.

    The metadata travels as a *string form field* named ``json``, not as the
    body itself.  That is the server's contract and the single most common way
    to get this request wrong by hand.
    """

    payload = bytes(jpeg)
    if not payload:
        raise ValueError("jpeg must not be empty")
    if not payload.startswith(b"\xff\xd8"):
        raise ValueError("frame is not a JPEG (missing SOI marker)")
    marker = boundary or f"dualvln-{uuid.uuid4().hex}"
    if not marker or any(character in marker for character in "\r\n\""):
        raise ValueError("multipart boundary contains invalid characters")

    json_bytes = json.dumps(
        dict(metadata), separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")

    prefix = (
        f"--{marker}\r\n"
        'Content-Disposition: form-data; name="json"\r\n'
        "Content-Type: application/json; charset=utf-8\r\n\r\n"
    ).encode("ascii")
    image_header = (
        f"\r\n--{marker}\r\n"
        f'Content-Disposition: form-data; name="{image_field}"; '
        f'filename="{image_filename}"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{marker}--\r\n".encode("ascii")
    return prefix + json_bytes + image_header + payload + suffix, marker


class HttpTransport:
    """One-request transport whose active socket can be closed on deactivate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: HTTPConnection | HTTPSConnection | None = None

    def __call__(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> tuple[int, bytes]:
        parsed = urlsplit(url)
        connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=timeout_s)
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        with self._lock:
            self._active = connection
        try:
            request_headers = dict(headers)
            if body is not None:
                request_headers["Content-Length"] = str(len(body))
            connection.request(method, target, body=body, headers=request_headers)
            response = connection.getresponse()
            return int(response.status), response.read()
        finally:
            with self._lock:
                if self._active is connection:
                    self._active = None
            connection.close()

    def close(self) -> None:
        with self._lock:
            connection = self._active
        if connection is not None:
            connection.close()


class InternVLAClient:
    """DualVLN client implementing the inference worker's ``Evaluator`` seam."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or HttpTransport()
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()

        self._state = STATE_IDLE
        self._episode = 0
        self._active_instruction: str | None = None
        # The gate.  With `require_instruction` (the default) the client boots
        # with no task in force, so the first camera frames produce NO request
        # at all: the model is only ever asked about a task an operator handed
        # over.  The configured instruction is deliberately not loaded into the
        # pending slot -- it is a reference text for the status view, and
        # letting it arm the client is exactly the "nobody asked, but the robot
        # is driving" failure this prevents.
        self._require_instruction = bool(config.require_instruction)
        self._armed = not self._require_instruction
        self._pending_instruction: str = config.instruction if self._armed else ""
        # True until the first request of the current episode has carried
        # ``reset: true``.  A failed frame sets it again: after any doubt about
        # what the server holds, the safe assumption is that its history is
        # wrong and has to be cleared.
        self._reset_pending = True
        # Frames sent since the last ``reset: true`` (the first one after a
        # reset counts as 1).  :attr:`evaluate` compares this against
        # ``history_reset_frames`` to flush the model's memory mid-episode.
        self._frame_id = 0
        self._memory_flushes = 0
        self._generation = 0
        self._requests = 0
        self._retries = 0
        self._executed = VelocityCommand()
        self._last_note = ""
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._healthy = False

    # ------------------------------------------------------------ instruction

    def set_instruction(self, instruction: str) -> None:
        """Record a new operator instruction.  Never blocks.

        Called from a ROS callback thread, so the only work done here is
        storing the text and invalidating whatever the worker is doing right
        now.  The next ``/eval_dual`` -- with ``reset: true``, because the
        instruction changed -- happens on the worker thread.
        """

        wanted = str(instruction).strip()
        if not wanted or len(wanted) > 1000:
            raise ValueError("instruction must contain 1..1000 characters")
        with self._lock:
            if wanted == self._pending_instruction and self._state == STATE_RUNNING:
                return
            self._armed = True
            self._pending_instruction = wanted
            # Any response still in flight now belongs to an instruction nobody
            # wants any more; the generation bump is what discards it.
            self._generation += 1
            if self._state == STATE_RUNNING:
                self._state = STATE_IDLE
            self._reset_pending = True
            self._last_note = f"instruction queued: {wanted}"

    def clear_instruction(self, note: str = "operator stop: awaiting a new instruction") -> None:
        """Drop the task in force and go back to waiting.  Never blocks.

        Retiring the plan is not enough on its own: the very next camera frame
        would ask the model the same question again and drive off on its answer,
        which is how an "operator stop" used to last only until the next
        inference.  Unarming is what makes it stick -- from here the client sends
        nothing until :meth:`set_instruction` is called again.
        """

        with self._lock:
            self._armed = False
            self._pending_instruction = ""
            self._generation += 1
            self._state = STATE_IDLE
            self._reset_pending = True
            self._last_note = note

    def set_executed_command(self, command: VelocityCommand) -> None:
        """Record the command actually sent to the chassis.

        DualVLN has no client-velocity feedback field, unlike MiniCPM-RobotTrack,
        so this is recorded for the diagnostics stream and nothing else.  It is
        kept because the ROS node reports it on every dispatch tick.
        """

        with self._lock:
            self._executed = command.clipped(self._config.max_vx, self._config.max_wz)

    # ------------------------------------------------------------------ state

    @property
    def instruction(self) -> str:
        with self._lock:
            return self._pending_instruction

    @property
    def episode(self) -> int:
        """How many instruction episodes have been started.

        Each one begins with ``reset: true``, i.e. with the server-side history
        cleared.  Flushes that happen *inside* an episode are counted separately
        -- see :attr:`memory_flushes`.
        """

        with self._lock:
            return self._episode

    @property
    def memory_flushes(self) -> int:
        """How many times the history was flushed mid-episode (same instruction).

        These are the ``reset: true`` frames issued by ``history_reset_frames``,
        not instruction changes and not recoveries from a failed frame.
        """

        with self._lock:
            return self._memory_flushes

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state == STATE_RUNNING

    @property
    def armed(self) -> bool:
        """Is there a task in force?  False means "send nothing, hold still".

        Set by :meth:`set_instruction`, cleared by :meth:`clear_instruction`
        (an operator stop).  With ``require_instruction`` it starts False, so a
        freshly booted stack is silent until somebody asks for something.
        """

        with self._lock:
            return self._armed and bool(self._pending_instruction)

    @property
    def request_index(self) -> int:
        with self._lock:
            return self._requests

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def last_note(self) -> str:
        with self._lock:
            return self._last_note

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def snapshot(self) -> dict[str, Any]:
        """Diagnostics payload for the ROS node and the MCP capability."""

        with self._lock:
            return {
                "state": self._state,
                "episode": self._episode,
                "instruction": self._pending_instruction,
                "armed": self._armed,
                "require_instruction": self._require_instruction,
                "active_instruction": self._active_instruction,
                "frame_id": self._frame_id,
                "memory_flushes": self._memory_flushes,
                "generation": self._generation,
                "requests": self._requests,
                "retries": self._retries,
                "reset_pending": self._reset_pending,
                "consecutive_failures": self._consecutive_failures,
                "healthy": self._healthy,
                "last_note": self._last_note,
                "last_error": self._last_error,
                "executed_vx": self._executed.vx,
                "executed_wz": self._executed.wz,
            }

    # ------------------------------------------------------ failure accounting

    def _fail(self, message: str) -> NoReturn:
        """Count one failure and raise; escalate once the threshold is crossed.

        A single bad frame costs one plan, and the plan store's own expiry
        handles that.  A run of failures means the model is gone, and then the
        plan still being executed has to be retired explicitly -- which is what
        :class:`ModelUnavailableError` tells the ROS runtime to do.

        Either way the episode is over: the next frame re-arms with
        ``reset: true``, so the server can never be left planning against a
        history this client is no longer sure about.

        Reached only once the frame's retries are spent (``failure_retries``).
        A single 500 is retried on the spot and does *not* land here; otherwise
        a flaky server would clear the model's history every few frames, which
        is indistinguishable from a robot that refuses to move.
        """

        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
            self._last_error = message
            self._healthy = False
            self._state = STATE_IDLE
            self._reset_pending = True
        if failures >= self._config.fault_after_failures:
            raise ModelUnavailableError(f"{message} ({failures} consecutive failures)")
        raise ProtocolError(message)

    def _succeed(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._last_error = None
            self._healthy = True

    # ------------------------------------------------------------------- HTTP

    def _url(self, path: str) -> str:
        return f"{self._config.server_url}{path}"

    # -------------------------------------------------------------- endpoints

    def _eval_dual(self, instruction: str, reset: bool, jpeg: bytes) -> Mapping[str, Any]:
        """One ``POST /eval_dual``.  Raises; never counts a failure itself."""

        body, boundary = build_multipart_body(
            {"reset": bool(reset), "instruction": instruction},
            jpeg,
        )
        try:
            status, payload = self._transport(
                "POST",
                self._url("/eval_dual"),
                body,
                {
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Accept": "application/json",
                },
                self._config.http_timeout_s,
            )
        except Exception as error:  # noqa: BLE001
            raise ProtocolError(f"/eval_dual unreachable: {error}") from error
        if not 200 <= status < 300:
            detail = payload[:200].decode("utf-8", "replace") if payload else ""
            raise ProtocolError(f"/eval_dual returned HTTP {status} {detail}")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(f"/eval_dual returned invalid JSON: {error}") from error
        if not isinstance(decoded, Mapping):
            raise ProtocolError("/eval_dual must return a JSON object")
        return decoded

    # -------------------------------------------------------------- Evaluator

    def evaluate(self, frame: EncodedFrame) -> InferencePlan:
        """One model step.  The slow call in the whole stack lives here.

        With no task in force this returns a zero plan and sends no request at
        all -- see :attr:`armed`.
        """

        if not self.armed:
            with self._lock:
                self._last_note = (
                    "awaiting an operator instruction (no request sent)"
                    if self._require_instruction
                    else "no instruction in force; holding still"
                )
            return InferencePlan(
                command=ZERO_COMMAND,
                waypoints=None,
                control_dt=CONTROL_DT,
                velocity_source="awaiting_instruction",
            )

        with self._lock:
            generation = self._generation
            instruction = self._pending_instruction
            # The instruction is the whole episode.  There is no task handle to
            # compare against, so "same instruction, still running" is the only
            # case that continues an episode instead of clearing the history.
            restart = (
                self._state != STATE_RUNNING
                or self._active_instruction != instruction
                or self._reset_pending
            )
            # Mid-episode memory flush, off by default (``history_reset_frames``
            # is 0).  The server keeps every frame since the episode began and
            # samples ``num_history`` (8) of them evenly across that whole
            # window -- and that is how the model was trained, so the window is
            # its memory of the scene, including where it first saw the target.
            # Flushing restarts the window at the current frame; every N frames
            # it used to be, and the price was a model that forgot the target it
            # had already found.  ``restart`` already implies a reset; this only
            # fires when it does not.
            flush = (
                not restart
                and self._config.history_reset_frames > 0
                and self._frame_id >= self._config.history_reset_frames
            )
            reset = restart or flush
            if restart or flush:
                frame_id = 0
            else:
                frame_id = self._frame_id
                self._frame_id += 1
            self._requests += 1

        if restart:
            with self._lock:
                self._episode += 1
                self._active_instruction = instruction
                self._state = STATE_RUNNING
                self._reset_pending = False
                self._frame_id = 1
                self._last_note = (
                    f"episode {self._episode} reset for {instruction!r}"
                )
        # A flush's own bookkeeping waits for the answer (below): a frame whose
        # retries all failed is re-sent as a full restart anyway, and counting it
        # here would inflate ``memory_flushes`` with a reset nobody observed.

        # One frame may be submitted more than once.  The live server answers a
        # noticeable share of requests with HTTP 500, and giving up on the first
        # one used to cost the frame *and* the episode: ``_fail`` re-arms
        # ``reset: true``, so the server's eight-frame history was thrown away
        # over a hiccup, and a robot whose history keeps resetting has nothing
        # to plan against.  Same frame, same instruction, same ``reset``.
        plan: InferencePlan | None = None
        error: Exception | None = None
        retries_used = 0
        for attempt in range(1 + self._config.failure_retries):
            try:
                payload = self._eval_dual(instruction, reset, frame.jpeg)
                plan = parse_plan(payload, config=self._config)
                break
            except Exception as caught:  # noqa: BLE001 -- one lost attempt either way
                error = caught
                if attempt < self._config.failure_retries:
                    retries_used += 1
                    with self._lock:
                        self._retries += 1

        if plan is None:
            if isinstance(error, (ProtocolError, ModelUnavailableError)):
                self._fail(f"frame {frame_id}: {error}")
            # An adapter or transport bug lands here too: still one lost plan.
            self._fail(f"frame {frame_id}: unexpected {type(error).__name__}: {error}")

        with self._lock:
            superseded = self._generation != generation
            self._consecutive_failures = 0
            self._last_error = None
            self._healthy = True
            note = plan.velocity_source
            if retries_used:
                note = f"{note} (recovered on retry {retries_used})"
            if flush:
                self._memory_flushes += 1
                self._frame_id = 1
                note = (
                    f"{note} (memory flushed: "
                    f"{self._config.history_reset_frames} frames of history cleared)"
                )
            self._last_note = (
                "discarded a plan for a superseded instruction" if superseded else note
            )

        if superseded:
            # The operator changed the instruction while this request was in
            # flight.  Executing its answer would drive the robot on a task
            # nobody asked for, so hold still for one step instead; the next
            # iteration starts the new episode.
            return InferencePlan(
                command=ZERO_COMMAND,
                waypoints=None,
                control_dt=plan.control_dt,
                velocity_source="superseded",
            )
        return plan

    # --------------------------------------------------------------- teardown

    def close(self) -> None:
        """Close the socket.

        There is nothing to tell the server: it holds no task for this client,
        and the history it does hold is cleared by the next ``reset``.
        """

        with self._lock:
            self._state = STATE_DONE
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()
