from __future__ import annotations

import math
import unittest

import numpy as np

from internvln_navigation.core import RuntimeConfig
from internvln_navigation.safety_gate import (
    DepthSafetyGate,
    SafetyGateConfig,
    gate_config_from_runtime,
)


FAR_MM = 4000
NEAR_MM = 200


def depth_frame(shape=(100, 100), value=FAR_MM, dtype=np.uint16) -> np.ndarray:
    return np.full(shape, value, dtype=dtype)


def config(**overrides) -> SafetyGateConfig:
    settings = dict(
        enabled=True,
        require_depth=True,
        stop_distance_m=0.30,
        release_distance_m=0.50,
        depth_timeout_s=0.30,
        min_valid_pixels=10,
        percentile=1.0,
    )
    settings.update(overrides)
    return SafetyGateConfig(**settings)


def with_block(frame: np.ndarray, value=NEAR_MM) -> np.ndarray:
    """Put a 20x20 block in the middle of the frame (inside the ROI)."""

    height, width = frame.shape
    top, bottom = height // 2 - 10, height // 2 + 10
    left, right = width // 2 - 10, width // 2 + 10
    frame[top:bottom, left:right] = value
    return frame


class DepthSafetyGateTests(unittest.TestCase):
    def test_a_wall_inside_the_stop_distance_blocks(self) -> None:
        gate = DepthSafetyGate(config())
        decision = gate.observe(with_block(depth_frame()), now=1.0)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "obstacle")
        self.assertAlmostEqual(decision.nearest_m, NEAR_MM / 1000.0)

    def test_a_far_wall_is_clear(self) -> None:
        gate = DepthSafetyGate(config())
        decision = gate.observe(depth_frame(), now=1.0)
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.reason, "clear")

    def test_the_floor_below_the_roi_cannot_block_the_robot(self) -> None:
        # The floor just under the camera is always within a metre; only the
        # band above the ROI bottom may trigger.
        frame = depth_frame()
        frame[int(frame.shape[0] * 0.85) :, :] = 300
        gate = DepthSafetyGate(config())
        decision = gate.observe(frame, now=1.0)
        self.assertFalse(decision.blocked, decision.reason)

    def test_a_valid_frame_after_a_fail_closed_start_is_judged_on_its_own(self) -> None:
        # The node boots with no depth and therefore "blocked".  That state must
        # not act like an obstacle latch: a first frame reading 0.60 m is above
        # the 0.30 m stop and has to open the gate, or the robot never moves on
        # a floor that sits between stop and release.
        gate = DepthSafetyGate(config())
        self.assertTrue(gate.check(now=1.0).blocked)

        decision = gate.observe(depth_frame(value=600), now=1.1)
        self.assertFalse(decision.blocked, decision.reason)
        self.assertEqual(decision.reason, "clear")

    def test_missing_depth_does_not_release_an_obstacle_latch(self) -> None:
        # ...but a real obstacle latch survives a stale gap: it may still be
        # there once the stream returns.
        gate = DepthSafetyGate(config())
        self.assertTrue(gate.observe(depth_frame(value=200), now=1.0).blocked)
        self.assertTrue(gate.check(now=1.5).blocked)  # stale
        self.assertTrue(gate.observe(depth_frame(value=400), now=1.6).blocked)
        self.assertFalse(gate.observe(depth_frame(value=600), now=1.7).blocked)

    def test_the_gate_has_hysteresis_between_stop_and_release(self) -> None:
        gate = DepthSafetyGate(config())
        self.assertTrue(gate.observe(with_block(depth_frame()), now=1.0).blocked)

        # Between stop and release the gate stays closed on purpose: opening at
        # 0.30 m would make the base pulse.
        held = gate.observe(depth_frame(value=400), now=1.1)
        self.assertTrue(held.blocked)
        self.assertEqual(held.reason, "obstacle")

        released = gate.observe(depth_frame(value=600), now=1.2)
        self.assertFalse(released.blocked)
        self.assertEqual(released.reason, "clear")

    def test_millimetres_are_converted_to_metres(self) -> None:
        gate = DepthSafetyGate(config())
        decision = gate.observe(with_block(depth_frame(dtype=np.uint16)), now=1.0)
        self.assertAlmostEqual(decision.nearest_m, 0.2)

    def test_float_depth_is_already_in_metres(self) -> None:
        frame = depth_frame(dtype=np.float32) / 1000.0
        with_block(frame, value=0.2)
        gate = DepthSafetyGate(config())
        decision = gate.observe(frame, now=1.0)
        self.assertTrue(decision.blocked)
        self.assertAlmostEqual(decision.nearest_m, 0.2)

    def test_an_explicit_scale_overrides_the_dtype_default(self) -> None:
        gate = DepthSafetyGate(config(depth_scale_m=0.0005))
        decision = gate.observe(with_block(depth_frame()), now=1.0)
        self.assertAlmostEqual(decision.nearest_m, NEAR_MM * 0.0005)

    def test_no_depth_fails_closed_when_required(self) -> None:
        gate = DepthSafetyGate(config())
        decision = gate.check(now=1.0)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "no_depth")

    def test_no_depth_is_clear_when_the_operator_accepts_it(self) -> None:
        gate = DepthSafetyGate(config(require_depth=False))
        decision = gate.check(now=1.0)
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.reason, "no_depth")

    def test_a_stale_frame_fails_closed(self) -> None:
        gate = DepthSafetyGate(config())
        gate.observe(depth_frame(), now=1.0)
        decision = gate.check(now=1.5)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "stale_depth")

    def test_a_frame_with_too_few_valid_pixels_fails_closed(self) -> None:
        gate = DepthSafetyGate(config())
        decision = gate.observe(depth_frame(value=0), now=1.0)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "invalid_depth")
        self.assertEqual(decision.valid_pixels, 0)

    def test_a_disabled_gate_is_always_clear(self) -> None:
        gate = DepthSafetyGate(config(enabled=False))
        decision = gate.observe(with_block(depth_frame()), now=1.0)
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.reason, "disabled")

    def test_obstacles_outside_the_forward_band_are_ignored(self) -> None:
        frame = depth_frame()
        # Left edge of the image, outside the centred 60% band.
        frame[40:60, 0:10] = 300
        gate = DepthSafetyGate(config())
        self.assertFalse(gate.observe(frame, now=1.0).blocked)
        # The same obstacle moved into the band does block.
        frame[40:60, 40:60] = 300
        self.assertTrue(gate.observe(frame, now=1.1).blocked)

    def test_malformed_frames_are_rejected(self) -> None:
        gate = DepthSafetyGate(config())
        for bad in (
            [[1, 2], [3, 4]],
            np.zeros((10, 10, 3), dtype=np.uint16),
            np.zeros((10, 10), dtype=np.uint8),
        ):
            with self.subTest(frame=type(bad)), self.assertRaises(ValueError):
                gate.observe(bad, now=1.0)

    def test_a_blocked_decision_carries_the_evidence(self) -> None:
        # Every blocked decision has to say *why*, or a stationary robot looks
        # like a hung process rather than a robot refusing to hit a wall.
        gate = DepthSafetyGate(config())
        decision = gate.observe(with_block(depth_frame()), now=1.0)
        self.assertTrue(decision.blocked)
        self.assertIsNotNone(decision.nearest_m)
        self.assertGreater(decision.valid_pixels, 0)
        self.assertEqual(decision.stop_distance_m, 0.30)
        self.assertEqual(decision.release_distance_m, 0.50)


class SafetyGateConfigTests(unittest.TestCase):
    def test_release_must_exceed_stop(self) -> None:
        with self.assertRaises(ValueError):
            SafetyGateConfig(
                stop_distance_m=1.0, release_distance_m=0.8
            ).validate()

    def test_the_roi_must_be_a_non_empty_band(self) -> None:
        with self.assertRaises(ValueError):
            SafetyGateConfig(
                roi_top_fraction=0.9, roi_bottom_fraction=0.8
            ).validate()

    def test_the_runtime_config_builds_the_gate_config(self) -> None:
        gate_config = gate_config_from_runtime(RuntimeConfig())
        self.assertTrue(gate_config.enabled)
        self.assertTrue(gate_config.require_depth)
        self.assertGreater(gate_config.release_distance_m, gate_config.stop_distance_m)
        self.assertTrue(math.isfinite(gate_config.stop_distance_m))


if __name__ == "__main__":
    unittest.main()
