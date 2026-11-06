"""ROS-independent core for the InternVLA-N1 DualVLN navigation bridge.

Only protocol-agnostic pieces live here: the normalized runtime configuration,
the one-slot latest-frame mailbox, the expiring plan store and the shared
command/plan value types.

The InternVLA HTTP protocol itself is in :mod:`internvln_navigation.http_client`
and its response-to-velocity conversion in :mod:`internvln_navigation.action_adapter`.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlparse

from .odometry import (
    Pose,
    advance_pose,
    distance_reached,
    turn_reached,
)


CONTROL_DT = 0.1
MAX_VX = 0.15
MAX_WZ = 0.30
# The package default stays at the value the Ranger chassis contract was
# validated against.  Explicit live profiles may request a stronger forward
# command, but never exceed the already-established downstream guard ceiling.
MAX_CONFIGURED_VX = 0.50
MAX_CONFIGURED_WZ = 0.40
MAX_PLAN_AGE_S = 1.5
# A plan may be held longer than one model step when the inference server is
# slower than the step horizon.  It stays bounded because the plan is open
# loop: while it is held, nothing but the chassis guard is watching the robot.
MAX_PLAN_AGE_LIMIT_S = 10.0
DEFAULT_DISPATCH_HZ = 50.0
# Lockstep (stop-and-go) execution.  A DualVLN plan is computed from a frame
# that keeps getting older while the robot drives it; the measured answer
# cadence on this deployment is ~0.78 s while the plan lifetime is 1.5 s, so
# free-running execution always commands a motion that belongs to the past.
# In lockstep the robot moves for exactly one step, stops, lets the chassis
# settle, and only then is the next frame captured and sent.  ``action_step_s``
# replaces ``plan_hold_s`` as the divisor that turns a discrete step into a
# velocity; the plan's own expiry stays the watchdog.
DEFAULT_ACTION_STEP_S = 1.0
DEFAULT_STEP_SETTLE_S = 0.5
# Odometry feedback: end a step when the chassis has actually turned the angle /
# covered the distance, instead of when its nominal duration has elapsed.  Both
# reference deployments are closed loop (the Go2 client PIDs against an
# odometry-referenced goal pose, the deployment document's client blocks on a
# chassis move), and open loop is what leaves the residual pose error that
# accumulates over a task.  ``DEFAULT_ODOM_TIMEOUT_S`` is how old the newest
# ``/odom`` sample may be before the controller gives up on feedback for that
# tick and falls back to the timer; ``DEFAULT_ODOM_SEGMENT_TIMEOUT_FACTOR``
# bounds how long a segment may keep running while the goal is not reported
# reached, so a stalled measurement can never hang a step.
DEFAULT_ODOM_TOPIC = "/odom"
DEFAULT_ODOM_TIMEOUT_S = 0.5
DEFAULT_ODOM_TURN_TOLERANCE_DEG = 1.5
DEFAULT_ODOM_DISTANCE_TOLERANCE_M = 0.02
DEFAULT_ODOM_SEGMENT_TIMEOUT_FACTOR = 2.0
# What the robot sends as "the camera sees this".  ``full_frame`` is the
# default: the server's ``--resize_w 384 --resize_h 384`` is documented as a
# plain resize of whatever arrives, and every client we can read (the
# deployment document's own client, the Go2 skill) sends the frame untouched.
# ``center_crop_height`` is the older local 384x384 centre crop.
MODEL_INPUT_MODE = "full_frame"
MODEL_INPUT_MODE_CENTER_CROP = "center_crop_height"
VALID_MODEL_INPUT_MODES = (MODEL_INPUT_MODE, MODEL_INPUT_MODE_CENTER_CROP)
MODEL_CROP_SIZE = 384

SOURCE_NAVIGATION = "navigation"
SOURCE_VLN = "vln"
VALID_SOURCES = (SOURCE_NAVIGATION, SOURCE_VLN)

# Payloads the ROS instruction topic treats as "stop" instead of as a goal.
# An operator holding only `ros2 topic pub` has no other way to halt the robot:
# publishing zeros on /cmd_vel_nav would be overwritten by the next dispatch
# tick, and /cmd_vel belongs to the chassis guard.  "stop" is not a usable
# visual-navigation goal, so reserving it costs nothing.
STOP_PAYLOADS = ("stop", "/stop", "__stop__")

# --------------------------------------------------------------------- guard
# Ranger chassis adaptation.  Everything this guard re-implements used to live
# inside go2_chassis, which the Ranger deployment does not have:
#
#   * the fail-closed motion gate (allow_motion/safety_ack/allowed_modes),
#   * the 0.25 m/s and 0.40 rad/s chassis clamps,
#   * the 0.25 s command timeout and the 0.50 s zero preamble,
#   * a Go2 sport-mode service that translates and rotates simultaneously.
#
# RangerROSMessenger::TwistCmdCallback replaces all of that with behaviour the
# upstream controller does not expect: it discards linear.x entirely and pivots
# in place whenever |linear.x| / |angular.z| drops below its min_turn_radius,
# and it switches the whole 4WS steering geometry the moment linear.y is
# non-zero.  The guard restores the Go2 contract on top of that.
#
# The full command chain:
#   internvla -> source mux -> /cmd_vel_nav
#             -> THIS GUARD  -> /cmd_vel -> ranger_chassis -> CAN
# A deployment that also runs the Nav2 service inserts its velocity smoother
# and velocity guard between the mux and this guard; that only changes
# guard_input_topic, never the chain's shape.
GUARD_INPUT_TOPIC = "/cmd_vel_nav"
GUARD_OUTPUT_TOPIC = "/cmd_vel"
GUARD_TURN_POLICIES = ("clamp_yaw", "allow_spin", "block")
# The vendored ranger_ros2 inside the Ranger chassis primitive reuses
# RangerMiniV2Params for robot_model=ranger_mini_v3, whose min_turn_radius is
# 0.4764 m.  The published Ranger Mini 3.0 manual states an 0.81 m Ackermann
# turning radius.  The larger value is used here: clamping yaw for the stricter
# radius guarantees a command can never land in the chassis's
# `radius < min_turn_radius` branch, which silently zeroes linear.x.
GUARD_MIN_TURN_RADIUS_M = 0.81


class ProtocolError(ValueError):
    """The inference server returned a response outside the official contract."""


class ModelUnavailableError(RuntimeError):
    """The inference server is unreachable or has failed repeatedly.

    Deliberately distinct from :class:`ProtocolError`.  One bad response costs
    one plan and the plan store's own expiry handles it; a *sustained* outage
    must also retire the plan that is still being executed, and only the ROS
    runtime can do that.
    """


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _absolute_topic(value: Any, name: str, default: str) -> str:
    topic = str(value or default)
    if (
        not topic.startswith("/")
        or topic == "/"
        or topic.endswith("/")
        or "//" in topic
        or any(character.isspace() for character in topic)
        or len(topic) > 255
    ):
        raise ValueError(f"{name} must be one normalized absolute ROS topic")
    return topic


def _positive_float(value: Any, name: str, default: float, maximum: float) -> float:
    result = _finite_float(default if value is None else value, name)
    if not 0.0 < result <= maximum:
        raise ValueError(f"{name} must be in (0, {maximum}]")
    return result


def _bounded_float(
    value: Any,
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
    include_minimum: bool = True,
) -> float:
    result = _finite_float(default if value is None else value, name)
    minimum_ok = result >= minimum if include_minimum else result > minimum
    if not minimum_ok or result > maximum:
        bracket = "[" if include_minimum else "("
        raise ValueError(f"{name} must be in {bracket}{minimum}, {maximum}]")
    return result


def _bounded_int(
    value: Any,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    result = int(default if value is None else value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return result


def _explicit_bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a bool")


def _server_base_url(value: Any, default: str) -> str:
    """Normalize the InternVLA server *base* URL.

    Unlike the single-endpoint MiniCPM-RobotTrack bridge, this is a base: the
    client appends ``/eval_dual`` -- the server's only route -- so a value that
    already carries it would be requested as ``/eval_dual/eval_dual``.
    Credentials are rejected outright because the Robonix manifest is plain YAML
    and every request would otherwise carry them into logs.
    """

    url = str(value or default).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("server_url must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("server_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("server_url must not carry a query or fragment")
    return url


@dataclass(frozen=True)
class RuntimeConfig:
    """Normalized runtime settings shared by ROS and the Robonix provider."""

    mode: str = "dry-run"
    rgb_topic: str = "/camera/color/image_raw"
    command_topic: str = "/ranger/vln/cmd_vel_raw"
    # Secondary instruction ingress, so the chain can be exercised with
    # `ros2 topic pub` on the bench without the whole Robonix stack running.
    # The Robonix capability writes to the same runtime slot.
    instruction_topic: str = "/ranger/vln/instruction"
    status_topic: str = "/ranger/vln/status"
    server_url: str = "http://127.0.0.1:5801"
    # Fallback only -- both config layers above set this explicitly.  Keep it
    # goal-shaped: DualVLN stops when it thinks the goal is reached.
    instruction: str = "Go to the chair and stop."
    # Boot IDLE and ask the model nothing until an operator hands over a task
    # (Robonix `vln/task set`, `rbnx chat`, or /ranger/vln/instruction).  Asking
    # a stateless, greedy DualVLN the same question frame after frame is what
    # produced the "infinite 0" episode, and with nobody's task to ask about the
    # honest thing to do is send no request at all and hold still.  `instruction`
    # above is then the text recorded for the status view, NOT something the
    # model is ever asked on its own.  False restores the old behaviour: arm the
    # configured instruction at boot and start asking on the first camera frame.
    require_instruction: bool = True
    model_input_mode: str = MODEL_INPUT_MODE
    model_crop_size: int = MODEL_CROP_SIZE
    jpeg_quality: int = 85
    http_timeout_s: float = 60.0
    dispatch_hz: float = DEFAULT_DISPATCH_HZ
    # The watchdog horizon: a plan stops commanding motion this long after its
    # last update.  It is deliberately *not* the step duration any more --
    # that is ``action_step_s``, so a slow model can no longer stretch or
    # shrink the distance the robot drives per decision.
    plan_hold_s: float = MAX_PLAN_AGE_S
    # Stop-and-go (lockstep) segment execution -- the fallback path, and the
    # behaviour of this stack before the servo existed.  When it is on and
    # ``servo_enabled`` is off, a plan is executed for as long as its own
    # program says (one segment per model action), then zero is published until
    # the next plan arrives, and the inference worker is gated so it does not
    # even take a new frame until the robot has settled.
    #
    # It is off by default because standing still to think is exactly the
    # stutter it causes: at a ~1 s answer cadence a step+settle cycle spends
    # more than half its time at zero.  The frame is instead timestamped with
    # the pose it was taken in (see ``servo_enabled``), which keeps the
    # hand-eye link without the pause.
    #
    # ``action_step_s`` is the nominal duration: a segment that has to run
    # below its nominal speed because of a cap takes proportionally longer.
    lockstep_enabled: bool = False
    action_step_s: float = DEFAULT_ACTION_STEP_S
    step_settle_s: float = DEFAULT_STEP_SETTLE_S
    max_vx: float = MAX_VX
    max_wz: float = MAX_WZ
    # Closed-loop steps.  When ``/odom`` is arriving, a segment ends as soon as
    # the chassis has turned its angle / covered its distance (measured from
    # the pose the segment started in), and only falls back to the nominal
    # duration if that measurement never arrives.  This is what the reference
    # deployments do; open loop leaves a residual error per step that
    # accumulates into "stopped short of the target" and "drove past it".
    # ``odom_feedback_enabled: false`` restores the old pure-timing behaviour
    # exactly -- that is the rollback switch, no code change needed.
    odom_feedback_enabled: bool = True
    odom_topic: str = DEFAULT_ODOM_TOPIC
    odom_timeout_s: float = DEFAULT_ODOM_TIMEOUT_S
    odom_turn_tolerance_deg: float = DEFAULT_ODOM_TURN_TOLERANCE_DEG
    odom_distance_tolerance_m: float = DEFAULT_ODOM_DISTANCE_TOLERANCE_M
    # A segment that has not reported "reached" after this many times its
    # nominal duration stops anyway.  Without it a chassis that cannot turn
    # (or an odometry frame that lies) would hold one segment forever.
    odom_segment_timeout_factor: float = DEFAULT_ODOM_SEGMENT_TIMEOUT_FACTOR
    # Continuous drive (the reference client's architecture).  Each answer
    # becomes one *goal pose* -- the pose the camera was in when the frame was
    # taken, advanced by the model's actions -- and the 50 Hz dispatch tick
    # servos toward it with a proportional controller.  The inference worker is
    # not gated, so a request is always in flight and the robot never stops to
    # wait for one: a new answer only moves the goal post.
    #
    # The steps are measured from the pose the answer lands on -- that is when
    # they are about to be carried out, and it is what the reference client
    # does (``homo_goal = self.homo_odom.copy()`` in
    # http_internvla_client.py:313).  Measuring from the pose stamped on the
    # frame instead looks more honest and works worse: the goal is then one
    # step in the past, already covered by the chassis while the request was in
    # flight, so it arrives reached (the servo brakes and waits out the rest of
    # the cycle) and its heading is a heading from a second ago (the servo
    # steers back to it, which is the S-shaped approach).  A trajectory answer
    # lands on the same pose by the same rule (``goal_from_waypoints``); the
    # frame's pose is still stamped and carried as the record of what the model
    # was looking at, not as the measurement origin.
    #
    # ``servo_enabled: false`` restores segment execution (timed, or odom-ended
    # per segment) and re-arms the lockstep gate if it is on.
    servo_enabled: bool = True
    # The reference's PID gains (Kp_trans=2.0, Kp_yaw=1.5 in
    # ``controllers.py``, with its derivative terms at zero).  Its speed caps
    # are 0.6/0.5 m/s; ours are the Ranger's 0.25/0.30, so the same gains
    # saturated almost everywhere and the controller was effectively
    # bang-bang: any offset over 12.5 cm meant full throttle, any heading
    # error over 11 deg meant full steering.  At 2.0/1.5 with a one-metre
    # servo goal the robot never left full throttle until the goal was
    # reached -- the "it drives like someone who knows the way but cannot
    # drive" behaviour.  Lower gains make the approach proportional over the
    # distances this chassis actually uses (0.35 m advances, 0.25 m steps).
    servo_kp_trans: float = 1.2
    servo_kp_yaw: float = 1.0
    # A goal is "reached" inside this box: publish zero and wait for the next
    # answer.  Looser than ``odom_distance_tolerance_m`` on purpose -- that one
    # decides whether a *step* was carried out, this one decides whether a
    # velocity loop may stop chasing a point.
    servo_position_tolerance_m: float = 0.05
    servo_yaw_tolerance_deg: float = 3.0
    # Action adapter: the physical size of one model step.
    discrete_forward_m: float = 0.25
    discrete_step_deg: float = 15.0
    # How many of the model's actions one answer may commit to.  The model is
    # trained on four-action chunks and both reference deployments execute all
    # four before looking again.  This stack used to truncate to one because it
    # had no closed loop per step (a step was blind travel for its whole
    # duration); with ``odom_feedback_enabled`` each element now ends when it
    # is actually achieved, so the chunk can be executed as the model asked.
    #
    # Was 1 until 2026-10-30.  The live capture showed the model answering
    # ``discrete_action[3, 3, 3, 3]`` (four turns, ~60 deg) while the robot
    # turned 15 deg, re-photographed, and got the same answer again -- the
    # chunk is the model's committed decision, and cutting it to one element
    # turns "turn to face it" into a long staircase of small corrections.
    #
    # 2026-11-04: 4 -> 2.  Four forward steps is a committed metre between two
    # photographs, and with the live model answering ``[1,1,1,1]`` repeatedly
    # that is what "it overshoots the target it can see" looked like.  Two
    # steps (0.5 m / 30 deg) keeps the chunk semantics while halving the blind
    # commitment; the next answer is a fresh picture either way.
    max_chunk_actions: int = 2
    # How many frames of one instruction the client may send before flushing
    # the server's frame history (``reset: true`` on the next frame, same
    # instruction).  0 -- the default -- never flushes, which is what the
    # official client does: ``policy_init`` is true only for its first request
    # and nothing resets afterwards.
    #
    # The server samples its ``num_history`` context images evenly over *every*
    # frame since the episode began (``np.linspace(0, episode_idx - 1, 8)`` in
    # ``internvla_n1_agent_realworld.py``), and that is how the model was
    # trained (``internvla_n1_lerobot_dataset.py`` builds the same linspace from
    # frame 0).  So that history is the model's memory of the scene, and of
    # where it first saw what it is driving towards.  Flushing it every 8 frames
    # -- what this knob did until 2026-10-30 -- left the model with no record of
    # a target that had left the frame: it could not hold on to it, and could
    # not tell that it had already driven past it.
    history_reset_frames: int = 0
    # How far along the model's path the trajectory controller aims.  The
    # commanded yaw is pure pursuit, 2 v sin(bearing) / L, with L the distance
    # to the point this picks; the implied radius is L / (2 sin(bearing)), the
    # circle that point lies on.  A longer lookahead reads the same curve from
    # further away, so this is mostly a smoothness/reach knob: it sets how far
    # ahead the curvature is measured, not a steering gain.
    #
    # It is also the reach the servo needs to stay ahead of the answer cadence.
    # Measured live on 2026-10-30: answers land every 1 s (median) to 3 s (p90),
    # and at 0.15-0.25 m/s the robot covers 0.15-0.75 m in that time.  With
    # 0.30 m the goal was already behind the robot when the *next* answer
    # arrived 45% of the time (62% of the time it was within 8 cm, i.e. reached
    # and overshot) -- the servo then turned back to a point it had passed, and
    # the next answer re-aimed it somewhere else: that is the S-shaped walk.
    # 1.00 m covers the p90 cadence so the robot pursues a point that is still
    # ahead of it.
    lookahead_m: float = 1.00
    # How far along the model's path one answer may commit to.  ``lookahead_m``
    # decides *where* the curvature is read from; this decides how much of that
    # path the robot actually drives before the next answer re-aims it.  The
    # two were the same number until 2026-11-04, and that is why a 0.30 m path
    # drawn by the model was driven as a one-metre lunge at the chassis cap:
    # the servo's goal sat a full lookahead away, so it held full speed for
    # seconds and arrived -- or overshot -- long after the frame that asked
    # for it.  The live complaint was exactly that: "the steering is too
    # strong, the throttle is too deep".  0.35 m is about one answer's worth
    # of travel at ``trajectory_cruise_vx``.
    trajectory_advance_m: float = 0.35
    # Forward speed a trajectory answer is driven at.  Deliberately below
    # ``max_vx``: ``max_vx`` is the chassis ceiling, not a target.  The old
    # controller asked for ``lookahead / action_step_s`` = 1.0 m/s, was clipped
    # to the cap every single frame, and stretched the step to 4 s -- i.e. it
    # never once commanded anything but full throttle.
    trajectory_cruise_vx: float = 0.18
    # A trajectory whose pursued point is closer than this means "stay here",
    # not "creep two centimetres": it is read as the model's arrival verdict
    # (the same statement a discrete STOP makes) and the runtime's consecutive
    # STOP counter can then finish the task.  Without it a short trajectory is
    # executed as a tiny held velocity forever, which is the "it got there and
    # then kept shuffling around the goal" behaviour.
    trajectory_stop_m: float = 0.05
    # There is deliberately no yaw gain.  The bearing to the lookahead point is
    # commanded directly (pure pursuit); the 2026-10-22 gain/boost experiments
    # amplified small bearings into swerves and were reverted.
    # Depth-based proximity gate.  This is not obstacle avoidance and it is not
    # part of the policy: it is a reflex that publishes zero velocity whenever
    # a surface inside a forward band is closer than stop_distance_m.  Fail
    # closed by default -- no depth, a stale depth frame or a frame with no
    # usable pixels all mean "blocked".
    safety_gate_enabled: bool = True
    safety_gate_require_depth: bool = True
    depth_topic: str = "/camera/depth/image_raw"
    safety_gate_stop_m: float = 0.30
    safety_gate_release_m: float = 0.50
    safety_gate_depth_timeout_s: float = 0.30
    # 0 = infer from dtype: uint16 is millimetres, float is metres.
    safety_gate_depth_scale_m: float = 0.0
    # Forward band, as fractions of the depth image.  The bottom is kept above
    # 1.0 on purpose: the floor just under the camera is always close.
    safety_gate_roi_top_fraction: float = 0.25
    safety_gate_roi_bottom_fraction: float = 0.80
    safety_gate_roi_width_fraction: float = 0.60
    safety_gate_min_valid_pixels: int = 200
    safety_gate_percentile: float = 1.0
    fault_after_failures: int = 3
    # A DualVLN answer of 0 is STOP: the model is saying the task is done.
    # A single 0, or a short burst of them, is not evidence of anything -- a
    # half-visible goal or one bad frame can produce it.  The task is only
    # finished when the model has been stuck on STOP continuously: at least
    # ``finish_after_stops`` answers in a row AND at least
    # ``finish_after_stops_min_s`` seconds since the run began.  Any driving
    # answer (1/2/3 or a trajectory) starts the run over.
    #
    # Finished means the same things an operator stop does: the plan is
    # retired, the client is disarmed and the model is not asked again; the
    # capability then reports "finished" / running=False so the executor can
    # end the leaf instead of polling a robot that will never move again.
    finish_after_stops: int = 20
    finish_after_stops_min_s: float = 30.0
    # The completion latch above is only half the fix.  Pilot replans the
    # moment a plan completes, so a finished VLN task that is dispatched again
    # used to restart the episode -- the robot was handed the same instruction
    # forever and answered STOP forever.  A SET carrying the *same*
    # instruction this many seconds after a finish is that echo, not a human
    # asking again: it is answered with status="finished" and changes nothing.
    finish_reissue_grace_s: float = 10.0
    # How long `vln/task set` may block waiting for the episode it started to
    # end.  Blocking is what makes the plan node represent the task: the node
    # stays Running while the robot drives, so Pilot has nothing to replan
    # until the answer is terminal, and the tool result it finally sees says
    # "finished" outright.  The old "queued, return immediately" answer turned
    # every task into a plan that completed in milliseconds and invited the
    # next dispatch.  <= 0 disables blocking (old behaviour); the bound only
    # exists so a wedged model cannot pin the call forever.
    set_block_timeout_s: float = 900.0
    # A single ``/eval_dual`` request fails often enough on the live server (a
    # ~25% HTTP 500 rate was measured against 10.0.105.11:5801) that one attempt
    # per frame is not enough: the frame is lost *and* the episode history is
    # cleared, which is exactly how the robot ends up standing still.  Retry the
    # same frame before believing the failure.
    failure_retries: int = 1
    # Mutually exclusive command-source mux.
    nav_raw_topic: str = "/ranger/vln/nav_cmd_vel_raw"
    vln_raw_topic: str = "/ranger/vln/cmd_vel_raw"
    selected_output_topic: str = "/cmd_vel_nav"
    selected_source: str = SOURCE_VLN
    source_max_age_s: float = 0.25
    # Ranger chassis guard (see GUARD_* above).  Defaults reproduce the
    # effective Go2 ceiling exactly: min(vln 0.50, chassis 0.25) == 0.25 m/s and
    # min(0.40, 0.40) == 0.40 rad/s.
    guard_input_topic: str = GUARD_INPUT_TOPIC
    guard_output_topic: str = GUARD_OUTPUT_TOPIC
    guard_forward_only: bool = True
    guard_max_vx_mps: float = 0.25
    guard_max_wz_rps: float = 0.40
    guard_max_linear_accel_mps2: float = 0.30
    guard_max_angular_accel_rps2: float = 0.80
    guard_command_timeout_s: float = 0.25
    guard_publish_hz: float = 50.0
    guard_min_turn_radius_m: float = GUARD_MIN_TURN_RADIUS_M
    guard_turn_policy: str = "clamp_yaw"
    guard_spin_vx_epsilon_mps: float = 0.02

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any] | None) -> "RuntimeConfig":
        cfg = dict(source or {})
        mux_value = cfg.get("source_mux", {})
        if mux_value is None:
            mux_value = {}
        if not isinstance(mux_value, Mapping):
            raise ValueError("source_mux must be a JSON object")
        mux = dict(mux_value)

        mode = str(cfg.get("mode") or "dry-run").strip().lower()
        if mode not in {"dry-run", "live"}:
            raise ValueError("mode must be exactly 'dry-run' or 'live'")

        guard_turn_policy = str(
            cfg.get("guard_turn_policy") or cls.guard_turn_policy
        ).strip().lower()
        if guard_turn_policy not in GUARD_TURN_POLICIES:
            raise ValueError(
                "guard_turn_policy must be one of "
                f"{', '.join(GUARD_TURN_POLICIES)}"
            )

        instruction = str(cfg.get("instruction") or cls.instruction).strip()
        if not instruction or len(instruction) > 1000:
            raise ValueError("instruction must contain 1..1000 characters")

        require_instruction = _explicit_bool(
            cfg.get("require_instruction"),
            "require_instruction",
            cls.require_instruction,
        )

        model_input_mode = str(
            cfg.get("model_input_mode") or MODEL_INPUT_MODE
        ).strip().lower()
        if model_input_mode not in VALID_MODEL_INPUT_MODES:
            raise ValueError(
                "model_input_mode must be 'full_frame' (send the camera frame, "
                "let the server resize) or 'center_crop_height'"
            )
        model_crop_size = int(cfg.get("model_crop_size", MODEL_CROP_SIZE))
        if model_crop_size != MODEL_CROP_SIZE:
            raise ValueError("model_crop_size must be the official 384 pixels")

        jpeg_quality = int(cfg.get("jpeg_quality", cls.jpeg_quality))
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be in [1, 100]")

        # InternVLA plans over a variable-length trajectory rather than the
        # official 0.1 s control step, so control_dt is fixed here and only
        # recorded for the plan record; nothing downstream divides by it.
        control_dt = _finite_float(cfg.get("control_dt", CONTROL_DT), "control_dt")
        if not math.isclose(control_dt, CONTROL_DT, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("control_dt must be 0.1 seconds")

        max_vx = _positive_float(
            cfg.get("max_vx"),
            "max_vx",
            MAX_VX,
            MAX_CONFIGURED_VX,
        )
        max_wz = _positive_float(
            cfg.get("max_wz"),
            "max_wz",
            MAX_WZ,
            MAX_CONFIGURED_WZ,
        )
        plan_hold_s = _positive_float(
            cfg.get("plan_hold_s"),
            "plan_hold_s",
            cls.plan_hold_s,
            MAX_PLAN_AGE_LIMIT_S,
        )
        # The adapter's step horizon and the command expiry are one setting.
        max_plan_age_s = _finite_float(
            cfg.get("max_plan_age_s", plan_hold_s), "max_plan_age_s"
        )
        if not math.isclose(max_plan_age_s, plan_hold_s, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("max_plan_age_s must equal plan_hold_s")

        lockstep_enabled = _explicit_bool(
            cfg.get("lockstep_enabled"),
            "lockstep_enabled",
            cls.lockstep_enabled,
        )
        action_step_s = _positive_float(
            cfg.get("action_step_s"),
            "action_step_s",
            cls.action_step_s,
            MAX_PLAN_AGE_LIMIT_S,
        )
        # A step that outlives its plan would be cut off mid-motion by the
        # watchdog, which is exactly the half-executed action lockstep exists
        # to remove.  The settle window is allowed to run past the expiry: the
        # robot is already commanding zero there.
        if action_step_s > plan_hold_s + 1e-9:
            raise ValueError(
                "action_step_s must not exceed plan_hold_s, or the watchdog "
                "ends the step early"
            )
        step_settle_s = _bounded_float(
            cfg.get("step_settle_s"),
            "step_settle_s",
            cls.step_settle_s,
            minimum=0.0,
            maximum=5.0,
        )

        selected_source = str(
            mux.get("selected_source", cfg.get("selected_source", SOURCE_VLN))
        ).strip().lower()
        if selected_source not in VALID_SOURCES:
            raise ValueError(
                f"selected_source must be one of {', '.join(VALID_SOURCES)}"
            )

        vln_raw_topic = _absolute_topic(
            mux.get("vln_input_topic", cfg.get("vln_raw_topic", cls.vln_raw_topic)),
            "vln_raw_topic",
            cls.vln_raw_topic,
        )
        command_topic = _absolute_topic(
            cfg.get("command_topic"), "command_topic", cls.command_topic
        )
        if command_topic != vln_raw_topic:
            raise ValueError("command_topic and source_mux.vln_input_topic must match")

        safety_gate_enabled = _explicit_bool(
            cfg.get("safety_gate_enabled"),
            "safety_gate_enabled",
            cls.safety_gate_enabled,
        )
        safety_gate_require_depth = _explicit_bool(
            cfg.get("safety_gate_require_depth"),
            "safety_gate_require_depth",
            cls.safety_gate_require_depth,
        )
        safety_gate_stop_m = _bounded_float(
            cfg.get("safety_gate_stop_m"),
            "safety_gate_stop_m",
            cls.safety_gate_stop_m,
            minimum=0.0,
            maximum=3.0,
            include_minimum=False,
        )
        safety_gate_release_m = _bounded_float(
            cfg.get("safety_gate_release_m"),
            "safety_gate_release_m",
            cls.safety_gate_release_m,
            minimum=0.0,
            maximum=5.0,
            include_minimum=False,
        )
        if safety_gate_release_m <= safety_gate_stop_m:
            raise ValueError(
                "safety_gate_release_m must exceed safety_gate_stop_m, or the "
                "gate chatters at the threshold"
            )
        safety_gate_roi_top_fraction = _bounded_float(
            cfg.get("safety_gate_roi_top_fraction"),
            "safety_gate_roi_top_fraction",
            cls.safety_gate_roi_top_fraction,
            minimum=0.0,
            maximum=1.0,
        )
        safety_gate_roi_bottom_fraction = _bounded_float(
            cfg.get("safety_gate_roi_bottom_fraction"),
            "safety_gate_roi_bottom_fraction",
            cls.safety_gate_roi_bottom_fraction,
            minimum=0.0,
            maximum=1.0,
        )
        if safety_gate_roi_bottom_fraction <= safety_gate_roi_top_fraction:
            raise ValueError(
                "safety_gate_roi_bottom_fraction must exceed "
                "safety_gate_roi_top_fraction"
            )

        return cls(
            mode=mode,
            rgb_topic=_absolute_topic(
                cfg.get("rgb_topic"), "rgb_topic", cls.rgb_topic
            ),
            command_topic=command_topic,
            instruction_topic=_absolute_topic(
                cfg.get("instruction_topic"),
                "instruction_topic",
                cls.instruction_topic,
            ),
            status_topic=_absolute_topic(
                cfg.get("status_topic"), "status_topic", cls.status_topic
            ),
            server_url=_server_base_url(cfg.get("server_url"), cls.server_url),
            instruction=instruction,
            require_instruction=require_instruction,
            model_input_mode=model_input_mode,
            model_crop_size=model_crop_size,
            jpeg_quality=jpeg_quality,
            http_timeout_s=_positive_float(
                cfg.get("http_timeout_s"), "http_timeout_s", cls.http_timeout_s, 600.0
            ),
            dispatch_hz=_positive_float(
                cfg.get("dispatch_hz"), "dispatch_hz", DEFAULT_DISPATCH_HZ, 100.0
            ),
            plan_hold_s=plan_hold_s,
            lockstep_enabled=lockstep_enabled,
            action_step_s=action_step_s,
            step_settle_s=step_settle_s,
            max_vx=max_vx,
            max_wz=max_wz,
            odom_feedback_enabled=_explicit_bool(
                cfg.get("odom_feedback_enabled"),
                "odom_feedback_enabled",
                cls.odom_feedback_enabled,
            ),
            odom_topic=_absolute_topic(
                cfg.get("odom_topic"), "odom_topic", cls.odom_topic
            ),
            odom_timeout_s=_bounded_float(
                cfg.get("odom_timeout_s"),
                "odom_timeout_s",
                cls.odom_timeout_s,
                minimum=0.0,
                maximum=5.0,
                include_minimum=False,
            ),
            odom_turn_tolerance_deg=_bounded_float(
                cfg.get("odom_turn_tolerance_deg"),
                "odom_turn_tolerance_deg",
                cls.odom_turn_tolerance_deg,
                minimum=0.0,
                maximum=45.0,
            ),
            odom_distance_tolerance_m=_bounded_float(
                cfg.get("odom_distance_tolerance_m"),
                "odom_distance_tolerance_m",
                cls.odom_distance_tolerance_m,
                minimum=0.0,
                maximum=0.5,
            ),
            odom_segment_timeout_factor=_bounded_float(
                cfg.get("odom_segment_timeout_factor"),
                "odom_segment_timeout_factor",
                cls.odom_segment_timeout_factor,
                minimum=1.0,
                maximum=10.0,
            ),
            servo_enabled=_explicit_bool(
                cfg.get("servo_enabled"),
                "servo_enabled",
                cls.servo_enabled,
            ),
            servo_kp_trans=_bounded_float(
                cfg.get("servo_kp_trans"),
                "servo_kp_trans",
                cls.servo_kp_trans,
                minimum=0.0,
                maximum=20.0,
                include_minimum=False,
            ),
            servo_kp_yaw=_bounded_float(
                cfg.get("servo_kp_yaw"),
                "servo_kp_yaw",
                cls.servo_kp_yaw,
                minimum=0.0,
                maximum=20.0,
                include_minimum=False,
            ),
            servo_position_tolerance_m=_bounded_float(
                cfg.get("servo_position_tolerance_m"),
                "servo_position_tolerance_m",
                cls.servo_position_tolerance_m,
                minimum=0.0,
                maximum=1.0,
                include_minimum=False,
            ),
            servo_yaw_tolerance_deg=_bounded_float(
                cfg.get("servo_yaw_tolerance_deg"),
                "servo_yaw_tolerance_deg",
                cls.servo_yaw_tolerance_deg,
                minimum=0.0,
                maximum=45.0,
                include_minimum=False,
            ),
            discrete_forward_m=_positive_float(
                cfg.get("discrete_forward_m"),
                "discrete_forward_m",
                cls.discrete_forward_m,
                5.0,
            ),
            discrete_step_deg=_positive_float(
                cfg.get("discrete_step_deg"),
                "discrete_step_deg",
                cls.discrete_step_deg,
                180.0,
            ),
            max_chunk_actions=_bounded_int(
                cfg.get("max_chunk_actions"),
                "max_chunk_actions",
                cls.max_chunk_actions,
                minimum=1,
                maximum=16,
            ),
            history_reset_frames=_bounded_int(
                cfg.get("history_reset_frames"),
                "history_reset_frames",
                cls.history_reset_frames,
                minimum=0,
                maximum=10000,
            ),
            lookahead_m=_positive_float(
                cfg.get("lookahead_m"), "lookahead_m", cls.lookahead_m, 5.0
            ),
            trajectory_advance_m=_positive_float(
                cfg.get("trajectory_advance_m"),
                "trajectory_advance_m",
                cls.trajectory_advance_m,
                5.0,
            ),
            trajectory_cruise_vx=_positive_float(
                cfg.get("trajectory_cruise_vx"),
                "trajectory_cruise_vx",
                cls.trajectory_cruise_vx,
                5.0,
            ),
            trajectory_stop_m=_positive_float(
                cfg.get("trajectory_stop_m"),
                "trajectory_stop_m",
                cls.trajectory_stop_m,
                5.0,
            ),
            safety_gate_enabled=safety_gate_enabled,
            safety_gate_require_depth=safety_gate_require_depth,
            depth_topic=_absolute_topic(
                cfg.get("depth_topic"), "depth_topic", cls.depth_topic
            ),
            safety_gate_stop_m=safety_gate_stop_m,
            safety_gate_release_m=safety_gate_release_m,
            safety_gate_depth_timeout_s=_bounded_float(
                cfg.get("safety_gate_depth_timeout_s"),
                "safety_gate_depth_timeout_s",
                cls.safety_gate_depth_timeout_s,
                minimum=0.0,
                maximum=5.0,
                include_minimum=False,
            ),
            safety_gate_depth_scale_m=_bounded_float(
                cfg.get("safety_gate_depth_scale_m"),
                "safety_gate_depth_scale_m",
                cls.safety_gate_depth_scale_m,
                minimum=0.0,
                maximum=1.0,
            ),
            safety_gate_roi_top_fraction=safety_gate_roi_top_fraction,
            safety_gate_roi_bottom_fraction=safety_gate_roi_bottom_fraction,
            safety_gate_roi_width_fraction=_bounded_float(
                cfg.get("safety_gate_roi_width_fraction"),
                "safety_gate_roi_width_fraction",
                cls.safety_gate_roi_width_fraction,
                minimum=0.0,
                maximum=1.0,
                include_minimum=False,
            ),
            safety_gate_min_valid_pixels=_bounded_int(
                cfg.get("safety_gate_min_valid_pixels"),
                "safety_gate_min_valid_pixels",
                cls.safety_gate_min_valid_pixels,
                minimum=1,
                maximum=1_000_000,
            ),
            safety_gate_percentile=_bounded_float(
                cfg.get("safety_gate_percentile"),
                "safety_gate_percentile",
                cls.safety_gate_percentile,
                minimum=0.0,
                maximum=50.0,
            ),
            fault_after_failures=_bounded_int(
                cfg.get("fault_after_failures"),
                "fault_after_failures",
                cls.fault_after_failures,
                minimum=1,
                maximum=100,
            ),
            finish_after_stops=_bounded_int(
                cfg.get("finish_after_stops"),
                "finish_after_stops",
                cls.finish_after_stops,
                minimum=1,
                maximum=1000,
            ),
            finish_after_stops_min_s=_bounded_float(
                cfg.get("finish_after_stops_min_s"),
                "finish_after_stops_min_s",
                cls.finish_after_stops_min_s,
                minimum=0.0,
                maximum=3600.0,
            ),
            finish_reissue_grace_s=_bounded_float(
                cfg.get("finish_reissue_grace_s"),
                "finish_reissue_grace_s",
                cls.finish_reissue_grace_s,
                minimum=0.0,
                maximum=600.0,
            ),
            set_block_timeout_s=_bounded_float(
                cfg.get("set_block_timeout_s"),
                "set_block_timeout_s",
                cls.set_block_timeout_s,
                minimum=0.0,
                maximum=86400.0,
            ),
            failure_retries=_bounded_int(
                cfg.get("failure_retries"),
                "failure_retries",
                cls.failure_retries,
                minimum=0,
                maximum=5,
            ),
            nav_raw_topic=_absolute_topic(
                mux.get(
                    "nav_input_topic",
                    cfg.get("nav_raw_topic", cls.nav_raw_topic),
                ),
                "nav_raw_topic",
                cls.nav_raw_topic,
            ),
            vln_raw_topic=vln_raw_topic,
            selected_output_topic=_absolute_topic(
                mux.get(
                    "output_topic",
                    cfg.get("selected_output_topic", cls.selected_output_topic),
                ),
                "selected_output_topic",
                cls.selected_output_topic,
            ),
            selected_source=selected_source,
            source_max_age_s=_positive_float(
                mux.get("max_age_s", cfg.get("source_max_age_s")),
                "source_max_age_s",
                cls.source_max_age_s,
                MAX_PLAN_AGE_LIMIT_S,
            ),
            guard_input_topic=_absolute_topic(
                cfg.get("guard_input_topic"),
                "guard_input_topic",
                cls.guard_input_topic,
            ),
            guard_output_topic=_absolute_topic(
                cfg.get("guard_output_topic"),
                "guard_output_topic",
                cls.guard_output_topic,
            ),
            guard_forward_only=_explicit_bool(
                cfg.get("guard_forward_only"),
                "guard_forward_only",
                cls.guard_forward_only,
            ),
            guard_max_vx_mps=_bounded_float(
                cfg.get("guard_max_vx_mps"),
                "guard_max_vx_mps",
                cls.guard_max_vx_mps,
                minimum=0.0,
                maximum=MAX_CONFIGURED_VX,
                include_minimum=False,
            ),
            guard_max_wz_rps=_bounded_float(
                cfg.get("guard_max_wz_rps"),
                "guard_max_wz_rps",
                cls.guard_max_wz_rps,
                minimum=0.0,
                maximum=MAX_CONFIGURED_WZ,
                include_minimum=False,
            ),
            guard_max_linear_accel_mps2=_bounded_float(
                cfg.get("guard_max_linear_accel_mps2"),
                "guard_max_linear_accel_mps2",
                cls.guard_max_linear_accel_mps2,
                minimum=0.0,
                maximum=10.0,
                include_minimum=False,
            ),
            guard_max_angular_accel_rps2=_bounded_float(
                cfg.get("guard_max_angular_accel_rps2"),
                "guard_max_angular_accel_rps2",
                cls.guard_max_angular_accel_rps2,
                minimum=0.0,
                maximum=20.0,
                include_minimum=False,
            ),
            guard_command_timeout_s=_bounded_float(
                cfg.get("guard_command_timeout_s"),
                "guard_command_timeout_s",
                cls.guard_command_timeout_s,
                minimum=0.0,
                maximum=5.0,
                include_minimum=False,
            ),
            guard_publish_hz=_bounded_float(
                cfg.get("guard_publish_hz"),
                "guard_publish_hz",
                cls.guard_publish_hz,
                minimum=1.0,
                maximum=200.0,
            ),
            guard_min_turn_radius_m=_bounded_float(
                cfg.get("guard_min_turn_radius_m"),
                "guard_min_turn_radius_m",
                cls.guard_min_turn_radius_m,
                minimum=0.0,
                maximum=10.0,
                include_minimum=False,
            ),
            guard_turn_policy=guard_turn_policy,
            guard_spin_vx_epsilon_mps=_bounded_float(
                cfg.get("guard_spin_vx_epsilon_mps"),
                "guard_spin_vx_epsilon_mps",
                cls.guard_spin_vx_epsilon_mps,
                minimum=0.0,
                maximum=1.0,
            ),
        )

    def as_ros_parameters(self) -> dict[str, Any]:
        """Return the flat parameter map consumed by ``InternVLANode``."""

        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(frozen=True)
class VelocityCommand:
    vx: float = 0.0
    wz: float = 0.0

    def clipped(self, max_vx: float = MAX_VX, max_wz: float = MAX_WZ) -> "VelocityCommand":
        vx = _finite_float(self.vx, "vx")
        wz = _finite_float(self.wz, "wz")
        return VelocityCommand(
            vx=max(-float(max_vx), min(float(max_vx), vx)),
            wz=max(-float(max_wz), min(float(max_wz), wz)),
        )


ZERO_COMMAND = VelocityCommand()


@dataclass(frozen=True)
class EncodedFrame:
    sequence: int
    jpeg: bytes
    source_timestamp: float
    received_monotonic: float
    # The chassis pose the frame was taken in, captured by the image callback
    # from the newest /odom sample.  This is the hand-eye anchor: the answer
    # this frame produces is applied to *this* pose, so the distance the robot
    # travels while the model is thinking is subtracted rather than ignored.
    # None when odometry is off or has not arrived yet.
    pose: Pose | None = None


class LatestFrameMailbox:
    """A one-slot mailbox: every incoming frame atomically replaces the prior one.

    This is the backpressure mechanism for a slow model.  The inference worker
    blocks for seconds inside one HTTP request while the camera keeps producing
    frames; because only the newest frame is kept, the worker always resumes on
    the freshest view instead of draining a backlog of stale images that no
    longer describe the world.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: EncodedFrame | None = None
        self._sequence = 0

    def put(
        self,
        jpeg: bytes,
        *,
        source_timestamp: float,
        received_monotonic: float | None = None,
        pose: Pose | None = None,
    ) -> EncodedFrame:
        payload = bytes(jpeg)
        if not payload:
            raise ValueError("jpeg frame must not be empty")
        stamp = _finite_float(source_timestamp, "source_timestamp")
        received = (
            time.monotonic()
            if received_monotonic is None
            else _finite_float(received_monotonic, "received_monotonic")
        )
        with self._condition:
            self._sequence += 1
            frame = EncodedFrame(self._sequence, payload, stamp, received, pose)
            self._latest = frame
            self._condition.notify_all()
            return frame

    def latest(self) -> EncodedFrame | None:
        with self._condition:
            return self._latest

    def wait_after(
        self,
        sequence: int,
        *,
        timeout_s: float,
        stop_event: threading.Event | None = None,
    ) -> EncodedFrame | None:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            while self._latest is None or self._latest.sequence <= sequence:
                if stop_event is not None and stop_event.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return None
                self._condition.wait(min(remaining, 0.1))
            return self._latest

    @property
    def retained_count(self) -> int:
        with self._condition:
            return 0 if self._latest is None else 1


# InternVLA trajectories are variable length and carry at least [x, y]; a
# yaw-bearing third element is preserved when the server provides one.
Waypoint = tuple[float, ...]


@dataclass(frozen=True)
class ProgramSegment:
    """One primitive's worth of motion: a command and how long to hold it.

    A DualVLN answer is a *chunk* -- ``[1, 1, 2, 2]`` is "forward, forward, turn
    left, turn left" and the reference deployment drives all four before it
    takes the next picture.  One segment per element is what makes this stack
    do the same instead of executing only the head of the chunk.
    """

    command: VelocityCommand
    duration_s: float
    # The geometry this segment is *for*, when the model's action named one:
    # a signed heading change for a turn, a forward distance for a move.  Zero
    # means "no goal of this kind" (a stop, a camera-only hold, a trajectory
    # segment).  With odometry feedback arriving, a segment with a goal ends
    # the moment the chassis reports having achieved it; the duration is then
    # only the fallback and the timeout base.
    turn_rad: float = 0.0
    distance_m: float = 0.0


def goal_from_program(
    start: Pose, segments: tuple[ProgramSegment, ...]
) -> Pose | None:
    """The world pose a program aims at, applied from ``start`` in order.

    ``None`` when no segment names a geometry: a chunk of STOPs or a camera-only
    answer has no target *pose* -- its command is a velocity to hold, not a
    place to go -- and the servo must not pretend otherwise.  A trajectory plan
    lands here too (its segments carry no turn/distance), which is why the plan
    store asks :func:`goal_from_waypoints` next.
    """

    goal = start
    moved = False
    for segment in segments:
        if segment.turn_rad:
            goal = advance_pose(goal, turn_rad=segment.turn_rad)
            moved = True
        if segment.distance_m:
            goal = advance_pose(goal, distance_m=segment.distance_m)
            moved = True
    return goal if moved else None


def goal_from_waypoints(
    start: Pose,
    waypoints: tuple[Waypoint, ...],
    *,
    lookahead_m: float = 1.00,
    stop_m: float = 0.05,
    advance_m: float | None = None,
) -> Pose | None:
    """The world pose a trajectory answer aims at, from ``start``.

    A trajectory is drawn in the robot's own frame (metres, +x forward, +y
    left), so it is a *relative* motion like a discrete step, not a place in
    the world: it has to be applied to a pose.  That pose is the one the answer
    landed on -- the same one the discrete path uses, for the same reason.

    The point aimed at is the first one at least ``lookahead_m`` away, else the
    last (``trajectory_to_velocity`` picks it identically).  The goal is that
    displacement from ``start``, with the heading turned by the bearing to it,
    so the servo arrives facing along the path instead of sideways-hopping onto
    a point.

    ``advance_m`` bounds how much of that displacement one answer commits to:
    the bearing still comes from the lookahead point (that is where the path
    is going), but the goal is placed no further than ``advance_m`` along it.
    Without the bound the servo spent a whole answer driving at the speed cap
    toward a point a metre away -- full throttle until it arrived, which is
    how the robot overshot targets it could see.  ``None`` keeps the old
    unbounded behaviour.

    ``None`` for a path shorter than ``stop_m``: the model drew "stay where you
    are".  That is an arrival verdict ("this is as close as the view supports"),
    not a two-centimetre move, and answering it with a creep toward a point
    under the robot's own bumper is how the robot ends up circling a target it
    has already reached.
    """

    points: list[tuple[float, float]] = []
    for point in waypoints:
        if len(point) < 2:
            continue
        try:
            x = float(point[0])
            y = float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            points.append((x, y))
    if not points:
        return None

    target = points[-1]
    for point in points:
        if math.hypot(point[0], point[1]) >= lookahead_m:
            target = point
            break

    distance = math.hypot(target[0], target[1])
    if distance <= stop_m:
        return None

    # Drive only as much of the path as one answer is allowed to commit to;
    # the direction is still the lookahead point's.  Guarded against a zero or
    # nonsensical bound: an explicit None means "no bound".
    if advance_m is not None:
        bound = float(advance_m)
        if bound > 0.0 and distance > bound:
            distance = bound

    bearing = math.atan2(target[1], target[0])
    return advance_pose(advance_pose(start, turn_rad=bearing), distance_m=distance)


@dataclass(frozen=True)
class InferencePlan:
    """One bounded motion command plus the model output it came from."""

    command: VelocityCommand
    waypoints: tuple[Waypoint, ...] | None
    control_dt: float = CONTROL_DT
    velocity_source: str = "discrete_action"
    # True when the model itself said "the goal is reached": discrete action 0,
    # or a trajectory whose pursued point sits inside the arrival box (see
    # ``trajectory_stop_m``).  A zero velocity is not enough to say so:
    # camera-only plans (5/9) are also zero but mean "wait".
    model_stop: bool = False
    # The whole answer as an ordered program.  ``command`` is the first
    # segment (what used to be the only thing executed); an empty program
    # means "hold ``command`` for one action step", which keeps every
    # hand-built plan (operator stop, tests) working unchanged.
    program: tuple[ProgramSegment, ...] = ()


@dataclass(frozen=True)
class DispatchState:
    command: VelocityCommand
    reason: str
    age_s: float | None
    version: int
    program: tuple[ProgramSegment, ...] = ()
    # World-frame pose this plan is trying to reach, and the pose the frame it
    # was computed from was taken in.  Both are None when the plan carries no
    # geometry (STOP, a camera-only answer, a hand-built plan) or when no
    # odometry sample accompanied the frame -- in which case there is nothing
    # to servo toward and the step controller runs as before.
    goal: Pose | None = None
    reference_pose: Pose | None = None
    # The model's trajectory when the answer carried one.  It is geometry even
    # though no segment names it, so a trajectory plan without a pose must be
    # held (servo_wait_odom) exactly like a discrete one instead of falling
    # through to the open-loop velocity hold.
    waypoints: tuple[Waypoint, ...] | None = None


STEP_RUNNING = "step"
STEP_DONE = "step_done"
SERVO_RUNNING = "servo"
SERVO_DONE = "servo_done"
# A plan that names a geometry arrived without a pose to measure it from --
# odometry is off, dead, or was not arriving when the frame was taken.  The
# servo holds instead of driving the plan blind.
SERVO_WAIT_ODOM = "servo_wait_odom"


class StepController:
    """Turn an expiring plan into one program of segments plus a settle window.

    The planner is open loop: every plan is computed from an image that is
    already one inference latency old, and free-running execution keeps
    commanding it while the next request is in flight.  Measured here, the
    model answers every ~0.78 s and the plan lives 1.5 s, so a step that
    should stop after ``action_step_s`` keeps being re-commanded from a stale
    view -- the robot rotates past the goal and the goal leaves the camera.

    This class is the missing clock.  Each plan *version* gets exactly its
    program -- one segment per model action; after the last segment the command
    is zero, and the gate reopens only once the chassis has had ``settle_s`` to
    come to rest.  A new plan arriving restarts the clock, so a slow model only
    makes the robot pause -- it no longer multiplies the motion.

    How a segment ends, in order of preference:

    1. **Odometry says it is done.**  ``pose`` is the newest ``/odom`` sample;
       a segment that carries a goal (``turn_rad`` / ``distance_m``) ends the
       moment the chassis has turned that angle / covered that distance from
       the pose the segment started in.  This is what the reference
       deployments do, and it is what stops a nominal "0.25 m" that really
       covers 0.20 m from becoming a task-long position error.
    2. **Its nominal duration has elapsed** -- exactly the old behaviour, and
       the only rule when no goal is attached (stop, camera hold, a trajectory
       the servo never sees) or when ``feedback_enabled`` is off, which is the
       rollback switch.
    3. **Its timeout**: ``timeout_factor`` x the nominal duration.  A goal that
       never reports reached (odometry that lies, a chassis blocked by the
       safety gate, wheels against a wall) still releases the program instead
       of driving one segment forever.

    A plan with an empty program is held for one ``step_s``; that is the path
    for hand-built plans (operator stop, tests) and it is the behaviour every
    single-command plan had before programs existed.
    """

    def __init__(
        self,
        *,
        step_s: float = DEFAULT_ACTION_STEP_S,
        settle_s: float = DEFAULT_STEP_SETTLE_S,
        enabled: bool = True,
        feedback_enabled: bool = False,
        turn_tolerance_rad: float = math.radians(
            DEFAULT_ODOM_TURN_TOLERANCE_DEG
        ),
        distance_tolerance_m: float = DEFAULT_ODOM_DISTANCE_TOLERANCE_M,
        timeout_factor: float = DEFAULT_ODOM_SEGMENT_TIMEOUT_FACTOR,
    ) -> None:
        self._step_s = _positive_float(
            step_s, "step_s", DEFAULT_ACTION_STEP_S, MAX_PLAN_AGE_LIMIT_S
        )
        self._settle_s = max(0.0, _finite_float(settle_s, "settle_s"))
        self._enabled = bool(enabled)
        # Default False so a directly constructed controller (tests, other
        # packages) keeps the pure-timing contract; the ROS node passes the
        # configured value explicitly.
        self._feedback_enabled = bool(feedback_enabled)
        self._turn_tolerance_rad = abs(_finite_float(turn_tolerance_rad, "turn_tolerance_rad"))
        self._distance_tolerance_m = abs(
            _finite_float(distance_tolerance_m, "distance_tolerance_m")
        )
        self._timeout_factor = max(1.0, _finite_float(timeout_factor, "timeout_factor"))
        self._lock = threading.Lock()
        self._version = -1
        self._segments: tuple[ProgramSegment, ...] = ()
        self._started_at: float | None = None
        self._seg_started_at: float | None = None
        self._seg_start_pose: Pose | None = None
        self._active = 0
        self._done_at: float | None = None
        self._latest_end_at: float | None = None
        self._feedback = "timer"
        self._reason = "no_plan"

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def segments(self) -> int:
        """Segments in the program currently being driven (0 when idle)."""

        with self._lock:
            return len(self._segments) if self._started_at is not None else 0

    def progress(self) -> dict[str, Any]:
        """Diagnostics for the status topic: which segment, and what ends it."""

        with self._lock:
            active = self._active
            segment = (
                self._segments[active] if active < len(self._segments) else None
            )
            goal = ""
            if segment is not None:
                if segment.turn_rad:
                    goal = f"turn {math.degrees(segment.turn_rad):+.1f} deg"
                elif segment.distance_m:
                    goal = f"forward {segment.distance_m:.3f} m"
            return {
                "segment": active + 1 if segment is not None else 0,
                "segments": len(self._segments) if self._started_at is not None else 0,
                "goal": goal,
                "feedback": self._feedback,
            }

    def _goal_reached(
        self, segment: ProgramSegment, pose: Pose | None
    ) -> bool | None:
        """True/False when odometry can judge this segment, None when it cannot.

        ``None`` means "use the timer", and it is the answer whenever feedback
        is off, either pose is missing, or the segment has no goal at all.
        """

        if not self._feedback_enabled:
            return None
        if pose is None or self._seg_start_pose is None:
            return None
        start = self._seg_start_pose
        if segment.turn_rad:
            if not turn_reached(
                start,
                pose,
                segment.turn_rad,
                tolerance_rad=self._turn_tolerance_rad,
            ):
                return False
        if segment.distance_m:
            if not distance_reached(
                start,
                pose,
                segment.distance_m,
                tolerance_m=self._distance_tolerance_m,
            ):
                return False
        if not (segment.turn_rad or segment.distance_m):
            return None
        return True

    def apply(
        self,
        state: DispatchState,
        *,
        now: float,
        pose: Pose | None = None,
    ) -> tuple[VelocityCommand, str]:
        """Return what to publish this tick, and the reason for it."""

        if not self._enabled:
            return state.command, state.reason
        with self._lock:
            if state.reason != "active_plan":
                # The plan expired or was retired: the watchdog owns the
                # brake, and the next plan starts a fresh program.  The last
                # program end is kept on purpose -- the chassis may still be
                # rolling, and the gate must not reopen until settle_s has
                # passed.
                self._version = -1
                self._segments = ()
                self._started_at = None
                self._seg_started_at = None
                self._seg_start_pose = None
                self._active = 0
                if self._done_at is None and self._latest_end_at is not None:
                    # The plan was retired before its program finished: the
                    # chassis is still rolling, so the settle window starts
                    # now rather than being skipped.
                    self._done_at = now
                self._reason = state.reason
                return ZERO_COMMAND, state.reason
            if state.version != self._version or self._started_at is None:
                self._version = state.version
                self._segments = state.program or (
                    ProgramSegment(state.command, self._step_s),
                )
                self._started_at = now
                self._active = 0
                self._seg_started_at = now
                self._seg_start_pose = pose
                self._done_at = None
                # Upper bound on when this program can be over: each segment
                # runs at most its own timeout.  `ready` uses it for ticks that
                # arrive without an `apply` in between (and when a segment is
                # ended early by odometry, `_done_at` is the tighter answer).
                self._latest_end_at = now + sum(
                    max(0.0, segment.duration_s)
                    * (
                        self._timeout_factor
                        if (segment.turn_rad or segment.distance_m)
                        and self._feedback_enabled
                        else 1.0
                    )
                    for segment in self._segments
                )
            if self._seg_start_pose is None and pose is not None:
                # Odometry started arriving after the segment did: adopt it
                # now, so the goal is still measured (from here) rather than
                # the whole segment falling back to the timer.
                self._seg_start_pose = pose
                self._feedback = "odom"
            while True:
                if self._active >= len(self._segments):
                    if self._done_at is None:
                        self._done_at = now
                    self._reason = STEP_DONE
                    return ZERO_COMMAND, STEP_DONE
                segment = self._segments[self._active]
                # Not `self._seg_started_at or now`: a segment that started at
                # monotonic 0.0 is falsy, and every tick would read elapsed=0.
                started = (
                    self._seg_started_at
                    if self._seg_started_at is not None
                    else now
                )
                elapsed = now - started
                reached = self._goal_reached(segment, pose)
                if reached is True:
                    self._feedback = "odom"
                elif reached is False:
                    self._feedback = "odom"
                    if elapsed < segment.duration_s * self._timeout_factor:
                        self._reason = STEP_RUNNING
                        return segment.command, STEP_RUNNING
                else:
                    self._feedback = "timer"
                    if elapsed < segment.duration_s:
                        self._reason = STEP_RUNNING
                        return segment.command, STEP_RUNNING
                # Goal reached, or its timeout expired: on to the next one.
                self._active += 1
                self._seg_started_at = now
                self._seg_start_pose = pose

    def ready(self, *, now: float) -> bool:
        """True when it is safe to capture the next frame and ask again."""

        if not self._enabled:
            return True
        with self._lock:
            if self._done_at is not None:
                return now >= self._done_at + self._settle_s
            if self._started_at is None:
                # No program at all (nothing has been driven yet): open.
                return True
            # A program is running and nothing has ended it yet on this tick.
            # Fall back to the bound it cannot outlive, so the gate still
            # reopens if apply() somehow stops being called.
            return now >= (self._latest_end_at or now) + self._settle_s


class PlanStore:
    """Thread-safe latest inference plan with a bounded expiry.

    The expiry is the brake.  Nothing else has to detect a dead model, a
    dropped link or a stalled worker: if no fresh plan arrives, the dispatcher
    falls back to zero within ``max_plan_age_s`` on its own.
    """

    def __init__(
        self,
        *,
        max_plan_age_s: float = MAX_PLAN_AGE_S,
        max_vx: float = MAX_VX,
        max_wz: float = MAX_WZ,
        segment_timeout_factor: float = 1.0,
        lookahead_m: float = 1.00,
        trajectory_stop_m: float = 0.05,
        trajectory_advance_m: float | None = None,
    ) -> None:
        self._max_plan_age_s = float(max_plan_age_s)
        self._max_vx = float(max_vx)
        self._max_wz = float(max_wz)
        # Where a trajectory answer's goal lands: the same rules the adapter
        # uses to turn that trajectory into a velocity, so the two paths agree
        # about which point of the path is being pursued.
        self._lookahead_m = max(0.0, _finite_float(lookahead_m, "lookahead_m"))
        self._trajectory_stop_m = max(
            0.0, _finite_float(trajectory_stop_m, "trajectory_stop_m")
        )
        # How much of the path one answer commits to.  ``None`` keeps the
        # unbounded (pre-2026-11-04) goal placement.
        self._trajectory_advance_m = (
            None
            if trajectory_advance_m is None
            else max(0.0, _finite_float(trajectory_advance_m, "trajectory_advance_m"))
        )
        # How much longer than its nominal duration a *goal-bearing* segment
        # may take before the step controller gives up on it.  Kept in sync
        # with the step controller's own ``timeout_factor`` by the node; 1.0
        # here means "a segment never outlives its nominal duration", which is
        # the behaviour before odometry feedback existed.
        self._segment_timeout_factor = max(
            1.0, _finite_float(segment_timeout_factor, "segment_timeout_factor")
        )
        self._lock = threading.Lock()
        self._plan: InferencePlan | None = None
        self._arrival_monotonic = 0.0
        self._expiry_monotonic = 0.0
        self._version = 0
        self._goal: Pose | None = None
        self._reference_pose: Pose | None = None

    def update(
        self,
        plan: InferencePlan,
        *,
        now: float | None = None,
        reference_pose: Pose | None = None,
    ) -> int:
        arrival = time.monotonic() if now is None else _finite_float(now, "now")
        program = tuple(
            ProgramSegment(
                segment.command.clipped(self._max_vx, self._max_wz),
                max(0.0, _finite_float(segment.duration_s, "duration_s")),
                _finite_float(segment.turn_rad, "turn_rad"),
                _finite_float(segment.distance_m, "distance_m"),
            )
            for segment in plan.program
        )
        bounded = InferencePlan(
            command=plan.command.clipped(self._max_vx, self._max_wz),
            waypoints=plan.waypoints,
            control_dt=plan.control_dt,
            velocity_source=plan.velocity_source,
            model_stop=plan.model_stop,
            program=program,
        )
        # The watchdog must outlive the motion the plan itself declares, or a
        # committed chunk would be cut off halfway through: the brake is
        # "this plan's own duration has run out and nothing new arrived", not
        # "the network was quiet for plan_hold_s".
        #
        # With odometry feedback a segment with a goal may legitimately run up
        # to ``segment_timeout_factor`` x its nominal duration (the chassis
        # has not reported "done" yet).  The watchdog has to allow for that,
        # or it -- not the step controller -- becomes the thing that ends the
        # segment, and the robot stops short without ever saying why.
        hold = self._max_plan_age_s
        if program:
            hold = max(
                hold,
                sum(
                    max(0.0, segment.duration_s)
                    * (
                        self._segment_timeout_factor
                        if (segment.turn_rad or segment.distance_m)
                        else 1.0
                    )
                    for segment in program
                ),
            )
        # The goal is worked out once, here, from the pose the answer was
        # measured from -- not per dispatch tick from the current pose.
        # Re-deriving it every tick would make the target run away with the
        # robot; fixing it here turns the answer into a place on the floor.
        #
        # Discrete programs carry their geometry in the segments; a trajectory
        # carries it as a path, which ``goal_from_waypoints`` reduces to the one
        # point the controller is actually pursuing.  Either way the servo gets
        # a world pose and can say "arrived", which a held velocity never can --
        # that gap is why a trajectory plan used to drive open-loop and never
        # come to rest on its target.
        goal = None
        if reference_pose is not None:
            goal = goal_from_program(reference_pose, program)
            if goal is None and plan.waypoints:
                goal = goal_from_waypoints(
                    reference_pose,
                    plan.waypoints,
                    lookahead_m=self._lookahead_m,
                    stop_m=self._trajectory_stop_m,
                    advance_m=self._trajectory_advance_m,
                )
        with self._lock:
            self._plan = bounded
            self._arrival_monotonic = arrival
            self._expiry_monotonic = arrival + hold
            self._version += 1
            self._goal = goal
            self._reference_pose = reference_pose
            return self._version

    def clear(self) -> None:
        with self._lock:
            self._plan = None
            self._arrival_monotonic = 0.0
            self._expiry_monotonic = 0.0
            self._version += 1
            self._goal = None
            self._reference_pose = None

    def dispatch(self, *, now: float | None = None) -> DispatchState:
        current = time.monotonic() if now is None else _finite_float(now, "now")
        with self._lock:
            plan = self._plan
            arrival = self._arrival_monotonic
            expiry = self._expiry_monotonic
            version = self._version
            goal = self._goal
            reference_pose = self._reference_pose
        if plan is None:
            return DispatchState(ZERO_COMMAND, "no_plan", None, version)
        age = max(0.0, current - arrival)
        if current > expiry:
            return DispatchState(ZERO_COMMAND, "stale_plan", age, version)
        return DispatchState(
            plan.command,
            "active_plan",
            age,
            version,
            plan.program,
            goal,
            reference_pose,
            plan.waypoints,
        )
