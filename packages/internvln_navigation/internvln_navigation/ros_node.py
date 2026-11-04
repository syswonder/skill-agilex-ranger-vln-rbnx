"""ROS 2 image, inference, dispatch, and command-source mux node.

One node does four things, in four threads at most:

    image callback (ROS)   centre-crop, JPEG-encode, hand to the mailbox
    inference worker       the ONLY slow thread: HTTP to the model server
    dispatch timer (ROS)   read the plan store at 50 Hz and publish
    instruction timer      drain the operator handoff once every 100 ms

Nothing in the dispatch path ever waits on the model.  The plan store expires
on its own, so if the model, the network or the worker stalls, the published
command becomes zero within ``plan_hold_s`` with no other mechanism involved.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Mapping, MutableSequence, Sequence

import cv2
from cv_bridge import CvBridge
from geometry_msgs.msg import Twist
import rclpy
from nav_msgs.msg import Odometry
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from std_msgs.msg import String

from .chassis_guard_node import RangerChassisGuardNode
from .core import (
    SERVO_DONE,
    SERVO_RUNNING,
    SERVO_WAIT_ODOM,
    SOURCE_NAVIGATION,
    SOURCE_VLN,
    STOP_PAYLOADS,
    LatestFrameMailbox,
    ModelUnavailableError,
    PlanStore,
    RuntimeConfig,
    StepController,
    VelocityCommand,
    ZERO_COMMAND,
)
from .http_client import InternVLAClient
from .image_preprocess import prepare_model_input
from .odometry import Pose, PoseBuffer, servo_command, yaw_from_quaternion
from .safety_gate import (
    DepthSafetyGate,
    SafetyGateDecision,
    gate_config_from_runtime,
)
from .source_mux import CommandSourceMux, TwistCommand
from .vln_runtime import RUNTIME
from .worker import InferenceWorker


PARAMETER_DEFAULTS = RuntimeConfig().as_ros_parameters()
INSTRUCTION_PUMP_HZ = 10.0
STATUS_PUBLISH_HZ = 1.0


def _velocity_twist(command: VelocityCommand) -> Twist:
    message = Twist()
    message.linear.x = float(command.vx)
    message.angular.z = float(command.wz)
    return message


def _full_twist(command: TwistCommand) -> Twist:
    message = Twist()
    message.linear.x = float(command.linear_x)
    message.linear.y = float(command.linear_y)
    message.linear.z = float(command.linear_z)
    message.angular.x = float(command.angular_x)
    message.angular.y = float(command.angular_y)
    message.angular.z = float(command.angular_z)
    return message


def _from_twist(message: Twist) -> TwistCommand:
    return TwistCommand.finite(
        (
            message.linear.x,
            message.linear.y,
            message.linear.z,
            message.angular.x,
            message.angular.y,
            message.angular.z,
        )
    )


def _image_source_timestamp(message: Image) -> float:
    return (
        float(message.header.stamp.sec)
        + float(message.header.stamp.nanosec) * 1e-9
    )


class InternVLANode(Node):
    """Forward the freshest camera frame and dispatch InternVLA commands."""

    def __init__(
        self,
        *,
        config_overrides: Mapping[str, Any] | None = None,
        context: Context | None = None,
        bind_shared_runtime: bool = True,
    ) -> None:
        overrides = [
            Parameter(name, value=value)
            for name, value in dict(config_overrides or {}).items()
            if name in PARAMETER_DEFAULTS
        ]
        super().__init__(
            "internvln_navigation",
            context=context,
            parameter_overrides=overrides,
        )
        parameter_values: dict[str, Any] = {}
        for name, default in PARAMETER_DEFAULTS.items():
            parameter_values[name] = self.declare_parameter(name, default).value
        self._config = RuntimeConfig.from_mapping(parameter_values)

        self._bridge = CvBridge()
        # The proximity gate is created in both modes so its state can be
        # watched in dry-run; it publishes nothing by itself.
        self._safety_gate = DepthSafetyGate(gate_config_from_runtime(self._config))
        self._safety_decision = self._safety_gate.check()
        self._safety_blocked_last = self._safety_decision.blocked
        self._last_safety_log = 0.0
        self._depth_subscription = None
        self._mailbox = LatestFrameMailbox()
        # Closed-loop steps: the newest /odom sample, read by the dispatcher to
        # end a segment when the chassis has actually achieved it.  The buffer
        # is fed in both modes -- in dry-run nothing moves, so the pose simply
        # never changes, which is harmless and keeps the wiring identical.
        self._poses = PoseBuffer()
        self._odom_subscription = None
        self._odom_seen = False
        # Odometry is a *pose source*, not just the step controller's switch:
        # the servo needs it too, and the two rollback switches are meant to be
        # independent (one decides "does a segment end on measurement", the
        # other "does a plan become a goal").  Subscribing when either is on
        # keeps `servo_enabled: true, odom_feedback_enabled: false` from
        # silently holding still for want of a pose.
        self._uses_odom = bool(
            self._config.odom_feedback_enabled or self._config.servo_enabled
        )
        # Whether the last dispatch tick had a usable pose; published on the
        # status topic so "why is it still on the timer?" is answerable.
        self._pose_fresh = False
        self._plans = PlanStore(
            max_plan_age_s=self._config.plan_hold_s,
            max_vx=self._config.max_vx,
            max_wz=self._config.max_wz,
            # The watchdog has to allow for a segment that is still waiting on
            # odometry, or it becomes the thing that cuts the step short.
            segment_timeout_factor=(
                self._config.odom_segment_timeout_factor
                if self._config.odom_feedback_enabled
                else 1.0
            ),
            # Trajectory answers become goals too: the store reduces the path
            # to the point the controller pursues, using the same lookahead and
            # the same "short path means stay here" threshold as the adapter.
            lookahead_m=self._config.lookahead_m,
            trajectory_stop_m=self._config.trajectory_stop_m,
            # One answer commits to at most this much of the path; the rest is
            # the next answer's business.
            trajectory_advance_m=self._config.trajectory_advance_m,
        )
        # Stop-and-go: one plan version is driven for at most action_step_s,
        # then the robot holds still until the next answer.  The gate below is
        # how the worker is told not to look at a moving camera.
        self._stepper = StepController(
            step_s=self._config.action_step_s,
            settle_s=self._config.step_settle_s,
            enabled=self._config.lockstep_enabled,
            feedback_enabled=self._config.odom_feedback_enabled,
            turn_tolerance_rad=math.radians(
                self._config.odom_turn_tolerance_deg
            ),
            distance_tolerance_m=self._config.odom_distance_tolerance_m,
            timeout_factor=self._config.odom_segment_timeout_factor,
        )
        self._step_gate = threading.Event()
        self._step_gate.set()
        # The servo tick that produced the command being published, if any,
        # plus the goal it was chasing.  Published on the status topic so "is
        # it chasing a goal or holding, and where is the goal?" is answerable
        # without guesswork.
        self._servo = None
        self._servo_goal = None
        # The command the last dispatch tick actually produced, after the
        # safety gate, published on status.  Everything else in the payload
        # describes intent (what the plan is, what the servo measures); this is
        # the one field that says what went out of the node, so "did the wheels
        # do what the model asked?" can be answered by comparing it with the
        # recorded /cmd_vel instead of inferring it from `step`.
        self._last_command = None
        self._client = InternVLAClient(self._config)
        self._mux = CommandSourceMux(
            self._config.selected_source,
            max_age_s=self._config.source_max_age_s,
        )
        self._bind_shared_runtime = bool(bind_shared_runtime)
        if self._bind_shared_runtime:
            # Hand this node's client and plan store to the Robonix capability,
            # which runs on its own thread and only ever touches them through
            # RUNTIME.pump() from the instruction timer below.
            RUNTIME.set_default(self._config.instruction)
            RUNTIME.bind(client=self._client, plans=self._plans)
            # How many consecutive model STOP answers end the task.  The
            # runtime counts them and pump() retires the plan, exactly like an
            # operator stop, so an executor polling `get` sees the leaf end.
            RUNTIME.configure(
                finish_after_stops=self._config.finish_after_stops,
                finish_after_stops_min_s=self._config.finish_after_stops_min_s,
                # Tells the capability how to answer the upper layer's echo of
                # a task it just saw finish, and how long `set` may block while
                # the task it started is actually running.
                finish_reissue_grace_s=self._config.finish_reissue_grace_s,
                set_block_timeout_s=self._config.set_block_timeout_s,
            )
            # The gate boots fail-closed when depth is required, and that has to
            # be visible to the capability before the first depth frame: an
            # instruction issued into a blind robot is held, not armed.
            RUNTIME.set_safety_state(
                blocked=self._safety_decision.blocked,
                reason=self._safety_decision.reason,
                nearest_m=self._safety_decision.nearest_m,
                stop_distance_m=self._safety_decision.stop_distance_m,
            )

        self._last_frame_error_log = 0.0
        self._last_depth_error_log = 0.0
        self._last_inference_error_log = 0.0
        self._last_dispatch_log = 0.0
        self._last_pump_log = 0.0
        self._closed = False
        self._raw_publisher = None
        self._selected_publisher = None
        self._nav_subscription = None
        self._vln_subscription = None

        self._image_subscription = self.create_subscription(
            Image,
            self._config.rgb_topic,
            self._on_image,
            qos_profile_sensor_data,
        )
        # The instruction ingress exists in dry-run too: it is an input, and
        # being able to confirm it works without arming the chassis is the
        # entire point of dry-run.
        self._instruction_subscription = self.create_subscription(
            String,
            self._config.instruction_topic,
            self._on_instruction,
            10,
        )
        # The depth gate is an input too, so it is wired in dry-run as well.
        # Without it there is no proximity reflex at all: the model sees one
        # RGB frame and has no depth.
        if self._config.safety_gate_enabled:
            self._depth_subscription = self.create_subscription(
                Image,
                self._config.depth_topic,
                self._on_depth,
                qos_profile_sensor_data,
            )
        # Odometry is an input, like the depth gate: wired in dry-run too, so
        # "is /odom even publishing?" can be answered without arming anything.
        if self._uses_odom:
            self._odom_subscription = self.create_subscription(
                Odometry,
                self._config.odom_topic,
                self._on_odom,
                qos_profile_sensor_data,
            )
        self._status_publisher = self.create_publisher(
            String, self._config.status_topic, 1
        )

        # Dry-run intentionally creates no *velocity* publisher.  Live has no
        # armed/disarmed state: activation immediately starts the two velocity
        # publishers and the mutually exclusive source mux.  The status topic
        # above is a String diagnostic and publishes in both modes.
        if self._config.mode == "live":
            self._raw_publisher = self.create_publisher(
                Twist, self._config.command_topic, 1
            )
            self._selected_publisher = self.create_publisher(
                Twist, self._config.selected_output_topic, 1
            )
            self._nav_subscription = self.create_subscription(
                Twist,
                self._config.nav_raw_topic,
                self._on_navigation_command,
                1,
            )
            self._vln_subscription = self.create_subscription(
                Twist,
                self._config.vln_raw_topic,
                self._on_vln_command,
                1,
            )

        self._worker = InferenceWorker(
            self._mailbox,
            self._client,
            self._plans,
            on_plan=self._on_plan,
            on_error=self._on_inference_error,
            # The gate is the stop-and-go interlock: it is only meaningful when
            # the servo is off, because the servo is what lets the robot keep
            # driving while the next answer is in flight.
            gate=(
                self._step_gate
                if (self._config.lockstep_enabled and not self._config.servo_enabled)
                else None
            ),
            # Read at the moment the answer lands, not when the frame was
            # taken -- see InferenceWorker's ``pose_source``.
            pose_source=self._latest_pose,
        )
        self._worker.start()
        self._dispatch_timer = self.create_timer(
            1.0 / self._config.dispatch_hz,
            self._dispatch,
        )
        self._instruction_timer = self.create_timer(
            1.0 / INSTRUCTION_PUMP_HZ,
            self._pump_instruction,
        )
        self._status_timer = self.create_timer(
            1.0 / STATUS_PUBLISH_HZ,
            self._publish_status,
        )
        self.get_logger().info(
            "InternVLA active: "
            f"mode={self._config.mode}, rgb={self._config.rgb_topic}, "
            f"server={self._config.server_url}, source={self._config.selected_source}, "
            f"hold={self._config.plan_hold_s:.2f}s, "
            f"servo={self._config.servo_enabled} "
            f"(Kp={self._config.servo_kp_trans:.1f}/"
            f"{self._config.servo_kp_yaw:.1f}), "
            f"lockstep={self._config.lockstep_enabled} "
            f"(step={self._config.action_step_s:.2f}s + "
            f"settle={self._config.step_settle_s:.2f}s), "
            f"instruction={self._config.instruction!r}"
        )
        if self._config.safety_gate_enabled:
            self.get_logger().info(
                "proximity gate: "
                f"depth={self._config.depth_topic}, "
                f"stop<{self._config.safety_gate_stop_m:.2f} m, "
                f"release>{self._config.safety_gate_release_m:.2f} m, "
                f"require_depth={self._config.safety_gate_require_depth} "
                "(fail-closed while no depth frame is arriving)"
            )
        else:
            self.get_logger().warn(
                "proximity gate DISABLED: the policy has no depth and cannot "
                "avoid an obstacle it does not see"
            )
        if self._config.require_instruction and not self._client.armed:
            # Say it out loud: a silent stack and a broken stack look identical
            # from the status topic's `requests` counter alone.
            self.get_logger().info(
                "no instruction in force: the model will NOT be asked until one "
                "arrives (robonix/primitive/vln/task set, `rbnx chat`, or "
                f"`ros2 topic pub {self._config.instruction_topic} ...`); "
                "holding still in the meantime"
            )

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self._config

    @property
    def inference_client(self) -> InternVLAClient:
        return self._client

    # ------------------------------------------------------------------- input

    def _latest_pose(self, now: float | None = None) -> Pose | None:
        """The newest fresh odometry pose, or None when there is none.

        One definition for the two places that read it: the frame is stamped
        with it at capture, and (for the servo) the answer's steps are measured
        from it when the answer lands.
        """
        if not self._uses_odom:
            return None
        return self._poses.latest(
            max_age_s=self._config.odom_timeout_s,
            now=time.monotonic() if now is None else now,
        )

    def _on_image(self, message: Image) -> None:
        # Stamp the frame with the pose it was taken in, before the decode and
        # the JPEG encode (a few ms later).  It is the robot's record of what
        # the model was looking at; the servo measures the answer from the pose
        # at the moment the answer arrives instead (see InferenceWorker).
        pose = self._latest_pose()
        try:
            bgr = self._bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
        except Exception as error:
            self._log_frame_error(error)
            return

        # Queue the frame the model will see.  This is the only consumer of the
        # frame: the MiniCPM bridge also mirrored frames to a preview endpoint,
        # which at 5 Hz would have polluted DualVLN's short frame history and
        # corrupted the very context the model plans from.
        try:
            model_bgr, _crop_geometry = prepare_model_input(
                bgr,
                mode=self._config.model_input_mode,
                crop_size=self._config.model_crop_size,
            )
            ok, encoded = cv2.imencode(
                ".jpg",
                model_bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), self._config.jpeg_quality],
            )
            if not ok:
                raise ValueError("OpenCV JPEG encoder returned false")
            self._mailbox.put(
                encoded.tobytes(),
                source_timestamp=_image_source_timestamp(message),
                pose=pose,
            )
        except Exception as error:
            self._log_frame_error(error)

    def _on_depth(self, message: Image) -> None:
        """Reduce one depth frame to a proximity decision.  Never blocks."""

        try:
            depth = self._bridge.imgmsg_to_cv2(
                message, desired_encoding="passthrough"
            )
            self._safety_gate.observe(depth)
        except Exception as error:
            self._log_depth_error(error)

    def _on_odom(self, message: Odometry) -> None:
        """One pose sample for the closed-loop step controller.  Never blocks."""

        pose = message.pose.pose
        # The planar yaw, using the official client's formula so a sign
        # disagreement can only ever be a frame-convention question, never a
        # difference between our odometry maths and the reference's.
        yaw = yaw_from_quaternion(pose.orientation.z, pose.orientation.w)
        self._poses.update(pose.position.x, pose.position.y, yaw, now=time.monotonic())
        if not self._odom_seen:
            self._odom_seen = True
            self.get_logger().info(
                f"odometry on {self._config.odom_topic}: pose feedback live "
                f"(servo={self._config.servo_enabled}, "
                f"closed-loop steps={self._config.odom_feedback_enabled})"
            )

    def _on_instruction(self, message: String) -> None:
        """Operator instruction from the ROS side.  Must never block."""

        instruction = str(message.data or "").strip()
        if not instruction:
            self.get_logger().warn("ignoring empty instruction")
            return
        if instruction.lower() in STOP_PAYLOADS:
            # An operator holding only `ros2 topic pub` has no other way to stop
            # the robot: publishing zeros on /cmd_vel_nav would be overwritten
            # by the next 50 Hz dispatch.  "stop" is not a usable navigation
            # goal, so reserving it costs nothing.
            result = RUNTIME.stop()
            self.get_logger().warn(f"stop requested: {result.get('detail')}")
            return
        result = RUNTIME.set_instruction(instruction)
        if result.get("accepted"):
            self.get_logger().info(f"instruction accepted: {instruction!r}")
        else:
            self.get_logger().error(f"instruction rejected: {result.get('detail')}")

    def _on_navigation_command(self, message: Twist) -> None:
        try:
            self._mux.update(SOURCE_NAVIGATION, _from_twist(message))
        except ValueError as error:
            self.get_logger().error(f"navigation raw Twist rejected: {error}")

    def _on_vln_command(self, message: Twist) -> None:
        try:
            self._mux.update(SOURCE_VLN, _from_twist(message))
        except ValueError as error:
            self.get_logger().error(f"InternVLA raw Twist rejected: {error}")

    # ------------------------------------------------------------- diagnostics

    def _log_frame_error(self, error: Exception) -> None:
        now = time.monotonic()
        if now - self._last_frame_error_log >= 1.0:
            self.get_logger().error(
                f"camera frame rejected: {type(error).__name__}: {error}"
            )
            self._last_frame_error_log = now

    def _log_depth_error(self, error: Exception) -> None:
        now = time.monotonic()
        if now - self._last_depth_error_log >= 1.0:
            self.get_logger().error(
                f"depth frame rejected ({self._config.depth_topic}): "
                f"{type(error).__name__}: {error}"
            )
            self._last_depth_error_log = now

    def _log_safety(
        self, decision: SafetyGateDecision, now: float, *, changed: bool
    ) -> None:
        # Log every transition at once; while blocked, repeat at 2 Hz so a
        # stationary robot with a wall in front of it is visibly explained
        # rather than looking hung.
        if not changed and not (
            decision.blocked and now - self._last_safety_log >= 2.0
        ):
            return
        self._last_safety_log = now
        nearest = (
            "n/a"
            if decision.nearest_m is None
            else f"{decision.nearest_m:.2f}m"
        )
        message = (
            f"proximity gate {'BLOCKED' if decision.blocked else 'clear'}: "
            f"reason={decision.reason}, nearest={nearest}, "
            f"valid_pixels={decision.valid_pixels}, "
            f"depth_age={decision.age_s if decision.age_s is None else round(decision.age_s, 3)}"
        )
        if decision.blocked:
            self.get_logger().warn(message)
        else:
            self.get_logger().info(message)

    def _on_plan(self, frame: Any, plan: Any) -> None:
        del frame
        # Runs on the inference thread, right after the plan is stored: close
        # the gate before this thread loops, so the next frame is not taken
        # until the step has been driven and the chassis has settled.
        #
        # Only a plan that actually moves the robot is gated.  A zero plan
        # (STOP, a camera-only 5/9, an unarmed client) leaves the robot where
        # it is, so the next frame is already taken from the right pose -- and
        # gating it anyway would slow STOP confirmation down to one answer per
        # step+settle cycle for no benefit.
        # A chunk is a program, so "moving" is true if *any* segment moves:
        # gating on the head alone would release the worker while the tail of
        # the chunk is still being driven.
        moving = any(
            segment.command.vx != 0.0 or segment.command.wz != 0.0
            for segment in plan.program
        ) or plan.command.vx != 0.0 or plan.command.wz != 0.0
        if (
            self._config.lockstep_enabled
            and not self._config.servo_enabled
            and self._client.armed
            and moving
        ):
            self._step_gate.clear()
        message = (
            f"InternVLA plan: vx={plan.command.vx:.3f}, "
            f"wz={plan.command.wz:.3f}, source={plan.velocity_source}"
        )
        if getattr(plan, "model_stop", False):
            # STOP is an arrival verdict, not a slow frame: say so at info so
            # the run-up to a finished task is visible without debug logging.
            self.get_logger().info(message)
        else:
            self.get_logger().debug(message)
        if self._bind_shared_runtime:
            RUNTIME.note_plan(plan)

    def _on_inference_error(self, frame: Any, error: Exception) -> None:
        del frame
        now = time.monotonic()
        if isinstance(error, ModelUnavailableError):
            # A sustained outage is different from one bad frame: the plan
            # still being executed was produced by a model that is now gone,
            # so retire it instead of letting it run out its remaining life.
            self._plans.clear()
            if now - self._last_inference_error_log >= 1.0:
                self.get_logger().error(
                    f"inference server unavailable, plan retired: {error}"
                )
                self._last_inference_error_log = now
            return
        if now - self._last_inference_error_log >= 1.0:
            self.get_logger().error(
                f"InternVLA inference failed: {type(error).__name__}: {error}"
            )
            self._last_inference_error_log = now

    def _pump_instruction(self) -> None:
        """Drain the operator handoff.  Cheap and idempotent when idle."""

        applied = RUNTIME.pump()
        if applied is None:
            return
        self.get_logger().info(f"operator action applied: {applied}")

    def _publish_status(self) -> None:
        payload = {
            "mode": self._config.mode,
            "source": self._config.selected_source,
            "plan": {
                "reason": self._plans.dispatch().reason,
            },
            # What the last dispatch tick produced after the safety gate.
            "command": self._last_command,
            "step": {
                "lockstep": self._config.lockstep_enabled,
                "reason": self._stepper.reason,
                "action_step_s": self._config.action_step_s,
                "settle_s": self._config.step_settle_s,
                "looking": self._step_gate.is_set(),
                # What will end the segment the robot is driving right now:
                # "odom" = measured, "timer" = the nominal duration.  Supplies
                # segment/segments/goal/feedback.
                **self._stepper.progress(),
            },
            "odom": {
                "enabled": self._config.odom_feedback_enabled,
                "subscribed": self._uses_odom,
                "topic": self._config.odom_topic,
                "received": self._odom_seen,
                "fresh": self._pose_fresh,
            },
            "servo": {
                "enabled": self._config.servo_enabled,
                "active": self._servo is not None,
                "done": bool(self._servo.done) if self._servo is not None else None,
                "translation_error_m": (
                    round(self._servo.translation_error_m, 3)
                    if self._servo is not None
                    else None
                ),
                "yaw_error_deg": (
                    round(math.degrees(self._servo.yaw_error_rad), 1)
                    if self._servo is not None
                    else None
                ),
                "goal": (
                    {
                        "x": round(self._servo_goal.x, 3),
                        "y": round(self._servo_goal.y, 3),
                        "yaw_deg": round(math.degrees(self._servo_goal.yaw), 1),
                    }
                    if self._servo_goal is not None
                    else None
                ),
            },
            "safety": {
                "enabled": self._config.safety_gate_enabled,
                "depth_topic": self._config.depth_topic,
                **self._safety_decision.snapshot(),
            },
            "client": self._client.snapshot(),
            "operator": RUNTIME.status(),
        }
        message = String()
        message.data = json.dumps(payload, separators=(",", ":"), default=str)
        self._status_publisher.publish(message)

    # ---------------------------------------------------------------- dispatch

    def _dispatch(self) -> None:
        now = time.monotonic()
        state = self._plans.dispatch(now=now)
        # The plan store says what the model last asked for; this tick decides
        # how to drive it: servoing toward its goal, or -- with the servo off --
        # handing it to the stop-and-go / closed-loop segment stepper.
        # `pose` is None when odometry is off, stale or was never received; the
        # servo then holds (a plan with geometry is never driven blind) and the
        # stepper falls back to its nominal-duration timing.
        pose = self._latest_pose(now=now)
        self._pose_fresh = pose is not None
        # Continuous drive.  The plan is a world-frame goal, re-aimed on every
        # answer and measured from the pose the answer landed on; this tick
        # servos toward it from the newest pose and never blocks, which is
        # exactly how the reference client's control_thread works.  A plan with
        # geometry but no pose is held rather than driven blind -- that is the
        # one case the servo refuses.
        servo = None
        if self._config.servo_enabled and state.reason == "active_plan":
            wants_geometry = bool(state.waypoints) or any(
                segment.turn_rad or segment.distance_m for segment in state.program
            )
            # A trajectory answer is driven at the cruise speed, not at the
            # chassis ceiling: the ceiling is a limit, and commanding it every
            # frame is what made the robot lunge at each new path.  A discrete
            # chunk keeps ``max_vx`` -- its geometry is what sets the speed.
            trajectory_plan = bool(state.waypoints) and not any(
                segment.turn_rad or segment.distance_m for segment in state.program
            )
            servo_max_vx = (
                self._config.trajectory_cruise_vx
                if trajectory_plan
                else self._config.max_vx
            )
            if state.goal is not None and pose is not None:
                servo = servo_command(
                    pose,
                    state.goal,
                    kp_trans=self._config.servo_kp_trans,
                    kp_yaw=self._config.servo_kp_yaw,
                    max_vx=servo_max_vx,
                    max_wz=self._config.max_wz,
                    position_tolerance_m=self._config.servo_position_tolerance_m,
                    yaw_tolerance_rad=math.radians(
                        self._config.servo_yaw_tolerance_deg
                    ),
                )
                step_command = VelocityCommand(servo.vx, servo.wz)
                step_reason = SERVO_DONE if servo.done else SERVO_RUNNING
            elif pose is None and wants_geometry:
                # A plan naming geometry (a discrete step, or a trajectory --
                # its path is geometry even though no segment carries it) with
                # no pose to measure from is held, never driven blind.
                step_command, step_reason = ZERO_COMMAND, SERVO_WAIT_ODOM
            else:
                # Nothing to aim at: a STOP, a camera-only hold, or a
                # trajectory short enough that the store read it as "stay
                # here".  These are already zero commands; the stepper holds
                # them for their duration.
                step_command, step_reason = self._stepper.apply(
                    state, now=now, pose=pose
                )
        else:
            step_command, step_reason = self._stepper.apply(state, now=now, pose=pose)
        self._servo = servo
        self._servo_goal = state.goal if servo is not None else None
        if (
            self._config.lockstep_enabled
            and not self._step_gate.is_set()
            and self._stepper.ready(now=now)
        ):
            self._step_gate.set()
        # The gate is evaluated before anything is published.  It has no idea
        # what the model asked for: if the forward band is too close, the
        # command is zero regardless.
        decision = self._safety_gate.check(now=now)
        self._safety_decision = decision
        changed = decision.blocked != self._safety_blocked_last
        self._safety_blocked_last = decision.blocked
        if self._bind_shared_runtime:
            # Tell the capability, and on the closing edge stop the primitive
            # for real: retiring the plan zeroes the velocity, disarming the
            # client is what makes the worker stop asking the model.  Dry-run
            # is included on purpose -- a held robot that keeps streaming
            # /eval_dual requests is the behaviour this is meant to remove.
            # Called every tick so the reported distance stays fresh; the
            # runtime only acts when the verdict flips.
            action = RUNTIME.set_safety_state(
                blocked=decision.blocked,
                reason=decision.reason,
                nearest_m=decision.nearest_m,
                stop_distance_m=decision.stop_distance_m,
            )
            if action is not None:
                message = f"operator state: {action.get('detail')}"
                if decision.blocked:
                    self.get_logger().warn(message)
                else:
                    self.get_logger().info(message)
        self._log_safety(decision, now, changed=changed)

        command = ZERO_COMMAND if decision.blocked else step_command
        reason = f"safety:{decision.reason}" if decision.blocked else step_reason
        self._last_command = {
            "vx": round(command.vx, 3),
            "wz": round(command.wz, 3),
            "reason": reason,
            "blocked": bool(decision.blocked),
            # False in dry-run: the value is what *would* have been published,
            # so a dry-run status never reads as if its wheels had moved.
            "published": self._config.mode != "dry-run",
        }

        if self._config.mode == "dry-run":
            self._client.set_executed_command(ZERO_COMMAND)
            if now - self._last_dispatch_log >= 1.0:
                nearest = (
                    "n/a"
                    if decision.nearest_m is None
                    else f"{decision.nearest_m:.2f}m"
                )
                age = (
                    "n/a"
                    if state.age_s is None
                    else f"{state.age_s:.3f}s"
                )
                self.get_logger().info(
                    "dry-run prediction: "
                    f"vx={step_command.vx:.3f}, wz={step_command.wz:.3f}, "
                    f"state={reason}, "
                    f"age={age}, "
                    f"gate={'blocked' if decision.blocked else 'clear'} "
                    f"({decision.reason}, nearest={nearest}), "
                    f"episode={self._client.episode}"
                )
                self._last_dispatch_log = now
            return

        assert self._raw_publisher is not None
        assert self._selected_publisher is not None
        self._raw_publisher.publish(_velocity_twist(command))
        self._client.set_executed_command(command)
        # Feed the locally generated command into the mux epoch immediately.
        # The self-subscription still verifies the configured raw topic, while
        # this avoids a one-timer-period delay before a fresh plan or a zero
        # reaches the output.
        self._mux.update(
            SOURCE_VLN,
            TwistCommand(linear_x=command.vx, angular_z=command.wz),
            received_monotonic=now,
        )
        selection = self._mux.output(now=now)
        self._selected_publisher.publish(_full_twist(selection.command))

    # ---------------------------------------------------------------- teardown

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._bind_shared_runtime:
            RUNTIME.unbind()
        self._plans.clear()
        if self._raw_publisher is not None:
            self._raw_publisher.publish(Twist())
        if self._selected_publisher is not None:
            self._selected_publisher.publish(Twist())
        self._client.set_executed_command(ZERO_COMMAND)
        # Interrupt an in-flight request before joining the worker, so provider
        # deactivation is not delayed until the full HTTP timeout expires.
        self._client.close()
        self._worker.stop(timeout_s=2.0)

    def destroy_node(self) -> bool:
        self.close()
        return super().destroy_node()


def run_ros_runtime(
    config: Mapping[str, Any],
    stop_event: threading.Event,
    ready_event: threading.Event,
    errors: MutableSequence[str],
) -> None:
    """Run one provider-owned ROS context until lifecycle deactivation."""

    context = Context()
    node: InternVLANode | None = None
    guard: RangerChassisGuardNode | None = None
    executor: SingleThreadedExecutor | None = None
    try:
        rclpy.init(args=[], context=context)
        node = InternVLANode(config_overrides=config, context=context)
        # Dry-run publishes no Twist at all, so the guard would only add a 50 Hz
        # stream of zeros on /cmd_vel.  Start it exactly when the VLN side is
        # allowed to command motion.
        if RuntimeConfig.from_mapping(config).mode == "live":
            guard = RangerChassisGuardNode(
                config_overrides=config,
                context=context,
            )
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        if guard is not None:
            executor.add_node(guard)
        ready_event.set()
        while context.ok() and not stop_event.is_set():
            executor.spin_once(timeout_sec=0.1)
    except Exception as error:
        errors.append(f"{type(error).__name__}: {error}")
        ready_event.set()
    finally:
        nodes = [entry for entry in (node, guard) if entry is not None]
        if executor is not None:
            for entry in nodes:
                try:
                    executor.remove_node(entry)
                except Exception:
                    pass
        for entry in nodes:
            entry.destroy_node()
        if context.ok():
            context.shutdown()


def main(args: Sequence[str] | None = None) -> None:
    rclpy.init(args=args)
    node = InternVLANode()
    nodes: list[Node] = [node]
    if node.runtime_config.mode == "live":
        nodes.append(RangerChassisGuardNode())
    executor = SingleThreadedExecutor()
    for entry in nodes:
        executor.add_node(entry)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        for entry in nodes:
            executor.remove_node(entry)
            entry.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
