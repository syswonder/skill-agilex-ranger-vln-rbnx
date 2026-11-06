"""ROS-independent chassis command policy for the Ranger base.

Why this exists
---------------
The Go2 deployment never needed a guard here, because three classes of
protection lived inside ``go2_chassis`` and inside the Go2 sport-mode service
it wraps:

* a fail-closed motion gate (``allow_motion``/``safety_ack``/``allowed_modes``),
* chassis-side clamps (0.25 m/s, 0.40 rad/s), a 0.25 s command timeout and a
  0.50 s zero preamble,
* a sport-mode call that translates and rotates at the same time and ignores
  every other Twist field.

The Ranger chassis primitive has none of them.  Its
``RangerROSMessenger::TwistCmdCallback`` instead does two things the upstream
follow controller does not expect:

1. It computes ``radius = |linear.x| / |angular.z|`` and, when that falls
   below ``min_turn_radius``, switches to ``MOTION_MODE_SPINNING`` and calls
   ``SetMotionCommand(0.0, 0.0, angular.z)`` -- **linear.x is discarded
   entirely**.  A follow controller asking for a tight curve gets a robot that
   stops advancing and pivots instead.
2. It switches the whole 4WS steering geometry (PARALLEL / SIDE_SLIP) the
   moment ``linear.y`` is non-zero.

On top of that the Ranger accepts up to 1.5 m/s where Go2 never exceeded
0.25 m/s, and it accepts negative ``linear.x`` (reverse) where Go2 rejected it.

What the policy does
--------------------
It restores the Go2 command contract on top of the Ranger's: forward-only,
clamped, rate-limited, with a command timeout, ``linear.y`` pinned to zero, and
a turn-radius constraint so a command can never silently lose its linear
component.

The turn-radius constraint is the one piece with no Go2 equivalent, because on
Go2 the situation it prevents cannot arise.  Three policies are available:

``clamp_yaw`` (default)
    Reduce ``|angular.z|`` until the pair sits exactly at ``min_turn_radius``.
    The robot keeps translating and arcs gently.  This reproduces the Go2
    "translate and rotate simultaneously" semantics, which is what the follow
    controller was tuned against, at the cost of under-turning on requests the
    chassis physically cannot execute as a curve.
``allow_spin``
    Pass the pair through unchanged and let the chassis pivot in place.  Useful
    when the person steps to the side and facing them matters more than
    approaching them, but it is exactly the behaviour that silently dropped
    ``linear.x`` before.
``block``
    Refuse the command outright (publish zero).  The most conservative option.

Genuine in-place rotation (``|linear.x|`` at or below ``spin_vx_epsilon``)
always passes through unclamped, because there the chassis's SPINNING mode is
the correct response rather than an accident.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ShapedCommand:
    """One guarded command plus the reason it differs from the request."""

    vx: float = 0.0
    wz: float = 0.0
    note: str = ""


def rate_limit(previous: float, target: float, max_rate: float, dt: float) -> float:
    """Move ``previous`` toward ``target`` by at most ``max_rate * dt``."""

    if not math.isfinite(previous) or not math.isfinite(target):
        return 0.0
    if not math.isfinite(max_rate) or max_rate <= 0.0:
        # A non-positive rate disables the limiter rather than freezing it.
        return target
    if dt <= 0.0:
        # No elapsed time means no motion is permitted yet.  Returning the
        # target here would let the very first tick jump straight to full
        # speed, defeating the limiter exactly when it matters most.
        return previous
    step = max_rate * dt
    delta = target - previous
    if delta > step:
        return previous + step
    if delta < -step:
        return previous - step
    return target


def shape_command(
    vx: float,
    wz: float,
    *,
    forward_only: bool = True,
    max_vx: float = 0.25,
    max_wz: float = 0.40,
    spin_vx_epsilon: float = 0.02,
    min_turn_radius: float = 0.81,
    turn_policy: str = "clamp_yaw",
) -> ShapedCommand:
    """Apply the Go2 command contract plus the Ranger turn-radius constraint.

    Pure function: no ROS, no clock, no state, so the whole policy is unit
    testable without a running chassis.
    """

    if not (math.isfinite(vx) and math.isfinite(wz)):
        return ShapedCommand(0.0, 0.0, "rejected non-finite command")

    note = ""
    if forward_only and vx < 0.0:
        # go2_chassis rejected negative linear.x outright; the Ranger Ackermann
        # path would happily drive in reverse.
        vx = 0.0
        note = "clamped negative linear.x (forward-only contract)"

    clamped_vx = max(-max_vx, min(max_vx, vx))
    if clamped_vx != vx:
        note = f"clamped linear.x to {clamped_vx:.3f} m/s"
        vx = clamped_vx
    clamped_wz = max(-max_wz, min(max_wz, wz))
    if clamped_wz != wz:
        note = f"clamped angular.z to {clamped_wz:.3f} rad/s"
        wz = clamped_wz

    # Genuine in-place rotation is the chassis's correct SPINNING response, not
    # an accident, so it is never constrained by the turn radius.
    if abs(vx) > spin_vx_epsilon and abs(wz) > 0.0:
        radius = abs(vx) / abs(wz)
        if radius < min_turn_radius:
            if turn_policy == "block":
                return ShapedCommand(
                    0.0,
                    0.0,
                    f"blocked command needing radius {radius:.3f} m "
                    f"< {min_turn_radius:.3f} m",
                )
            if turn_policy == "clamp_yaw":
                wz = math.copysign(abs(vx) / min_turn_radius, wz)
                note = (
                    f"yaw clamped to {wz:.3f} rad/s to hold radius "
                    f"{min_turn_radius:.3f} m (requested {radius:.3f} m)"
                )
            # allow_spin: pass through and let the chassis pivot.
    return ShapedCommand(vx, wz, note)
