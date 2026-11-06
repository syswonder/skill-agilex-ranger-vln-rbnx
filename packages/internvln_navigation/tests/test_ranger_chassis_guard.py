from __future__ import annotations

import math
import unittest

from internvln_navigation.chassis_guard import rate_limit, shape_command
from internvln_navigation.core import RuntimeConfig


class ShapeCommandTests(unittest.TestCase):
    def test_gentle_curve_passes_through_unchanged(self) -> None:
        # radius = 0.15 / 0.10 = 1.5 m, comfortably above the 0.81 m limit.
        shaped = shape_command(0.15, 0.10)
        self.assertAlmostEqual(shaped.vx, 0.15)
        self.assertAlmostEqual(shaped.wz, 0.10)
        self.assertEqual(shaped.note, "")

    def test_tight_curve_clamps_yaw_instead_of_losing_linear_x(self) -> None:
        # radius = 0.10 / 0.30 = 0.333 m.  RangerROSMessenger would switch to
        # SPINNING here and discard linear.x entirely; the guard keeps the
        # forward component and reduces yaw to the turn-radius limit.
        shaped = shape_command(0.10, 0.30)
        self.assertAlmostEqual(shaped.vx, 0.10)
        self.assertAlmostEqual(shaped.wz, 0.10 / 0.81, places=9)
        self.assertIn("yaw clamped", shaped.note)

    def test_tight_curve_result_never_enters_the_spin_branch(self) -> None:
        for vx in (0.03, 0.10, 0.25):
            for wz in (0.30, 0.40, -0.40):
                shaped = shape_command(vx, wz)
                radius = abs(shaped.vx) / abs(shaped.wz)
                self.assertGreaterEqual(radius, 0.81 - 1e-12)

    def test_clamped_yaw_keeps_the_requested_turn_direction(self) -> None:
        left = shape_command(0.10, 0.30)
        right = shape_command(0.10, -0.30)
        self.assertGreater(left.wz, 0.0)
        self.assertLess(right.wz, 0.0)
        self.assertAlmostEqual(left.wz, -right.wz)

    def test_pure_rotation_passes_through_for_the_spin_mode(self) -> None:
        # No linear component means SPINNING is the correct chassis response,
        # not an accident, so the radius constraint must not touch it.
        shaped = shape_command(0.0, 0.40)
        self.assertAlmostEqual(shaped.vx, 0.0)
        self.assertAlmostEqual(shaped.wz, 0.40)
        self.assertEqual(shaped.note, "")

    def test_allow_spin_policy_leaves_the_pair_alone(self) -> None:
        shaped = shape_command(0.10, 0.30, turn_policy="allow_spin")
        self.assertAlmostEqual(shaped.vx, 0.10)
        self.assertAlmostEqual(shaped.wz, 0.30)
        self.assertEqual(shaped.note, "")

    def test_allow_spin_policy_still_applies_the_other_gates(self) -> None:
        shaped = shape_command(0.90, -1.20, turn_policy="allow_spin")
        self.assertAlmostEqual(shaped.vx, 0.25)
        self.assertAlmostEqual(shaped.wz, -0.40)

    def test_block_policy_refuses_the_command_outright(self) -> None:
        shaped = shape_command(0.10, 0.30, turn_policy="block")
        self.assertEqual((shaped.vx, shaped.wz), (0.0, 0.0))
        self.assertIn("blocked", shaped.note)

    def test_block_policy_still_allows_a_valid_curve(self) -> None:
        shaped = shape_command(0.15, 0.10, turn_policy="block")
        self.assertAlmostEqual(shaped.vx, 0.15)
        self.assertAlmostEqual(shaped.wz, 0.10)

    def test_negative_linear_x_is_rejected_because_go2_rejected_it(self) -> None:
        shaped = shape_command(-0.20, 0.0)
        self.assertEqual(shaped.vx, 0.0)
        self.assertIn("forward-only", shaped.note)

    def test_negative_linear_x_is_allowed_when_forward_only_is_off(self) -> None:
        shaped = shape_command(-0.20, 0.0, forward_only=False)
        self.assertAlmostEqual(shaped.vx, -0.20)

    def test_chassis_clamps_match_the_go2_envelope(self) -> None:
        # The Ranger accepts 1.5 m/s and 4.8 rad/s; Go2 never exceeded
        # 0.25 m/s and 0.40 rad/s and the follow stack was tuned inside that
        # envelope.  The speed clamps apply first, then the turn-radius
        # constraint composes on top of the already-clamped pair
        # (0.25 / 0.40 == 0.625 m < 0.81 m).
        shaped = shape_command(1.50, 4.80)
        self.assertAlmostEqual(shaped.vx, 0.25)
        self.assertAlmostEqual(shaped.wz, 0.25 / 0.81, places=9)
        self.assertIn("yaw clamped", shaped.note)

    def test_go2_yaw_ceiling_is_unreachable_at_the_go2_speed_ceiling(self) -> None:
        # 0.25 / 0.40 == 0.625 m, inside the Ranger's 0.81 m turn radius, so
        # the Go2 envelope's full 0.40 rad/s simply cannot be executed on this
        # chassis while translating.  The guard caps yaw at 0.25 / 0.81 and
        # says so, instead of letting the chassis pivot and drop linear.x.
        shaped = shape_command(0.40, 0.40)
        self.assertAlmostEqual(shaped.vx, 0.25)
        self.assertAlmostEqual(shaped.wz, 0.25 / 0.81, places=9)
        self.assertIn("yaw clamped", shaped.note)

    def test_full_yaw_survives_once_the_pair_clears_the_radius(self) -> None:
        # radius = 0.25 / 0.20 = 1.25 m, above the 0.81 m limit, so the yaw
        # the controller asked for is preserved unchanged.
        shaped = shape_command(0.40, 0.20)
        self.assertAlmostEqual(shaped.vx, 0.25)
        self.assertAlmostEqual(shaped.wz, 0.20)
        self.assertIn("clamped linear.x", shaped.note)

    def test_non_finite_command_is_rejected(self) -> None:
        for vx, wz in (
            (math.nan, 0.0),
            (0.0, math.inf),
            (math.inf, math.nan),
        ):
            shaped = shape_command(vx, wz)
            self.assertEqual((shaped.vx, shaped.wz), (0.0, 0.0))

    def test_vx_at_the_spin_epsilon_is_treated_as_pure_rotation(self) -> None:
        shaped = shape_command(0.02, 0.40, spin_vx_epsilon=0.02)
        self.assertAlmostEqual(shaped.wz, 0.40)

    def test_vx_above_the_spin_epsilon_is_constrained(self) -> None:
        shaped = shape_command(0.03, 0.40, spin_vx_epsilon=0.02)
        self.assertAlmostEqual(shaped.wz, 0.03 / 0.81, places=9)


class RateLimitTests(unittest.TestCase):
    def test_accelerates_at_the_configured_rate(self) -> None:
        self.assertAlmostEqual(rate_limit(0.0, 0.25, 0.30, 0.1), 0.03)

    def test_decelerates_at_the_configured_rate(self) -> None:
        self.assertAlmostEqual(rate_limit(0.25, 0.0, 0.30, 0.1), 0.22)

    def test_snaps_when_the_target_is_within_one_step(self) -> None:
        self.assertAlmostEqual(rate_limit(0.24, 0.25, 0.30, 0.1), 0.25)

    def test_zero_dt_holds_the_previous_value(self) -> None:
        self.assertAlmostEqual(rate_limit(0.10, 0.25, 0.30, 0.0), 0.10)

    def test_non_finite_input_collapses_to_zero(self) -> None:
        self.assertEqual(rate_limit(math.nan, 0.25, 0.30, 0.1), 0.0)


class GuardConfigTests(unittest.TestCase):
    def test_defaults_reproduce_the_effective_go2_ceiling(self) -> None:
        config = RuntimeConfig()
        self.assertAlmostEqual(config.guard_max_vx_mps, 0.25)
        self.assertAlmostEqual(config.guard_max_wz_rps, 0.40)
        self.assertAlmostEqual(config.guard_min_turn_radius_m, 0.81)
        self.assertEqual(config.guard_turn_policy, "clamp_yaw")
        self.assertTrue(config.guard_forward_only)

    def test_manifest_style_overrides_are_accepted(self) -> None:
        config = RuntimeConfig.from_mapping(
            {
                "guard_turn_policy": "allow_spin",
                "guard_max_vx_mps": 0.30,
                "guard_min_turn_radius_m": 0.4764,
            }
        )
        self.assertEqual(config.guard_turn_policy, "allow_spin")
        self.assertAlmostEqual(config.guard_max_vx_mps, 0.30)
        self.assertAlmostEqual(config.guard_min_turn_radius_m, 0.4764)

    def test_unknown_turn_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"guard_turn_policy": "spin"})

    def test_guard_speed_ceiling_cannot_exceed_the_configured_profile(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"guard_max_vx_mps": 1.50})

    def test_non_absolute_guard_topic_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"guard_output_topic": "cmd_vel"})

    def test_guard_keys_survive_as_ros_parameters(self) -> None:
        # The manifest reaches the ROS nodes only through as_ros_parameters(),
        # so every guard key must be present in the flattened parameter map.
        parameters = RuntimeConfig().as_ros_parameters()
        for name in (
            "guard_input_topic",
            "guard_output_topic",
            "guard_min_turn_radius_m",
            "guard_turn_policy",
        ):
            self.assertIn(name, parameters)


if __name__ == "__main__":
    unittest.main()
