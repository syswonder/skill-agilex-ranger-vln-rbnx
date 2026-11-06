"""Tests for the odometry feedback used by the closed-loop step controller.

The point of these tests is the arithmetic that decides "the chassis has done
the step": it has to be right in *direction*, not just in magnitude, because a
sign error here would turn a 15 degree request into a 15 degree turn the wrong
way -- and the step controller would then happily end the segment.
"""

from __future__ import annotations

import math
import unittest

from internvln_navigation.odometry import (
    Pose,
    PoseBuffer,
    advance_pose,
    distance_progress,
    distance_reached,
    servo_command,
    turn_progress,
    turn_reached,
    wrap_angle,
    yaw_from_quaternion,
)


def pose(x: float = 0.0, y: float = 0.0, yaw: float = 0.0, at: float = 0.0) -> Pose:
    return Pose(x=x, y=y, yaw=yaw, received_at=at)


class AngleTests(unittest.TestCase):
    def test_wrap_folds_into_the_short_way_round(self) -> None:
        self.assertAlmostEqual(wrap_angle(0.0), 0.0)
        self.assertAlmostEqual(wrap_angle(2 * math.pi), 0.0)
        self.assertAlmostEqual(wrap_angle(-math.pi / 2), -math.pi / 2)
        # 350 degrees is -10 degrees, not +350.
        self.assertAlmostEqual(wrap_angle(math.radians(350)), math.radians(-10))

    def test_yaw_matches_the_official_client_formula(self) -> None:
        # The Go2 client computes yaw as atan2(2zw, 1 - 2z^2); a 90 degree
        # planar rotation is z = w = sqrt(2)/2.
        half = math.sqrt(2.0) / 2.0
        self.assertAlmostEqual(yaw_from_quaternion(half, half), math.pi / 2)
        self.assertAlmostEqual(yaw_from_quaternion(0.0, 1.0), 0.0)


class PoseBufferTests(unittest.TestCase):
    def test_a_fresh_sample_is_returned(self) -> None:
        buffer = PoseBuffer()
        buffer.update(1.0, 2.0, 0.3, now=10.0)
        latest = buffer.latest(max_age_s=0.5, now=10.2)
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest.x, 1.0)
        self.assertAlmostEqual(latest.yaw, 0.3)

    def test_a_stale_sample_is_none_so_the_timer_takes_over(self) -> None:
        buffer = PoseBuffer()
        buffer.update(1.0, 2.0, 0.3, now=10.0)
        self.assertIsNone(buffer.latest(max_age_s=0.5, now=10.6))
        self.assertTrue(buffer.ever_seen)

    def test_no_sample_at_all_is_none(self) -> None:
        buffer = PoseBuffer()
        self.assertIsNone(buffer.latest(max_age_s=0.5, now=10.0))
        self.assertFalse(buffer.ever_seen)

    def test_non_finite_samples_are_ignored(self) -> None:
        buffer = PoseBuffer()
        buffer.update(float("nan"), 0.0, 0.0, now=1.0)
        self.assertIsNone(buffer.latest(max_age_s=1.0, now=1.0))


class TurnTests(unittest.TestCase):
    def test_a_left_turn_counts_only_leftward(self) -> None:
        start = pose()
        target = math.radians(15.0)
        right = pose(yaw=math.radians(-20.0))
        self.assertFalse(
            turn_reached(start, right, target, tolerance_rad=math.radians(1.5))
        )
        left = pose(yaw=math.radians(14.0))
        self.assertTrue(
            turn_reached(start, left, target, tolerance_rad=math.radians(1.5))
        )

    def test_a_right_turn_counts_only_rightward(self) -> None:
        start = pose()
        target = math.radians(-15.0)
        left = pose(yaw=math.radians(20.0))
        self.assertFalse(
            turn_reached(start, left, target, tolerance_rad=math.radians(1.5))
        )
        right = pose(yaw=math.radians(-14.0))
        self.assertTrue(
            turn_reached(start, right, target, tolerance_rad=math.radians(1.5))
        )

    def test_the_turn_is_measured_across_the_pi_seam(self) -> None:
        # Starting just below +pi and turning 15 degrees left crosses into
        # -pi; the naive difference would read as a 345 degree turn backwards.
        start = pose(yaw=math.radians(179.0))
        current = pose(yaw=math.radians(-166.0))
        self.assertAlmostEqual(
            turn_progress(start, current, math.radians(15.0)),
            math.radians(15.0),
            places=6,
        )
        self.assertTrue(
            turn_reached(start, current, math.radians(15.0), tolerance_rad=0.01)
        )


class DistanceTests(unittest.TestCase):
    def test_forward_motion_counts_toward_a_forward_goal(self) -> None:
        start = pose()
        target = 0.25
        self.assertTrue(
            distance_reached(start, pose(x=0.24), target, tolerance_m=0.02)
        )
        self.assertFalse(
            distance_reached(start, pose(x=0.10), target, tolerance_m=0.02)
        )

    def test_sideways_motion_does_not_count_as_forward_progress(self) -> None:
        # While a turn is still finishing, the chassis slides: that must not be
        # read as having covered the 0.25 m the next segment was asked for.
        start = pose()
        self.assertAlmostEqual(distance_progress(start, pose(y=0.5)), 0.0)
        self.assertFalse(
            distance_reached(start, pose(y=0.5), 0.25, tolerance_m=0.02)
        )

    def test_progress_is_measured_along_the_start_heading(self) -> None:
        # Facing +y (90 degrees), moving +y is forward.
        start = pose(yaw=math.pi / 2)
        self.assertAlmostEqual(distance_progress(start, pose(y=0.1)), 0.1)
        self.assertAlmostEqual(distance_progress(start, pose(x=0.1)), 0.0)


class AdvancePoseTests(unittest.TestCase):
    def test_a_forward_step_moves_along_the_heading(self) -> None:
        goal = advance_pose(pose(), distance_m=0.25)
        self.assertAlmostEqual(goal.x, 0.25)
        self.assertAlmostEqual(goal.y, 0.0)
        self.assertAlmostEqual(goal.yaw, 0.0)

    def test_a_forward_step_follows_a_heading_already_turned(self) -> None:
        # Facing +y, forward is +y.  This is what makes a chunk applied in
        # order match the reference's incremental_change_goal.
        goal = advance_pose(pose(yaw=math.pi / 2), distance_m=0.25)
        self.assertAlmostEqual(goal.x, 0.0, places=9)
        self.assertAlmostEqual(goal.y, 0.25)

    def test_a_turn_changes_only_the_heading(self) -> None:
        goal = advance_pose(pose(x=1.0, y=2.0), turn_rad=math.radians(15))
        self.assertAlmostEqual(goal.x, 1.0)
        self.assertAlmostEqual(goal.y, 2.0)
        self.assertAlmostEqual(goal.yaw, math.radians(15))

    def test_a_turn_wraps_across_the_pi_seam(self) -> None:
        goal = advance_pose(pose(yaw=math.radians(175)), turn_rad=math.radians(15))
        self.assertAlmostEqual(goal.yaw, math.radians(-170))


class ServoCommandTests(unittest.TestCase):
    """The reference PID, reduced to its proportional term."""

    def command(self, start: Pose, goal: Pose):
        return servo_command(
            start,
            goal,
            kp_trans=2.0,
            kp_yaw=1.5,
            max_vx=0.25,
            max_wz=0.30,
            position_tolerance_m=0.05,
            yaw_tolerance_rad=math.radians(3.0),
        )

    def test_a_goal_ahead_drives_forward(self) -> None:
        result = self.command(pose(), pose(x=0.25))
        self.assertAlmostEqual(result.vx, 0.25)  # 2.0 * 0.25 saturates at max_vx
        self.assertAlmostEqual(result.wz, 0.0)
        self.assertFalse(result.done)

    def test_a_chunk_that_turns_left_steers_left(self) -> None:
        # [forward, left] leaves the goal 0.25 m ahead and 15 degrees to the
        # left: +yaw is a left turn, so +wz is the answer.
        goal = advance_pose(
            advance_pose(pose(), distance_m=0.25), turn_rad=math.radians(15)
        )
        result = self.command(pose(), goal)
        self.assertGreater(result.wz, 0.0)
        self.assertGreater(result.vx, 0.0)

    def test_the_heading_error_steers_not_the_bearing_to_the_point(self) -> None:
        # A goal offset to the side but facing the same way is "arrived"
        # laterally.  The model expresses a turn as a heading change, so the
        # heading -- not the direction to the point -- is what steers; chasing
        # the bearing would turn for something the goal never asked for.
        result = self.command(pose(), pose(y=0.10))
        self.assertTrue(result.done)
        self.assertEqual(result.wz, 0.0)

    def test_a_pure_rotation_has_no_linear_term(self) -> None:
        result = self.command(pose(), pose(yaw=math.radians(15)))
        self.assertAlmostEqual(result.vx, 0.0)
        self.assertGreater(result.wz, 0.0)

    def test_a_goal_behind_never_commands_reverse(self) -> None:
        # The Ranger would happily drive backwards; the reference clamps a
        # negative PID output to zero instead, and so do we.
        result = self.command(pose(), pose(x=-0.30))
        self.assertEqual(result.vx, 0.0)

    def test_a_goal_inside_the_box_is_done(self) -> None:
        result = self.command(pose(), pose(x=0.02, yaw=math.radians(1.0)))
        self.assertTrue(result.done)
        self.assertEqual((result.vx, result.wz), (0.0, 0.0))

    def test_speed_is_capped(self) -> None:
        result = self.command(pose(), pose(x=50.0, yaw=math.pi))
        self.assertLessEqual(result.vx, 0.25)
        self.assertLessEqual(abs(result.wz), 0.30)

    def test_the_yaw_error_is_wrapped(self) -> None:
        # 10 degrees the short way, not 350 the long way.
        result = self.command(
            pose(yaw=math.radians(175)), pose(yaw=math.radians(-175))
        )
        self.assertAlmostEqual(math.degrees(result.yaw_error_rad), 10.0, places=6)
        self.assertGreater(result.wz, 0.0)

    def test_the_error_is_the_goal_seen_from_the_robot_frame(self) -> None:
        # Facing +y with the goal +y 0.3 m away: that is 0.3 m *ahead*, so the
        # offset must be projected on the heading, not read off x.
        result = self.command(pose(yaw=math.pi / 2), pose(y=0.30))
        self.assertAlmostEqual(result.translation_error_m, 0.30, places=9)
        self.assertGreater(result.vx, 0.0)


if __name__ == "__main__":
    unittest.main()
