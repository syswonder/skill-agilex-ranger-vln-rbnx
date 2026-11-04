"""InternVLA-N1 DualVLN output -> Ranger velocity command.

This is the only place on the robot that knows what a model action *means*.
Everything above it deals in :class:`VelocityCommand` and nothing else, so the
whole translation is three pure functions plus one payload dispatcher and can
be re-tuned without touching ROS, HTTP or the chassis guard.

The model answers in one of two shapes (see the DualVLN server):

    {"discrete_action": [1]}                        one high-level primitive
    {"trajectory": [[x, y], ...], "pixel_goal": ...} a short open-loop path

`discrete_action` wins when both are present, because an explicit decision is
strictly more informative than a path that the server may have produced as a
by-product.

Action meanings are the ones the DualVLN training set defines:

    0  STOP
    1  forward, about 0.25 m
    2  turn left, about 15 degrees
    3  turn right, about 15 degrees
    5  look down        (camera pitch -- this chassis has no such joint)
    9  camera action    (ditto)

Actions 5 and 9 are *no-ops*, not errors.  The model is allowed to ask for a
degree of freedom the Ranger does not have; the correct answer is to stand
still and let the next frame re-plan, not to fault the whole run.

``discrete_action`` is normally a *list* of up to four steps, not one: the
model answers with an action chunk ("↑↑↑↑" in its text), and the server turns
every arrow it finds into one element
(``internvla_n1_agent_realworld.py``, ``parse_actions``).  The reference Go2
skill reads that list as an ordered sequence and executes **every** element
before taking the next picture (``skill-internvla-navigation-rbnx``,
``discrete_to_commands``; so does the deployment client's
``incremental_change_goal``, which walks the list adding 0.25 m / 15 deg per
element to its goal pose).

Until 2026-10-30 this robot executed only the first element
(``max_chunk_actions`` = 1), because a step used to be blind travel and four of
them meant a metre of it per decision.  That reasoning stopped holding when
``odom_feedback_enabled`` gave every element a geometry (15 deg / 0.25 m) that
the step controller and the servo both end on measured odometry -- and the live
capture showed the cost: the model asked for ``[3, 3, 3, 3]`` (~60 deg), got
15 deg, was re-photographed, and asked again.  The cap is now 4, which is what
both reference deployments do (the Go2 skill's ``for command in commands:
_chassis.move(**command)`` and the official client's ``incremental_change_goal``).

* 0, 5 and 9 inside a list are skipped -- a leading 0 is "no step here", not
  "arrived", so reading only ``[0]`` used to brake the robot on the spot for a
  frame it should have driven through;
* the cap (``max_chunk_actions``, 4 by default) decides *how many* elements of
  the chunk one answer may commit to, never *which order*;
* a list that is *all* zeros means the task is done;
* a list of nothing but 5/9 is the model asking for a camera degree of freedom
  this chassis lacks -- stand still and re-plan, which costs one plan hold, not
  an episode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .core import (
    CONTROL_DT,
    InferencePlan,
    ProgramSegment,
    ProtocolError,
    RuntimeConfig,
    VelocityCommand,
    _finite_float,
)


DISCRETE_STOP = 0
DISCRETE_FORWARD = 1
DISCRETE_LEFT = 2
DISCRETE_RIGHT = 3
DISCRETE_LOOK_DOWN = 5
DISCRETE_CAMERA = 9

# Chassis degrees of freedom this model may request but the Ranger lacks.
UNSUPPORTED_DISCRETE_ACTIONS = (DISCRETE_LOOK_DOWN, DISCRETE_CAMERA)


@dataclass(frozen=True)
class ActionStep:
    """A velocity command, how long to hold it, and why the log says so.

    ``duration_s`` is derived from the step's own geometry, not fixed: a step
    that has to run below its nominal speed because the chassis cap is in the
    way takes *longer* instead of covering less ground.  That is the
    difference between "the robot moved 0.25 m" and "the robot was told 0.25 m
    and moved 0.15 m", and it is why the distance the model asked for is the
    distance the robot actually covers.
    """

    command: VelocityCommand
    note: str
    clipped: bool = False
    duration_s: float = 0.0
    # The geometry this step is *for*, so the step controller can end it when
    # odometry says the chassis actually achieved it instead of when the timer
    # says its nominal duration is over.  Zero = no goal of that kind (stop,
    # camera hold, a trajectory segment, which is followed rather than aimed).
    turn_rad: float = 0.0
    distance_m: float = 0.0
    # True when this step *is* the model saying "the goal is reached" -- today
    # only a trajectory too short to be a move (see ``trajectory_stop_m``).
    # The runtime counts consecutive STOPs to end a task, and a trajectory
    # answer that says "stay here" has to count, or a model that only emits
    # trajectories can never finish.
    model_stop: bool = False


def _scale_to_limits(
    vx: float, wz: float, max_vx: float, max_wz: float
) -> tuple[float, float, float]:
    """Scale a (vx, wz) pair down uniformly until it fits the caps.

    Uniform scaling keeps the *shape* of the motion -- an arc keeps its radius,
    a pure turn stays a pure turn -- and only changes how fast it is driven.
    Clipping each axis on its own (what this used to do) shortened the arc and
    left the yaw alone, so the robot ended up more turned than the path asked
    for.  Returns the new pair plus the factor applied (1.0 = nothing to do).
    """

    scale = 1.0
    if max_vx > 0.0 and abs(vx) > max_vx:
        scale = min(scale, max_vx / abs(vx))
    if max_wz > 0.0 and abs(wz) > max_wz:
        scale = min(scale, max_wz / abs(wz))
    return vx * scale, wz * scale, scale


def _annotate(note: str, clipped: bool) -> str:
    return f"{note} (held longer: chassis speed cap)" if clipped else note


def discrete_to_velocity(
    action: int,
    *,
    step_s: float,
    discrete_forward_m: float = 0.25,
    discrete_step_deg: float = 15.0,
    max_vx: float = 0.15,
    max_wz: float = 0.30,
) -> ActionStep:
    """Convert one DualVLN discrete action into one timed velocity command.

    ``step_s`` is the *nominal* time the model's step is meant to take.  Both
    reference deployments execute an action as a closed-loop chassis move --
    "forward 0.25 m", "rotate 15 degrees" -- and report back when the target is
    reached; this adapter has no closed loop, so it commands the velocity that
    covers that geometry in ``step_s`` and keeps the command up for as long as
    the speed cap forces it to.  The robot therefore travels
    ``discrete_forward_m`` and turns ``discrete_step_deg`` whatever the caps
    are, instead of travelling a shorter distance in a fixed time.

    Facing a turn, ``vx`` is exactly zero.  That is deliberate: the Ranger
    controller treats a zero-linear command as an in-place pivot, which is what
    "rotate 15 degrees" should be.
    """

    hold = _finite_float(step_s, "step_s")
    if hold <= 0.0:
        raise ProtocolError("step_s must be positive")
    if not math.isfinite(discrete_forward_m) or discrete_forward_m <= 0.0:
        raise ProtocolError("discrete_forward_m must be positive")
    if not math.isfinite(discrete_step_deg) or discrete_step_deg <= 0.0:
        raise ProtocolError("discrete_step_deg must be positive")

    if action == DISCRETE_STOP:
        return ActionStep(VelocityCommand(0.0, 0.0), "stop", duration_s=hold)

    if action == DISCRETE_FORWARD:
        wanted = discrete_forward_m / hold
        command_vx, _, _ = _scale_to_limits(wanted, 0.0, max_vx, max_wz)
        clipped = command_vx < wanted
        duration = discrete_forward_m / command_vx
        return ActionStep(
            VelocityCommand(command_vx, 0.0),
            _annotate(
                f"forward {discrete_forward_m:.3f} m over {duration:.2f} s", clipped
            ),
            clipped,
            duration,
            distance_m=discrete_forward_m,
        )

    if action in (DISCRETE_LEFT, DISCRETE_RIGHT):
        sign = 1.0 if action == DISCRETE_LEFT else -1.0
        side = "left" if action == DISCRETE_LEFT else "right"
        wanted = sign * math.radians(discrete_step_deg) / hold
        _, command_wz, _ = _scale_to_limits(0.0, wanted, max_vx, max_wz)
        clipped = abs(command_wz) < abs(wanted)
        duration = math.radians(discrete_step_deg) / abs(command_wz)
        return ActionStep(
            VelocityCommand(0.0, command_wz),
            _annotate(
                f"turn {side} {discrete_step_deg:.1f} deg over {duration:.2f} s",
                clipped,
            ),
            clipped,
            duration,
            turn_rad=sign * math.radians(discrete_step_deg),
        )

    if action in UNSUPPORTED_DISCRETE_ACTIONS:
        # Correct behaviour, not a failure: hold position and let the next
        # frame produce an action this chassis can execute.
        return ActionStep(
            VelocityCommand(0.0, 0.0), f"no-op action {action}", duration_s=hold
        )

    raise ProtocolError(f"unknown discrete_action {action}")


VALID_DISCRETE_ACTIONS = (
    DISCRETE_STOP,
    DISCRETE_FORWARD,
    DISCRETE_LEFT,
    DISCRETE_RIGHT,
    *UNSUPPORTED_DISCRETE_ACTIONS,
)


def executable_actions(actions: Sequence[int], *, limit: int | None = None) -> tuple[int, ...]:
    """Return the chassis actions in a plan list, in the order the model gave.

    The reference skill's ``discrete_to_commands`` drops 0/5/9 and keeps the
    rest *in order*; its caller then drives every one of them before taking
    the next picture (``for command in commands: _chassis.move(**command)``).
    The official client does the same thing in pose space: it walks the list
    and adds 0.25 m or 15 degrees per element to the goal
    (``incremental_change_goal``).  So the list is one committed chunk, and an
    answer of ``[1, 1, 1, 1]`` means "a metre", not "a quarter of a metre".

    How much of that chunk this robot drives is the caller's decision, made
    through ``limit`` (``max_chunk_actions``, default **1**): the reference
    chassis primitives close the loop on distance/pose per step, this one does
    not, so a step is blind travel and the default is one step per decision.
    Truncation always cuts from the tail -- the order is the model's.
    Unknown codes anywhere in the list are a protocol error, whether or not
    the cap would have reached them: a code this chassis was never taught is
    contract drift and makes the whole answer suspect.
    """

    for action in actions:
        if action not in VALID_DISCRETE_ACTIONS:
            raise ProtocolError(f"unknown discrete_action {action}")

    kept = tuple(
        action
        for action in actions
        if action not in (DISCRETE_STOP, *UNSUPPORTED_DISCRETE_ACTIONS)
    )
    if limit is not None and limit > 0:
        return kept[:limit]
    return kept


def select_discrete_action(actions: Sequence[int]) -> int | None:
    """The head of the chunk, or ``None`` when there is no chassis action.

    Kept because the log and the status line still want one symbolic answer
    ("what is it doing right now"), and because the camera-only / STOP / wait
    distinctions are made from it.  Execution uses :func:`executable_actions`.
    """

    kept = executable_actions(actions)
    if kept:
        return kept[0]
    if all(action == DISCRETE_STOP for action in actions):
        return DISCRETE_STOP
    return None


def _describe_actions(actions: Sequence[int], steps: Sequence[ActionStep]) -> str:
    """Log prefix: the model's list, then what each element turned into."""

    shown = ", ".join(str(action) for action in actions)
    if not steps:
        return f"discrete_action[{shown}]"
    total = sum(step.duration_s for step in steps)
    moves = " + ".join(step.note for step in steps)
    return (
        f"discrete_action[{shown}] -> {len(steps)} step(s) over {total:.2f} s: "
        f"{moves}"
    )


def _parse_trajectory(value: Any) -> tuple[tuple[float, ...], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProtocolError("trajectory must be an array of points")
    points: list[tuple[float, ...]] = []
    for index, point in enumerate(value):
        if isinstance(point, (str, bytes)) or not isinstance(point, Sequence):
            raise ProtocolError(f"trajectory[{index}] must be an array of numbers")
        if len(point) < 2:
            raise ProtocolError(f"trajectory[{index}] must carry at least [x, y]")
        try:
            coordinates = tuple(
                _finite_float(item, f"trajectory[{index}][{position}]")
                for position, item in enumerate(point)
            )
        except (TypeError, ValueError) as error:
            raise ProtocolError(str(error)) from error
        points.append(coordinates)
    if not points:
        raise ProtocolError("trajectory must not be empty")
    return tuple(points)


def trajectory_to_velocity(
    trajectory: Sequence[Sequence[float]],
    *,
    step_s: float,
    lookahead_m: float = 1.00,
    min_turn_radius_m: float = 0.81,
    max_vx: float = 0.15,
    max_wz: float = 0.30,
    stop_m: float = 0.05,
    cruise_vx: float | None = None,
) -> ActionStep:
    """Convert an ``[x, y]`` trajectory into one held velocity command.

    The controller pursues the first point at least ``lookahead_m`` away
    (falling back to the final point when the whole path is shorter than that),
    and commands the single circular arc through it -- textbook pure pursuit::

        w = 2 * v * sin(bearing) / L,   L = distance to the pursued point

    which implies a radius of ``L / (2 sin(bearing))``, exactly the circle the
    point lies on.  The robot therefore drives the model's own curve instead of
    driving straight and rounding the steering off to nothing.

    What this replaced: ``w = bearing / hold``, i.e. an implied radius of
    ``advance / bearing``.  That is not a curvature estimate -- written out, it
    equals the model's radius only when the pursued point sits *twice* the
    advance away, and comes out at **double** the model's radius when it sits
    exactly one lookahead away, which is the normal case.  Measured on a 2.86 m
    arc from the live output shape: the old rule commanded a 5.73 m radius
    (half the curvature), the pure-pursuit form commands 3.8 m and converges
    onto 2.86 m as the path is sampled further out.  The old rule was also
    wildly sensitive to *which* point got picked (2.86 m to 5.73 m over one
    lookahead's worth of variation), which is the giveaway that it was not
    estimating curvature at all.

    There is still deliberately **no gain and no turn threshold**: a path that
    leans three degrees gets the yaw that a three-degree lean implies, and a
    perfectly straight path yields exactly zero yaw -- because its bearing
    really is zero, not because it fell under a threshold.  Amplifying the
    bearing (the ``trajectory_yaw_gain`` experiments of 2026-10-22) turned
    gentle paths into swerves and was reverted.  The bearing is clamped to
    +/-90 degrees: past a right angle the point is behind the robot and the
    next frame re-plans.

    The reference deployments look *further* ahead than they travel in one
    tick, which is what the ``L`` in this formula does: the official MPC takes
    reference points 0.12 m apart over a 0.72 m horizon while moving ~0.3 m
    before the next frame; the Go2 skill re-walks the whole path at 0.25 m per
    step.  Steering by a point at or beyond the advance (rather than by the
    advance itself) is the same idea.

    ``min_turn_radius_m`` is the chassis's own limit, and it picks between the
    two honest ways to obey the model:

    * the arc is drivable (radius at least the minimum) -> drive the arc;
    * tighter than that -> the Ranger would silently drop ``linear.x``
      (``RangerROSMessenger::TwistCmdCallback``) and turn the commanded arc
      into an unasked-for pivot, so the pivot is commanded openly instead, and
      the next frame re-plans from the new heading.

    Trajectory frame: metres, +x forward, +y left, so a positive bearing is a
    left turn, matching ROS REP-103 and the chassis's own convention.
    """

    hold = _finite_float(step_s, "step_s")
    if hold <= 0.0:
        raise ProtocolError("step_s must be positive")

    radius_limit = _finite_float(min_turn_radius_m, "min_turn_radius_m")
    if radius_limit <= 0.0:
        raise ProtocolError("min_turn_radius_m must be positive")

    stop_radius = _finite_float(stop_m, "stop_m")
    if stop_radius < 0.0:
        raise ProtocolError("stop_m must not be negative")

    points = _parse_trajectory(trajectory)

    target = points[-1]
    for point in points:
        if math.hypot(point[0], point[1]) >= lookahead_m:
            target = point
            break

    x, y = target[0], target[1]
    distance = math.hypot(x, y)
    extent = math.hypot(points[-1][0], points[-1][1])
    # "Stay here."  A path that ends closer than the servo's own arrival box
    # asks for no motion at all -- and saying so (model_stop) is what lets the
    # runtime finish a task from trajectory answers, which otherwise can never
    # end one.  This used to be a 1e-6 guard, so a 2 cm path was driven as a
    # held 2 cm/s creep that never arrives anywhere.
    if distance <= stop_radius:
        return ActionStep(
            VelocityCommand(0.0, 0.0),
            f"trajectory {len(points)} points, extent {extent:.2f} m: "
            f"nothing to drive, holding (arrived)",
            duration_s=hold,
            model_stop=True,
        )

    bearing_deg = math.degrees(math.atan2(y, x))
    # Pure pursuit: the bearing is clamped to a right angle; past that the
    # point is behind the robot and the next frame re-plans.
    steered_deg = max(-90.0, min(90.0, bearing_deg))
    advance = min(lookahead_m, distance)

    # w = 2 v sin(bearing) / L, L = the distance to the point actually pursued.
    # Scale the pair *uniformly* to fit the caps, then stretch the hold by the
    # same factor: the arc the model drew is still the arc the robot drives,
    # it just takes longer.  Clipping each axis separately shortened the
    # advance while leaving the full bearing in place, which is over-turning.
    # The commanded speed used to be ``advance / hold`` alone: with a one-metre
    # lookahead and a one-second step that is 1.0 m/s against a 0.25 m/s
    # chassis, so the pair was clipped on every single answer and the hold was
    # stretched to four seconds.  A ``cruise_vx`` below the chassis cap keeps
    # one answer to one answer's worth of travel instead of one met of travel:
    # the robot still drives the arc's own curvature (both components scale
    # together, so the radius is untouched), just at a speed that does not
    # require it to be cut short.
    wanted_vx = advance / hold
    if cruise_vx is not None:
        cruise = _finite_float(cruise_vx, "cruise_vx")
        if cruise > 0.0:
            wanted_vx = min(wanted_vx, cruise)
    arc_vx, arc_wz, scale = _scale_to_limits(
        wanted_vx,
        2.0 * wanted_vx * math.sin(math.radians(steered_deg)) / distance,
        max_vx,
        max_wz,
    )
    duration = hold / scale

    if arc_vx >= abs(arc_wz) * radius_limit:
        shape = f"arc radius {arc_vx / abs(arc_wz):.2f} m" if arc_wz else "straight"
        return ActionStep(
            VelocityCommand(arc_vx, arc_wz),
            _annotate(
                f"trajectory bearing {bearing_deg:+.1f} deg, "
                f"forward {advance:.3f} m "
                f"over {duration:.2f} s ({shape}), "
                f"{len(points)} points, extent {extent:.2f} m",
                scale < 1.0,
            ),
            scale < 1.0,
            duration,
        )

    # Tighter than the chassis can drive: pivot in place through the whole
    # bearing at whatever rate the yaw cap allows.  (Not pure pursuit -- there
    # is no arc here, the goal is simply to end up facing the point.)
    _, pivot_wz, pivot_scale = _scale_to_limits(
        0.0, math.radians(steered_deg) / hold, max_vx, max_wz
    )
    pivot_duration = hold / pivot_scale
    return ActionStep(
        VelocityCommand(0.0, pivot_wz),
        _annotate(
            f"trajectory bearing {bearing_deg:+.1f} deg over "
            f"{pivot_duration:.2f} s, tighter "
            f"than the chassis minimum turn radius {radius_limit:.2f} m: pivoting, "
            f"{len(points)} points, extent {extent:.2f} m",
            pivot_scale < 1.0,
        ),
        pivot_scale < 1.0,
        pivot_duration,
    )


def parse_plan(
    response: Mapping[str, Any],
    *,
    config: RuntimeConfig,
) -> InferencePlan:
    """Turn one ``POST /eval_dual`` JSON body into an :class:`InferencePlan`.

    Raises :class:`ProtocolError` for anything outside the official contract.
    The caller treats that as one lost plan; the plan store's expiry then
    handles the robot side.
    """

    if not isinstance(response, Mapping):
        raise ProtocolError("inference response must be a JSON object")
    if response.get("error"):
        raise ProtocolError(f"inference server error: {response['error']}")
    if response.get("ok") is False:
        raise ProtocolError(f"inference server reported failure: {dict(response)}")

    waypoints: tuple[tuple[float, ...], ...] | None = None
    trajectory_value = response.get("trajectory")
    if trajectory_value is not None:
        waypoints = _parse_trajectory(trajectory_value)

    discrete_value = response.get("discrete_action")
    if discrete_value is not None:
        if isinstance(discrete_value, (str, bytes)):
            raise ProtocolError("discrete_action must be a number or an array")
        if isinstance(discrete_value, Sequence):
            if not discrete_value:
                raise ProtocolError("discrete_action must not be an empty array")
            raw_actions = list(discrete_value)
        else:
            raw_actions = [discrete_value]
        try:
            actions = [int(_finite_float(item, "discrete_action")) for item in raw_actions]
        except (TypeError, ValueError) as error:
            raise ProtocolError(str(error)) from error

        kept = executable_actions(actions, limit=config.max_chunk_actions)
        steps = tuple(
            discrete_to_velocity(
                action,
                step_s=config.action_step_s,
                discrete_forward_m=config.discrete_forward_m,
                discrete_step_deg=config.discrete_step_deg,
                max_vx=config.max_vx,
                max_wz=config.max_wz,
            )
            for action in kept
        )
        if not steps:
            if all(action == DISCRETE_STOP for action in actions):
                steps = (
                    ActionStep(
                        VelocityCommand(0.0, 0.0),
                        "stop",
                        duration_s=config.action_step_s,
                    ),
                )
            else:
                # Camera-only plan: 5 and 9 are the model asking for a pitch
                # joint the Ranger does not have.  Hold position for one action
                # step and let the next frame re-plan -- the same "wait" the
                # reference skill answers with, not a silent no-op nobody can
                # see in the log.
                steps = (
                    ActionStep(
                        VelocityCommand(0.0, 0.0),
                        "camera-only plan (5/9): no chassis action, holding still",
                        duration_s=config.action_step_s,
                    ),
                )
        program = tuple(
            ProgramSegment(
                step.command,
                step.duration_s,
                turn_rad=step.turn_rad,
                distance_m=step.distance_m,
            )
            for step in steps
        )
        return InferencePlan(
            command=steps[0].command,
            waypoints=waypoints,
            control_dt=CONTROL_DT,
            velocity_source=_describe_actions(actions, steps),
            program=program,
            # STOP is a statement about the task, not just a zero command:
            # the runtime counts consecutive ones and finishes the task.
            model_stop=not kept and all(action == DISCRETE_STOP for action in actions),
        )

    if waypoints is None:
        raise ProtocolError(
            "response carries neither discrete_action nor trajectory"
        )

    step = trajectory_to_velocity(
        waypoints,
        step_s=config.action_step_s,
        lookahead_m=config.lookahead_m,
        min_turn_radius_m=config.guard_min_turn_radius_m,
        max_vx=config.max_vx,
        max_wz=config.max_wz,
        stop_m=config.trajectory_stop_m,
        cruise_vx=config.trajectory_cruise_vx,
    )
    return InferencePlan(
        command=step.command,
        waypoints=waypoints,
        control_dt=CONTROL_DT,
        velocity_source=f"trajectory: {step.note}",
        program=(ProgramSegment(step.command, step.duration_s),),
        # A trajectory short enough to mean "stay here" is the model's arrival
        # verdict, the same statement a discrete 0 makes.
        model_stop=step.model_stop,
    )
