"""Odometry feedback, so a model step can end when it is *done*, not when time is up.

Why this exists
---------------
Both reference deployments execute a DualVLN step closed loop: the official
Go2 client turns every action into an odometry-referenced goal pose and runs a
PID against it at 10 Hz (``http_internvla_client.py``: ``incremental_change_goal``
+ ``PID_controller``), and the deployment document's own client does a blocking
chassis move and reports back when the target is reached.  Neither of them
drives "forward 0.25 m" as a fixed duration.

This stack used to be the exception.  ``discrete_to_velocity`` computed the
speed that covers 0.25 m / 15 deg in ``action_step_s`` and held it for exactly
that long -- open loop.  Measured on this Ranger, a nominal 0.25 m step covers
about 0.20 m (see ``docs/CHANGELOG.md``), and the error is not constant: it
depends on floor grip, the chassis' own ramp, and how far the previous segment
coasted.  Left to accumulate over a task, it is the difference between stopping
in front of the target and stopping at it, and it is what makes "turned 15
degrees" unsafe to assume when the next frame is interpreted.

What this module provides
-------------------------
Only the geometry: a pose buffer fed by ``/odom`` and the predicates that say
"this segment's goal has been reached from that start pose".  It has no ROS
import and no state beyond the latest sample, so the whole thing is testable
without a chassis -- and the step controller can fall back to its old
time-based behaviour by simply passing ``pose=None`` when no odometry is
arriving.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import Lock


def wrap_angle(angle: float) -> float:
    """Fold an angle into ``(-pi, pi]``."""

    if not math.isfinite(angle):
        return 0.0
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def yaw_from_quaternion(z: float, w: float) -> float:
    """Heading of a planar quaternion, the same formula the official client uses.

    ``atan2(2zw, 1 - 2z^2)`` instead of the textbook
    ``atan2(2(wz + xy), 1 - 2(y^2 + z^2))``: under the usual no-roll/no-pitch
    assumption the two agree, and keeping the reference formula means a sign
    disagreement can never be "our odometry function" rather than the frame.
    """

    return math.atan2(2.0 * z * w, 1.0 - 2.0 * z * z)


@dataclass(frozen=True)
class Pose:
    """One planar pose sample, stamped with the monotonic clock."""

    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    received_at: float = 0.0


class PoseBuffer:
    """Thread-safe "latest pose", with a staleness window.

    The ROS callback writes from the executor thread, the dispatcher reads from
    its own tick: both go through the lock.  ``latest`` returns ``None`` for a
    frame older than ``max_age_s``, which is what makes a dead odometry topic
    degrade into the old open-loop timing instead of freezing the robot on a
    goal it can no longer measure.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._pose: Pose | None = None

    def update(self, x: float, y: float, yaw: float, *, now: float) -> None:
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(yaw)):
            return
        with self._lock:
            self._pose = Pose(float(x), float(y), wrap_angle(yaw), float(now))

    def latest(self, *, max_age_s: float, now: float) -> Pose | None:
        with self._lock:
            pose = self._pose
        if pose is None:
            return None
        if max_age_s > 0.0 and now - pose.received_at > max_age_s:
            return None
        return pose

    @property
    def ever_seen(self) -> bool:
        with self._lock:
            return self._pose is not None


def turn_progress(start: Pose, current: Pose, target_rad: float) -> float:
    """Signed heading change since ``start``, in radians.

    Wrapped, so a turn that crosses +-pi is measured as the short way round.
    """

    if not math.isfinite(target_rad):
        return 0.0
    return wrap_angle(current.yaw - start.yaw)


def distance_progress(start: Pose, current: Pose) -> float:
    """How far the chassis has moved along ``start``'s heading.

    Projected onto the start heading rather than measured as a straight-line
    displacement: while a turn is still finishing, sideways motion must not
    count as forward progress toward a "drive 0.25 m" goal.
    """

    dx = current.x - start.x
    dy = current.y - start.y
    return math.cos(start.yaw) * dx + math.sin(start.yaw) * dy


def turn_reached(
    start: Pose,
    current: Pose,
    target_rad: float,
    *,
    tolerance_rad: float,
) -> bool:
    """Has the chassis turned at least ``target_rad`` in the asked direction?"""

    delta = turn_progress(start, current, target_rad)
    if target_rad >= 0.0:
        return delta >= target_rad - abs(tolerance_rad)
    return delta <= target_rad + abs(tolerance_rad)


def distance_reached(
    start: Pose,
    current: Pose,
    target_m: float,
    *,
    tolerance_m: float,
) -> bool:
    """Has the chassis advanced at least ``target_m`` along its start heading?"""

    return distance_progress(start, current) >= target_m - abs(tolerance_m)


# --------------------------------------------------------------- continuous drive
#
# The reference client does not hold a velocity for a computed duration and
# then stop: it keeps one goal pose per answer and *servos* toward it from a
# 10 Hz control thread that never blocks (``http_internvla_client.py``,
# ``control_thread`` + ``controllers.PID_controller``), while a second thread
# asks the model and merely moves the goal post.  That decoupling is why its
# motion is continuous and ours was stop-and-go: ours had the request inside
# the execution loop.
#
# The two functions below are that control law, reduced to its proportional
# term (the reference's derivative terms are configured to zero).


@dataclass(frozen=True)
class ServoCommand:
    """One 50 Hz tick of the continuous controller."""

    vx: float = 0.0
    wz: float = 0.0
    done: bool = False
    translation_error_m: float = 0.0
    yaw_error_rad: float = 0.0


def advance_pose(
    pose: Pose,
    *,
    distance_m: float = 0.0,
    turn_rad: float = 0.0,
) -> Pose:
    """The pose a goal lands on after one model action, taken from ``pose``.

    Same arithmetic as the reference's ``incremental_change_goal``: a forward
    step moves along the *goal's own* heading, so a chunk is applied in order
    and ``[1, 2]`` means "forward, then turn left", never "turn, then forward".
    """

    if turn_rad:
        return Pose(
            pose.x, pose.y, wrap_angle(pose.yaw + turn_rad), pose.received_at
        )
    if distance_m:
        return Pose(
            pose.x + distance_m * math.cos(pose.yaw),
            pose.y + distance_m * math.sin(pose.yaw),
            pose.yaw,
            pose.received_at,
        )
    return pose


def servo_command(
    pose: Pose,
    goal: Pose,
    *,
    kp_trans: float,
    kp_yaw: float,
    max_vx: float,
    max_wz: float,
    position_tolerance_m: float,
    yaw_tolerance_rad: float,
) -> ServoCommand:
    """Velocity toward ``goal`` from ``pose``, as the reference computes it.

    ``translation_error`` is the goal offset projected onto the *robot's*
    heading and ``yaw_error`` is the wrapped heading difference -- exactly
    ``PID_controller.calculate_errors``.  Forward only: a goal the robot has
    already driven past yields ``vx = 0`` rather than a reverse command, the
    same clamp the reference applies (``if v < 0.0: v = 0.0``), so overshoot
    stops instead of backing up.

    A ``done`` tick means the goal is inside tolerance: publish zero and wait
    for the next answer.  That is the only place this controller can stop.
    """

    translation = (goal.x - pose.x) * math.cos(pose.yaw) + (
        goal.y - pose.y
    ) * math.sin(pose.yaw)
    yaw_error = wrap_angle(goal.yaw - pose.yaw)
    if (
        abs(translation) <= abs(position_tolerance_m)
        and abs(yaw_error) <= abs(yaw_tolerance_rad)
    ):
        return ServoCommand(0.0, 0.0, True, translation, yaw_error)

    vx = kp_trans * max(-1.0, min(1.0, translation))
    wz = kp_yaw * max(-1.0, min(1.0, yaw_error))
    return ServoCommand(
        vx=max(0.0, min(float(max_vx), vx)),
        wz=max(-float(max_wz), min(float(max_wz), wz)),
        done=False,
        translation_error_m=translation,
        yaw_error_rad=yaw_error,
    )
