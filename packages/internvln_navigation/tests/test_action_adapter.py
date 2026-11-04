from __future__ import annotations

import math
import unittest

from internvln_navigation.action_adapter import (
    discrete_to_velocity,
    parse_plan,
    trajectory_to_velocity,
)
from internvln_navigation.core import ProtocolError, RuntimeConfig


# One stop-and-go step.  The adapter divides by this, not by the plan's
# watchdog horizon.
HOLD = 1.5


def config(**overrides) -> RuntimeConfig:
    return RuntimeConfig.from_mapping(overrides)


class DiscreteActionTests(unittest.TestCase):
    def test_stop_is_zero(self) -> None:
        step = discrete_to_velocity(0, step_s=HOLD)
        self.assertEqual((step.command.vx, step.command.wz), (0.0, 0.0))
        self.assertEqual(step.note, "stop")

    def test_forward_step_covers_the_trained_distance(self) -> None:
        # 0.25 m spread over a 1.5 s hold is 0.1667 m/s, which is above this
        # package's 0.15 m/s default, so it is clipped and says so.
        step = discrete_to_velocity(1, step_s=HOLD, max_vx=0.50)
        self.assertAlmostEqual(step.command.vx, 0.25 / HOLD)
        self.assertEqual(step.command.wz, 0.0)
        self.assertFalse(step.clipped)

    def test_a_capped_forward_step_is_held_longer_not_cut_short(self) -> None:
        # The cap decides how fast the step is driven, never how far.  Asking
        # for 0.25 m at a 0.15 m/s ceiling takes 1.67 s -- the distance the
        # model asked for is the distance the robot covers.
        step = discrete_to_velocity(1, step_s=HOLD, max_vx=0.15)
        self.assertEqual(step.command.vx, 0.15)
        self.assertTrue(step.clipped)
        self.assertAlmostEqual(step.duration_s, 0.25 / 0.15, places=6)
        self.assertAlmostEqual(step.command.vx * step.duration_s, 0.25, places=6)
        self.assertIn("held longer", step.note)

    def test_left_and_right_are_opposite_pure_rotations(self) -> None:
        left = discrete_to_velocity(2, step_s=HOLD)
        right = discrete_to_velocity(3, step_s=HOLD)
        # vx must be exactly zero: the Ranger pivots in place on a zero-linear
        # command, which is what "rotate 15 degrees" should be.
        self.assertEqual(left.command.vx, 0.0)
        self.assertEqual(right.command.vx, 0.0)
        self.assertAlmostEqual(left.command.wz, math.radians(15.0) / HOLD)
        self.assertAlmostEqual(right.command.wz, -math.radians(15.0) / HOLD)

    def test_each_step_carries_the_geometry_it_is_for(self) -> None:
        # The step controller ends a segment on odometry, which needs to know
        # what the segment was *for*.  A turn says the angle; a move says the
        # distance; a stop says neither (so it stays on the timer).
        forward = discrete_to_velocity(1, step_s=HOLD, max_vx=0.50)
        self.assertAlmostEqual(forward.distance_m, 0.25)
        self.assertEqual(forward.turn_rad, 0.0)
        left = discrete_to_velocity(2, step_s=HOLD)
        self.assertAlmostEqual(left.turn_rad, math.radians(15.0))
        right = discrete_to_velocity(3, step_s=HOLD)
        self.assertAlmostEqual(right.turn_rad, -math.radians(15.0))
        self.assertEqual(right.distance_m, 0.0)
        stop = discrete_to_velocity(0, step_s=HOLD)
        self.assertEqual((stop.turn_rad, stop.distance_m), (0.0, 0.0))

    def test_unsupported_camera_actions_are_no_ops_not_errors(self) -> None:
        for action in (5, 9):
            with self.subTest(action=action):
                step = discrete_to_velocity(action, step_s=HOLD)
                self.assertEqual((step.command.vx, step.command.wz), (0.0, 0.0))
                self.assertIn("no-op", step.note)

    def test_an_unknown_action_is_a_protocol_error(self) -> None:
        with self.assertRaises(ProtocolError):
            discrete_to_velocity(7, step_s=HOLD)

    def test_the_step_horizon_must_be_positive(self) -> None:
        with self.assertRaises(ProtocolError):
            discrete_to_velocity(1, step_s=0.0)


class TrajectoryTests(unittest.TestCase):
    """The trajectory controller drives the model's curve, threshold-free.

    The bearings and step sizes here are the ones the live model actually
    produced (a path that leans 3 degrees at 0.3 m is a real answer, not
    noise), because the point of this controller is that they all get a
    proportional yaw instead of being rounded to zero.

    The invariant every arc test is written against is geometric, not
    formula-shaped: the commanded (vx, wz) must describe the circle that passes
    through the pursued point and is tangent to the robot's heading, i.e.
    ``radius = distance / (2 sin(bearing))``.
    """

    def _step(self, trajectory, **overrides):
        settings = dict(step_s=HOLD, lookahead_m=0.30, max_vx=0.50)
        settings.update(overrides)
        return trajectory_to_velocity(trajectory, **settings)

    def _circle_radius(self, step) -> float:
        return step.command.vx / abs(step.command.wz)

    def test_the_first_point_beyond_the_lookahead_is_pursued(self) -> None:
        step = self._step([[0.05, 0.0], [0.10, 0.0], [0.40, 0.0], [1.00, 0.0]])
        self.assertAlmostEqual(step.command.vx, 0.30 / HOLD)
        self.assertEqual(step.command.wz, 0.0)
        self.assertIn("straight", step.note)

    def test_a_short_trajectory_falls_back_to_its_final_point(self) -> None:
        # Shorter than the lookahead but still a move: the final point is the
        # one pursued, so the advance is the path's whole extent.
        step = self._step([[0.06, 0.0], [0.12, 0.0]])
        self.assertAlmostEqual(step.command.vx, 0.12 / HOLD)
        self.assertFalse(step.model_stop)

    def test_a_trajectory_inside_the_arrival_box_means_stay_here(self) -> None:
        # 5 cm or less is the model saying "you are there", not "creep 2 cm".
        # It is reported as the model's own STOP so a model that only ever
        # answers with trajectories can still end a task, and it commands
        # nothing at all -- the old behaviour held a 0.01 m/s creep for the
        # whole duration, which is the shuffling around a reached goal.
        step = self._step([[0.02, 0.0], [0.05, 0.0]])
        self.assertEqual((step.command.vx, step.command.wz), (0.0, 0.0))
        self.assertTrue(step.model_stop)
        self.assertIn("extent 0.05 m", step.note)

    def test_a_small_bearing_still_steers(self) -> None:
        # The live model's own left-leaning path: 3 degrees of bearing at
        # 0.3 m.  This used to be rounded to "drive straight" and is the whole
        # reason the robot never followed a curve it was given.
        step = self._step([[0.40, 0.02097]])
        self.assertAlmostEqual(step.command.vx, 0.30 / HOLD)
        self.assertGreater(step.command.wz, 0.0)
        self.assertIn("arc radius", step.note)

    def test_there_is_no_dead_zone_around_zero(self) -> None:
        for bearing_deg in (0.2, 1.0, 3.0, 10.0):
            with self.subTest(bearing_deg=bearing_deg):
                ahead = 0.40
                point = [ahead, ahead * math.tan(math.radians(bearing_deg))]
                step = self._step([point])
                self.assertNotEqual(step.command.wz, 0.0)
                # Monotone in the bearing: a steeper lean asks for a tighter
                # arc, never for a dead zone.
                self.assertAlmostEqual(
                    self._circle_radius(step),
                    math.hypot(*point)
                    / (2.0 * math.sin(math.radians(bearing_deg))),
                    places=6,
                )

    def test_a_right_hand_bearing_turns_the_other_way(self) -> None:
        step = self._step([[0.40, -0.02097]])
        self.assertLess(step.command.wz, 0.0)
        self.assertGreater(step.command.vx, 0.0)

    def test_the_commanded_arc_passes_through_the_point_it_aims_at(self) -> None:
        # vx / wz is the radius of the circle the robot will drive, and it has
        # to be the circle through the pursued point.
        step = self._step([[0.60, 0.0317]])
        distance = math.hypot(0.60, 0.0317)
        bearing = math.atan2(0.0317, 0.60)
        self.assertAlmostEqual(
            self._circle_radius(step), distance / (2.0 * math.sin(bearing)), places=6
        )
        self.assertIn("arc radius", step.note)

    def test_the_commanded_radius_is_the_models_radius_not_double_it(self) -> None:
        # A point taken from a real 2.86 m arc, one lookahead along it.  The
        # rule this replaced (wz = bearing / hold) implied a radius of
        # advance / bearing, which for a point one lookahead away is exactly
        # twice the model's -- the robot drove a path half as curved as it was
        # given and only caught up by re-planning every frame.  Pure pursuit
        # reproduces the radius the point sits on.
        radius_m = 2.86
        theta = 0.30 / radius_m
        point = [radius_m * math.sin(theta), radius_m * (1.0 - math.cos(theta))]
        step = self._step([point])
        self.assertAlmostEqual(self._circle_radius(step), radius_m, places=6)
        # The chord is marginally shorter than the arc, so the advance is
        # capped by the point itself -- still one lookahead's worth of motion.
        self.assertAlmostEqual(
            step.command.vx, min(0.30, math.hypot(*point)) / HOLD, places=6
        )

    def test_a_cruise_speed_keeps_the_step_to_one_steps_travel(self) -> None:
        # Without the cap, ``advance / hold`` is 0.30 m/s here and 1.0 m/s at
        # the deployment's 1 m lookahead -- i.e. the chassis ceiling on every
        # answer, with the hold stretched to match.  A cruise speed below the
        # cap commands that speed for the nominal step instead.
        step = self._step([[0.40, 0.0], [1.20, 0.0]], cruise_vx=0.10)
        self.assertAlmostEqual(step.command.vx, 0.10)
        self.assertAlmostEqual(step.duration_s, HOLD)
        self.assertFalse(step.clipped)

    def test_a_cruise_speed_does_not_change_the_arc_radius(self) -> None:
        point = [0.60, 0.0317]
        distance = math.hypot(*point)
        bearing = math.atan2(point[1], point[0])
        step = self._step([point], cruise_vx=0.10)
        self.assertAlmostEqual(
            self._circle_radius(step), distance / (2.0 * math.sin(bearing)), places=6
        )

    def test_a_bearing_past_a_right_angle_is_clamped(self) -> None:
        # 120 degrees puts the point behind the robot; it turns 90 and the
        # next frame re-plans instead of spinning past the target.
        step = self._step([[-0.20, 0.20 * math.tan(math.radians(60.0))]])
        self.assertEqual(step.command.vx, 0.0)
        self.assertGreater(step.command.wz, 0.0)
        self.assertAlmostEqual(step.command.wz, min(0.30, math.pi / 2 / HOLD))

    def test_a_bearing_inside_the_chassis_radius_is_driven_as_an_arc(self) -> None:
        # 10 degrees at a 0.4 m lookahead wants a 1.17 m radius, which the
        # Ranger can drive, so the robot arcs through it.
        step = self._step([[0.40, 0.40 * math.tan(math.radians(10.0))]])
        self.assertGreater(step.command.vx, 0.0)
        self.assertGreater(step.command.wz, 0.0)
        self.assertGreaterEqual(self._circle_radius(step), 0.81 - 1e-9)
        self.assertNotIn("pivoting", step.note)

    def test_a_bearing_tighter_than_the_chassis_radius_pivots(self) -> None:
        # 20 degrees at the same lookahead wants a 0.62 m radius -- tighter
        # than the chassis minimum, where the Ranger would silently drop
        # linear.x.  (It used to be commanded as a 0.86 m arc, because the old
        # rule read the curvature as half of what it is.)  The pivot is
        # commanded in the open instead.
        step = self._step([[0.40, 0.40 * math.tan(math.radians(20.0))]])
        self.assertEqual(step.command.vx, 0.0)
        self.assertGreater(step.command.wz, 0.0)
        self.assertIn("pivoting", step.note)

    def test_a_steep_bearing_pivots_too(self) -> None:
        step = self._step([[0.40, 0.40 * math.tan(math.radians(30.0))]])
        self.assertEqual(step.command.vx, 0.0)
        self.assertGreater(step.command.wz, 0.0)
        self.assertIn("pivoting", step.note)

    def test_a_point_behind_the_robot_is_turned_to_not_driven_through(self) -> None:
        # A bearing past 90 degrees means the path leaves backwards.  The
        # chassis is forward-only, so the answer is a pivot of the right sign,
        # never a forward command "towards" a point that is behind.
        step = self._step([[-0.30, -0.10]])
        self.assertEqual(step.command.vx, 0.0)
        self.assertLess(step.command.wz, 0.0)
        self.assertIn("pivoting", step.note)

    def test_a_speed_cap_stretches_the_arc_instead_of_distorting_it(self) -> None:
        # Caps scale (vx, wz) by one factor, so the arc keeps the radius the
        # model drew and only takes longer.  Clipping vx alone used to leave
        # the full bearing in place: the robot advanced less than asked and
        # turned more than the path required.
        loose = self._step([[0.40, 0.40 * math.tan(math.radians(10.0))]])
        capped = self._step(
            [[0.40, 0.40 * math.tan(math.radians(10.0))]], max_vx=0.05
        )
        self.assertAlmostEqual(capped.command.vx, 0.05, places=6)
        self.assertAlmostEqual(
            capped.command.vx / capped.command.wz,
            loose.command.vx / loose.command.wz,
            places=6,
        )
        self.assertAlmostEqual(
            capped.command.wz * capped.duration_s,
            loose.command.wz * loose.duration_s,
            places=6,
        )
        self.assertGreater(capped.duration_s, loose.duration_s)

    def test_a_pivot_beyond_the_yaw_limit_turns_at_the_ceiling_and_says_so(self) -> None:
        # A right-angle bearing wants 1.57 rad/s, well past the 0.30 rad/s
        # chassis ceiling.  The robot turns as hard as it may -- for exactly as
        # long as the full 90 degrees needs -- and re-plans.
        step = self._step([[0.0, 0.50]])
        self.assertEqual(step.command.vx, 0.0)
        self.assertEqual(step.command.wz, 0.30)
        self.assertTrue(step.clipped)
        self.assertAlmostEqual(
            step.command.wz * step.duration_s, math.radians(90.0), places=6
        )
        self.assertIn("held longer", step.note)

    def test_a_yaw_bearing_point_is_accepted_and_ignored(self) -> None:
        step = self._step([[0.40, 0.0, 1.2]])
        self.assertAlmostEqual(step.command.vx, 0.30 / HOLD)

    def test_a_zero_length_trajectory_holds_still(self) -> None:
        step = self._step([[0.0, 0.0]])
        self.assertEqual((step.command.vx, step.command.wz), (0.0, 0.0))

    def test_the_step_horizon_and_the_radius_must_be_positive(self) -> None:
        for overrides in ({"step_s": 0.0}, {"min_turn_radius_m": 0.0}):
            with self.subTest(**overrides), self.assertRaises(ProtocolError):
                self._step([[0.40, 0.0]], **overrides)

    def test_malformed_trajectories_are_rejected(self) -> None:
        for bad in (
            [],
            "not-a-trajectory",
            [[0.1]],
            [[0.1, 0.2], ["x", 0.2]],
            [[0.1, float("nan")]],
        ):
            with self.subTest(trajectory=bad), self.assertRaises(ProtocolError):
                self._step(bad)


class ParsePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._config = config(max_vx=0.50)

    def test_discrete_action_wins_over_trajectory(self) -> None:
        plan = parse_plan(
            {"discrete_action": [1], "trajectory": [[0.4, 0.4]]},
            config=self._config,
        )
        self.assertGreater(plan.command.vx, 0.0)
        self.assertEqual(plan.command.wz, 0.0)
        self.assertIn("discrete_action[1]", plan.velocity_source)
        # The trajectory is still recorded for diagnostics.
        self.assertEqual(len(plan.waypoints or ()), 1)

    def test_a_scalar_discrete_action_is_accepted(self) -> None:
        plan = parse_plan({"discrete_action": 0}, config=self._config)
        self.assertEqual(plan.command.vx, 0.0)

    def test_a_trajectory_only_response_is_used(self) -> None:
        plan = parse_plan(
            {"trajectory": [[0.10, 0.0], [0.40, 0.0]], "pixel_goal": [1, 2]},
            config=self._config,
        )
        self.assertIn("trajectory", plan.velocity_source)
        self.assertEqual(len(plan.waypoints or ()), 2)
        # A trajectory segment is followed, not aimed: it carries no
        # turn/distance goal, so the closed loop leaves it on the timer.
        self.assertEqual(len(plan.program), 1)
        self.assertEqual(plan.program[0].turn_rad, 0.0)
        self.assertEqual(plan.program[0].distance_m, 0.0)

    def test_the_program_carries_each_actions_goal(self) -> None:
        plan = parse_plan(
            {"discrete_action": [1, 2]},
            config=config(max_vx=0.50, max_chunk_actions=2),
        )
        self.assertEqual(len(plan.program), 2)
        self.assertAlmostEqual(plan.program[0].distance_m, 0.25)
        self.assertAlmostEqual(plan.program[1].turn_rad, math.radians(15.0))

    def test_the_sign_of_y_is_the_sign_of_the_turn_end_to_end(self) -> None:
        # The model's own left-leaning answer, and the same answer mirrored.
        # +y really is left (the vendor's own converter maps a positive
        # atan2(dy, dx) to its "left" action code), so a mirrored path has to
        # come out as an exactly opposite yaw -- and a right-hand answer has to
        # be able to steer right at all.  Nothing anywhere may take |y|.
        leftward = [
            [0.0, 0.0],
            [0.0993, 0.00527],
            [0.1998, 0.01006],
            [0.3002, 0.01516],
            [0.4011, 0.02126],
            [0.5018, 0.03072],
            [0.6020, 0.04451],
        ]
        rightward = [[x, -y] for x, y in leftward]
        left = parse_plan({"trajectory": leftward}, config=self._config)
        right = parse_plan({"trajectory": rightward}, config=self._config)
        self.assertGreater(left.command.wz, 0.0)
        self.assertLess(right.command.wz, 0.0)
        self.assertAlmostEqual(left.command.wz, -right.command.wz)
        self.assertAlmostEqual(left.command.vx, right.command.vx)

    def test_the_packaged_config_drives_the_models_own_radius(self) -> None:
        # Same invariant through the packaged configuration: the yaw is the
        # pure-pursuit pair for the point pursued, so vx / wz is the radius
        # that point sits on.
        distance = math.hypot(0.40, 0.40 * math.tan(math.radians(3.0)))
        bearing = math.atan2(0.40 * math.tan(math.radians(3.0)), 0.40)
        plan = parse_plan(
            {"trajectory": [[0.40, 0.40 * math.tan(math.radians(3.0))]]},
            config=self._config,
        )
        self.assertAlmostEqual(
            plan.command.vx / abs(plan.command.wz),
            distance / (2.0 * math.sin(bearing)),
            places=6,
        )

    def test_a_server_error_is_a_protocol_error(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_plan({"error": "model oom"}, config=self._config)
        with self.assertRaises(ProtocolError):
            parse_plan({"ok": False, "detail": "no"}, config=self._config)

    def test_a_response_with_neither_field_is_a_protocol_error(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_plan({"pixel_goal": [1, 2]}, config=self._config)

    def test_an_empty_discrete_action_is_a_protocol_error(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_plan({"discrete_action": []}, config=self._config)

    def test_a_non_object_response_is_a_protocol_error(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_plan(["discrete_action"], config=self._config)  # type: ignore[arg-type]

    def test_an_unknown_action_surfaces_as_a_protocol_error(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_plan({"discrete_action": [42]}, config=self._config)

    def test_an_unknown_action_anywhere_in_a_plan_is_a_protocol_error(self) -> None:
        # A code this chassis has never been taught is contract drift, and the
        # whole plan is suspect -- not just the tail this tick does not run.
        with self.assertRaises(ProtocolError):
            parse_plan({"discrete_action": [1, 42, 1, 1]}, config=self._config)

    def test_a_leading_zero_does_not_brake_a_plan_that_has_steps(self) -> None:
        """The server sends four steps at a time; [0] alone is not "arrived"."""

        plan = parse_plan(
            {"discrete_action": [0, 1, 1, 1]},
            config=config(max_vx=0.50, max_chunk_actions=4),
        )
        self.assertGreater(plan.command.vx, 0.0)
        self.assertEqual(plan.command.wz, 0.0)
        self.assertIn("discrete_action[0, 1, 1, 1]", plan.velocity_source)
        # The zero is dropped and the three moves that follow it are driven.
        self.assertEqual(len(plan.program), 3)
        self.assertIn("3 step(s)", plan.velocity_source)

        # At the shipped cap of 2 the zero is still dropped and the first two
        # moves are driven -- never the zero.
        shipped = parse_plan({"discrete_action": [0, 1, 1, 1]}, config=self._config)
        self.assertEqual(len(shipped.program), 2)
        self.assertGreater(shipped.command.vx, 0.0)

    def test_a_plan_of_nothing_but_zeros_is_the_stop(self) -> None:
        plan = parse_plan({"discrete_action": [0, 0, 0, 0]}, config=self._config)
        self.assertEqual((plan.command.vx, plan.command.wz), (0.0, 0.0))
        self.assertIn("stop", plan.velocity_source)

    def test_the_default_commits_to_two_steps(self) -> None:
        # The trained chunk is four actions, and both reference deployments
        # drive all of it before looking again -- but four forward steps is a
        # committed metre between two photographs, and the live model answers
        # [1,1,1,1] repeatedly.  The shipped cap is 2 (2026-11-04): half the
        # blind commitment, the chunk's order and semantics intact.
        for payload in ([1], [1, 1], [1, 1, 1], [1, 1, 1, 1]):
            with self.subTest(payload=payload):
                plan = parse_plan(
                    {"discrete_action": payload}, config=config(max_vx=0.50)
                )
                driven = min(len(payload), 2)
                self.assertEqual(len(plan.program), driven)
                covered = sum(
                    segment.command.vx * segment.duration_s
                    for segment in plan.program
                )
                self.assertAlmostEqual(covered, 0.25 * driven, places=6)

        # Longer than the cap is cut at the cap: 2 is how many elements one
        # answer may commit to, never "however many the server sent".
        plan = parse_plan({"discrete_action": [1] * 6}, config=config(max_vx=0.50))
        self.assertEqual(len(plan.program), 2)

    def test_the_cap_can_be_raised_to_drive_whole_chunks(self) -> None:
        # The machinery is deliberately left in place: both reference
        # deployments read the list as one committed chunk, so raising the cap
        # is the switch that turns that behaviour back on.
        chunked = config(max_vx=0.50, max_chunk_actions=4)
        plan = parse_plan({"discrete_action": [1, 1, 1, 1]}, config=chunked)
        self.assertEqual(len(plan.program), 4)
        covered = sum(
            segment.command.vx * segment.duration_s for segment in plan.program
        )
        self.assertAlmostEqual(covered, 4 * chunked.discrete_forward_m, places=6)

    def test_a_raised_cap_keeps_the_order_and_the_geometry(self) -> None:
        # [5, 2, 2, 2] with a raised cap is three 15-degree left turns, driven
        # one after the other -- never merged, never reordered.
        chunked = config(max_vx=0.50, max_chunk_actions=4)
        plan = parse_plan({"discrete_action": [5, 2, 2, 2]}, config=chunked)
        self.assertEqual(len(plan.program), 3)
        for segment in plan.program:
            self.assertEqual(segment.command.vx, 0.0)
            self.assertAlmostEqual(
                segment.command.wz,
                math.radians(15.0) / chunked.action_step_s,
                places=6,
            )
        self.assertAlmostEqual(
            sum(segment.duration_s for segment in plan.program),
            3 * chunked.action_step_s,
            places=6,
        )

    def test_a_chunk_longer_than_the_cap_is_cut_not_reordered(self) -> None:
        plan = parse_plan(
            {"discrete_action": [1, 1, 1, 1, 1, 1]},
            config=config(max_vx=0.50, max_chunk_actions=4),
        )
        self.assertEqual(len(plan.program), 4)

    def test_a_step_is_found_behind_the_camera_actions(self) -> None:
        plan = parse_plan({"discrete_action": [1, 5, 5, 9]}, config=self._config)
        self.assertGreater(plan.command.vx, 0.0)

    def test_a_camera_only_plan_holds_still_and_says_so(self) -> None:
        plan = parse_plan({"discrete_action": [5, 5, 5, 5]}, config=self._config)
        self.assertEqual((plan.command.vx, plan.command.wz), (0.0, 0.0))
        # Not a bare "no-op": the log has to show the model asking for the
        # pitch joint this chassis does not have, or a standstill looks like a
        # client that has stopped talking to the server.
        self.assertIn("camera-only", plan.velocity_source)
        self.assertIn("[5, 5, 5, 5]", plan.velocity_source)

    def test_a_zero_mixed_with_camera_actions_is_not_a_stop(self) -> None:
        # Reference semantics: only an all-zero plan means "arrived".
        plan = parse_plan({"discrete_action": [0, 5]}, config=self._config)
        self.assertEqual((plan.command.vx, plan.command.wz), (0.0, 0.0))
        self.assertIn("camera-only", plan.velocity_source)

    def test_only_an_all_zero_plan_marks_the_task_as_stopped(self) -> None:
        """STOP is the model's arrival verdict; 5/9 and driving are not."""

        for payload in (0, [0], [0, 0, 0, 0]):
            with self.subTest(payload=payload):
                plan = parse_plan({"discrete_action": payload}, config=self._config)
                self.assertTrue(plan.model_stop)

        for payload in (
            1,
            [0, 1, 1, 1],
            [5, 2, 2, 2],
            [0, 5],
            [5, 5, 5, 5],
        ):
            with self.subTest(payload=payload):
                plan = parse_plan({"discrete_action": payload}, config=self._config)
                self.assertFalse(plan.model_stop)

        # A trajectory is motion, not a verdict -- unless it is too short to be
        # one: a path ending inside the arrival box is the model saying "you
        # are there", and it has to count, or a model that only ever answers
        # with trajectories can never finish a task.
        plan = parse_plan(
            {"trajectory": [[0.10, 0.0], [0.40, 0.0]]}, config=self._config
        )
        self.assertFalse(plan.model_stop)
        plan = parse_plan(
            {"trajectory": [[0.01, 0.0], [0.02, 0.0]]}, config=self._config
        )
        self.assertTrue(plan.model_stop)

    def test_the_configured_action_step_is_what_scales_the_command(self) -> None:
        # plan_hold_s is only the watchdog now; action_step_s is the divisor.
        quick = config(action_step_s=0.5, max_vx=0.50)
        plan = parse_plan({"discrete_action": [1]}, config=quick)
        self.assertAlmostEqual(plan.command.vx, 0.25 / 0.5, places=6)
        slow = config(action_step_s=2.0, plan_hold_s=2.0, max_vx=0.50)
        plan = parse_plan({"discrete_action": [1]}, config=slow)
        self.assertAlmostEqual(plan.command.vx, 0.25 / 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
