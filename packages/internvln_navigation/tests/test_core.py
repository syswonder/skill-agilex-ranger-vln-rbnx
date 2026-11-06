from __future__ import annotations

import math
import threading
import unittest

from internvln_navigation.core import (
    CONTROL_DT,
    MAX_CONFIGURED_VX,
    MAX_CONFIGURED_WZ,
    MAX_PLAN_AGE_S,
    LatestFrameMailbox,
    DispatchState,
    PlanStore,
    ProgramSegment,
    RuntimeConfig,
    StepController,
    VelocityCommand,
    ZERO_COMMAND,
)
from internvln_navigation.core import InferencePlan, goal_from_program, goal_from_waypoints
from internvln_navigation.odometry import Pose


class RuntimeConfigTests(unittest.TestCase):
    def test_defaults_are_dry_run_with_an_unarmed_chassis(self) -> None:
        config = RuntimeConfig()
        self.assertEqual(config.mode, "dry-run")
        self.assertEqual(config.rgb_topic, "/camera/color/image_raw")
        self.assertEqual(config.command_topic, "/ranger/vln/cmd_vel_raw")
        self.assertEqual(config.server_url, "http://127.0.0.1:5801")
        self.assertEqual(config.selected_source, "vln")
        self.assertEqual(config.selected_output_topic, "/cmd_vel_nav")
        self.assertEqual(config.guard_output_topic, "/cmd_vel")
        # The camera frame goes out untouched by default: the server resizes
        # whatever it receives (--resize_w/h 384), which is what both the
        # deployment document's client and the Go2 skill rely on.
        self.assertEqual(config.model_input_mode, "full_frame")
        self.assertEqual(config.model_crop_size, 384)
        self.assertEqual(config.plan_hold_s, MAX_PLAN_AGE_S)

    def test_the_server_url_is_a_base_not_an_endpoint(self) -> None:
        # The InternVLA client appends /eval_dual -- the server's only route --
        # itself, so a trailing /eval_dual would double up.
        config = RuntimeConfig.from_mapping(
            {"server_url": "http://10.0.0.5:5801/"}
        )
        self.assertEqual(config.server_url, "http://10.0.0.5:5801")
        self.assertFalse(config.server_url.endswith("/eval_dual"))

    def test_server_url_rejects_credentials_and_query_strings(self) -> None:
        for bad in (
            {"server_url": "http://user:pass@10.0.0.5:5801"},
            {"server_url": "http://10.0.0.5:5801?token=abc"},
            {"server_url": "ftp://10.0.0.5:5801"},
            {"server_url": "10.0.0.5:5801"},
        ):
            with self.subTest(server_url=bad), self.assertRaises(ValueError):
                RuntimeConfig.from_mapping(bad)

    def test_control_horizon_has_one_name_and_one_value(self) -> None:
        # plan_hold_s and max_plan_age_s are the same horizon under two names;
        # allowing them to differ is how the robot ends up overrunning a step.
        config = RuntimeConfig.from_mapping({"plan_hold_s": 3.0})
        self.assertEqual(config.plan_hold_s, 3.0)
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping(
                {"plan_hold_s": 3.0, "max_plan_age_s": 1.5}
            )
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"plan_hold_s": 30.0})

    def test_mux_nesting_overrides_the_flat_keys(self) -> None:
        config = RuntimeConfig.from_mapping(
            {
                "command_topic": "/custom/vln_raw",
                "source_mux": {
                    "vln_input_topic": "/custom/vln_raw",
                    "nav_input_topic": "/custom/nav_raw",
                    "output_topic": "/custom/out",
                    "selected_source": "navigation",
                    "max_age_s": 0.5,
                },
            }
        )
        self.assertEqual(config.vln_raw_topic, "/custom/vln_raw")
        self.assertEqual(config.nav_raw_topic, "/custom/nav_raw")
        self.assertEqual(config.selected_output_topic, "/custom/out")
        self.assertEqual(config.selected_source, "navigation")
        self.assertEqual(config.source_max_age_s, 0.5)

    def test_command_topic_must_be_the_mux_input(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping(
                {
                    "command_topic": "/one",
                    "source_mux": {"vln_input_topic": "/other"},
                }
            )

    def test_live_profile_may_raise_the_forward_limit_within_the_chassis_ceiling(
        self,
    ) -> None:
        config = RuntimeConfig.from_mapping({"mode": "live", "max_vx": 0.45})
        self.assertEqual(config.max_vx, 0.45)
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"max_vx": 0.51})
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"max_wz": 0.41})

    def test_official_preprocessing_values_are_locked(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"model_input_mode": "stretch"})
        # The older crop mode is still selectable -- it is the only other
        # framing ever run on the robot -- but nothing else is.
        self.assertEqual(
            RuntimeConfig.from_mapping(
                {"model_input_mode": "center_crop_height"}
            ).model_input_mode,
            "center_crop_height",
        )
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"model_crop_size": 512})
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping(
                {"control_dt": 0.2}
            )

    def test_invalid_scalars_are_rejected(self) -> None:
        for override in (
            {"mode": "armed"},
            {"selected_source": "follow"},
            {"guard_turn_policy": "spin"},
            {"instruction": "x" * 1001},
            {"jpeg_quality": 0},
            {"jpeg_quality": 101},
            {"fault_after_failures": 0},
            {"fault_after_failures": "three"},
            {"finish_after_stops": 0},
            {"finish_after_stops": 1001},
            {"finish_after_stops": "three"},
            {"finish_after_stops_min_s": -1.0},
            {"finish_after_stops_min_s": 3601.0},
            {"finish_after_stops_min_s": "thirty"},
            {"rgb_topic": "camera/color/image_raw"},
            {"rgb_topic": "/camera/color/image_raw/"},
            {"guard_forward_only": "true"},
            {"discrete_forward_m": 0.0},
            {"lookahead_m": 0.0},
            {"lookahead_m": 6.0},
            {"guard_min_turn_radius_m": 0.0},
            {"safety_gate_enabled": "true"},
            {"safety_gate_stop_m": 0.0},
            {"safety_gate_depth_timeout_s": 0.0},
            {"safety_gate_depth_scale_m": -0.001},
            {"safety_gate_roi_width_fraction": 0.0},
            {"safety_gate_percentile": 60.0},
            {"safety_gate_min_valid_pixels": 0},
            {"depth_topic": "camera/depth/image_raw"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                RuntimeConfig.from_mapping(override)

    def test_the_proximity_gate_is_on_and_fails_closed_by_default(self) -> None:
        config = RuntimeConfig()
        self.assertTrue(config.safety_gate_enabled)
        self.assertTrue(config.safety_gate_require_depth)
        self.assertGreater(config.safety_gate_release_m, config.safety_gate_stop_m)
        # The band stops above the bottom of the image: the floor a metre in
        # front of the camera is always inside it.
        self.assertLess(config.safety_gate_roi_bottom_fraction, 1.0)

    def test_the_proximity_gate_thresholds_are_cross_checked(self) -> None:
        config = RuntimeConfig.from_mapping(
            {
                "safety_gate_enabled": False,
                "safety_gate_stop_m": 0.6,
                "safety_gate_release_m": 0.9,
                "safety_gate_roi_top_fraction": 0.3,
                "safety_gate_roi_bottom_fraction": 0.7,
            }
        )
        self.assertFalse(config.safety_gate_enabled)
        self.assertEqual(config.safety_gate_stop_m, 0.6)
        self.assertEqual(config.safety_gate_release_m, 0.9)
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping(
                {"safety_gate_stop_m": 1.0, "safety_gate_release_m": 1.0}
            )
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping(
                {
                    "safety_gate_roi_top_fraction": 0.8,
                    "safety_gate_roi_bottom_fraction": 0.8,
                }
            )

    def test_as_ros_parameters_covers_every_field(self) -> None:
        config = RuntimeConfig()
        parameters = config.as_ros_parameters()
        self.assertEqual(
            sorted(parameters), sorted(RuntimeConfig.__dataclass_fields__)
        )
        self.assertEqual(parameters["mode"], "dry-run")


class VelocityCommandTests(unittest.TestCase):
    def test_clipping_is_symmetric_and_bounded(self) -> None:
        self.assertEqual(
            VelocityCommand(1.0, -1.0).clipped(0.25, 0.4), VelocityCommand(0.25, -0.4)
        )
        self.assertEqual(VelocityCommand(0.1, 0.2).clipped(0.25, 0.4).vx, 0.1)

    def test_zero_command_is_zero(self) -> None:
        self.assertEqual((ZERO_COMMAND.vx, ZERO_COMMAND.wz), (0.0, 0.0))


class LatestFrameMailboxTests(unittest.TestCase):
    def test_only_the_newest_frame_is_retained(self) -> None:
        mailbox = LatestFrameMailbox()
        mailbox.put(b"\xff\xd8one", source_timestamp=1.0)
        second = mailbox.put(b"\xff\xd8two", source_timestamp=2.0)
        self.assertEqual(mailbox.retained_count, 1)
        self.assertIs(mailbox.latest(), second)

    def test_empty_frames_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LatestFrameMailbox().put(b"", source_timestamp=1.0)

    def test_wait_after_returns_none_on_timeout(self) -> None:
        mailbox = LatestFrameMailbox()
        frame = mailbox.put(b"\xff\xd8one", source_timestamp=1.0)
        self.assertIsNone(
            mailbox.wait_after(frame.sequence, timeout_s=0.05)
        )

    def test_wait_after_wakes_on_a_newer_frame(self) -> None:
        mailbox = LatestFrameMailbox()
        first = mailbox.put(b"\xff\xd8one", source_timestamp=1.0)

        def later() -> None:
            mailbox.put(b"\xff\xd8two", source_timestamp=2.0)

        threading.Timer(0.05, later).start()
        frame = mailbox.wait_after(first.sequence, timeout_s=2.0)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.sequence, first.sequence + 1)

    def test_wait_after_honours_a_stop_event(self) -> None:
        mailbox = LatestFrameMailbox()
        stop = threading.Event()
        stop.set()
        self.assertIsNone(mailbox.wait_after(0, timeout_s=5.0, stop_event=stop))


class PlanStoreTests(unittest.TestCase):
    def test_no_plan_dispatches_zero(self) -> None:
        state = PlanStore().dispatch(now=100.0)
        self.assertEqual(state.reason, "no_plan")
        self.assertEqual(state.command, ZERO_COMMAND)

    def test_a_fresh_plan_is_active_and_then_expires(self) -> None:
        # Limits are opened up so this test is about the clock, not about the
        # clip; the packaged defaults would clamp 0.2 to 0.15 and the assertions
        # below would then be measuring the wrong thing.
        store = PlanStore(
            max_plan_age_s=1.5,
            max_vx=MAX_CONFIGURED_VX,
            max_wz=MAX_CONFIGURED_WZ,
        )
        store.update(
            InferencePlan(command=VelocityCommand(0.2, 0.1), waypoints=None),
            now=100.0,
        )
        active = store.dispatch(now=100.5)
        self.assertEqual(active.reason, "active_plan")
        self.assertEqual(active.command.vx, 0.2)
        stale = store.dispatch(now=101.6)
        self.assertEqual(stale.reason, "stale_plan")
        self.assertEqual(stale.command, ZERO_COMMAND)

    def test_updates_are_clipped_to_the_configured_limits(self) -> None:
        store = PlanStore(max_vx=0.15, max_wz=0.30)
        store.update(
            InferencePlan(command=VelocityCommand(5.0, -5.0), waypoints=None),
            now=0.0,
        )
        state = store.dispatch(now=0.0)
        self.assertEqual(state.command.vx, 0.15)
        self.assertEqual(state.command.wz, -0.30)

    def test_a_committed_chunk_outlives_plan_hold_s(self) -> None:
        # Four 1 s actions are four seconds of motion the model already asked
        # for; the watchdog must not cut it off at plan_hold_s.  It is a brake
        # for silent networks, not for answers that are still being driven.
        store = PlanStore(max_plan_age_s=1.5, max_vx=0.5, max_wz=0.5)
        program = tuple(
            ProgramSegment(VelocityCommand(0.25, 0.0), 1.0) for _ in range(4)
        )
        store.update(
            InferencePlan(
                command=VelocityCommand(0.25, 0.0), waypoints=None, program=program
            ),
            now=0.0,
        )
        self.assertEqual(store.dispatch(now=3.9).reason, "active_plan")
        self.assertEqual(store.dispatch(now=4.1).reason, "stale_plan")

    def test_a_plan_without_a_program_still_expires_at_the_watchdog(self) -> None:
        store = PlanStore(max_plan_age_s=1.5)
        store.update(
            InferencePlan(command=VelocityCommand(0.1, 0.0), waypoints=None),
            now=0.0,
        )
        self.assertEqual(store.dispatch(now=1.4).reason, "active_plan")
        self.assertEqual(store.dispatch(now=1.6).reason, "stale_plan")

    def test_the_program_is_clipped_segment_by_segment(self) -> None:
        store = PlanStore(max_vx=0.15, max_wz=0.30)
        store.update(
            InferencePlan(
                command=VelocityCommand(5.0, 0.0),
                waypoints=None,
                program=(
                    ProgramSegment(VelocityCommand(5.0, 0.0), 1.0),
                    ProgramSegment(VelocityCommand(0.0, -5.0), 1.0),
                ),
            ),
            now=0.0,
        )
        program = store.dispatch(now=0.0).program
        self.assertEqual(program[0].command.vx, 0.15)
        self.assertEqual(program[1].command.wz, -0.30)

    def test_the_program_keeps_the_goal_each_segment_is_for(self) -> None:
        # The step controller needs turn_rad / distance_m to end a segment on
        # odometry.  The store rebuilds every segment (to clip it), so it has
        # to carry those two fields across or the closed loop silently dies.
        store = PlanStore(max_vx=0.15, max_wz=0.30)
        store.update(
            InferencePlan(
                command=VelocityCommand(0.25, 0.0),
                waypoints=None,
                program=(
                    ProgramSegment(
                        VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25
                    ),
                    ProgramSegment(
                        VelocityCommand(0.0, 0.30), 0.87, turn_rad=0.26
                    ),
                ),
            ),
            now=0.0,
        )
        program = store.dispatch(now=0.0).program
        self.assertEqual(program[0].distance_m, 0.25)
        self.assertEqual(program[0].turn_rad, 0.0)
        self.assertEqual(program[1].turn_rad, 0.26)

    def test_the_watchdog_outlives_a_segment_waiting_on_odometry(self) -> None:
        # A goal-bearing segment may run up to segment_timeout_factor x its
        # duration while /odom has not reported "done" yet.  If the watchdog
        # expired first it would be the watchdog -- not the step controller --
        # that ends the step, and the robot would stop short silently.
        store = PlanStore(max_plan_age_s=1.5, segment_timeout_factor=2.0)
        store.update(
            InferencePlan(
                command=VelocityCommand(0.25, 0.0),
                waypoints=None,
                program=(
                    ProgramSegment(VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25),
                ),
            ),
            now=0.0,
        )
        self.assertEqual(store.dispatch(now=1.9).reason, "active_plan")
        self.assertEqual(store.dispatch(now=2.1).reason, "stale_plan")

    def test_the_watchdog_ignores_the_factor_for_goalless_segments(self) -> None:
        store = PlanStore(max_plan_age_s=1.5, segment_timeout_factor=2.0)
        store.update(
            InferencePlan(
                command=VelocityCommand(0.25, 0.0),
                waypoints=None,
                program=(ProgramSegment(VelocityCommand(0.25, 0.0), 1.0),),
            ),
            now=0.0,
        )
        self.assertEqual(store.dispatch(now=1.6).reason, "stale_plan")

    def test_clear_retires_the_plan_immediately(self) -> None:
        store = PlanStore()
        store.update(
            InferencePlan(command=VelocityCommand(0.1, 0.0), waypoints=None),
            now=0.0,
        )
        before = store.dispatch(now=0.0).version
        store.clear()
        after = store.dispatch(now=0.0)
        self.assertEqual(after.reason, "no_plan")
        self.assertGreater(after.version, before)


class StepControllerTests(unittest.TestCase):
    """Stop-and-go: one plan version drives exactly one timed step."""

    def setUp(self) -> None:
        self.command = VelocityCommand(0.25, 0.0)

    def _state(self, now: float, version: int = 1, age: float = 0.0) -> DispatchState:
        return DispatchState(self.command, "active_plan", age, version)

    def test_a_step_is_driven_for_exactly_action_step_s(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5)
        stepper.apply(self._state(0.0), now=0.0)
        command, reason = stepper.apply(self._state(0.9), now=0.9)
        self.assertEqual(command, self.command)
        self.assertEqual(reason, "step")
        command, reason = stepper.apply(self._state(1.0), now=1.0)
        self.assertEqual(command, ZERO_COMMAND)
        self.assertEqual(reason, "step_done")

    def test_a_slow_model_pauses_instead_of_multiplying_the_motion(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5)
        stepper.apply(self._state(0.0), now=0.0)
        # The next answer arrives at 3 s; the same plan version is still
        # active (its watchdog is 10 s here) but must not be re-driven.
        for now in (1.5, 2.0, 2.9):
            command, _ = stepper.apply(self._state(now), now=now)
            self.assertEqual(command, ZERO_COMMAND)
        # A new plan restarts the clock and gets its own full step.
        command, reason = stepper.apply(self._state(3.0, version=2), now=3.0)
        self.assertEqual(command, self.command)
        self.assertEqual(reason, "step")

    def test_the_gate_reopens_only_after_the_chassis_has_settled(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5)
        stepper.apply(self._state(0.0), now=0.0)
        self.assertFalse(stepper.ready(now=0.9))
        self.assertFalse(stepper.ready(now=1.0))
        self.assertFalse(stepper.ready(now=1.4))
        self.assertTrue(stepper.ready(now=1.5))

    def test_the_settle_window_survives_the_plan_expiring(self) -> None:
        # With action_step_s == plan_hold_s the watchdog fires exactly when
        # the step ends; the robot is still rolling and must not be looked at.
        stepper = StepController(step_s=1.0, settle_s=0.5)
        stepper.apply(self._state(0.0), now=0.0)
        stepper.apply(DispatchState(ZERO_COMMAND, "stale_plan", 1.0, 1), now=1.0)
        self.assertFalse(stepper.ready(now=1.2))
        self.assertTrue(stepper.ready(now=1.5))

    def test_no_plan_means_no_step_and_an_open_gate(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5)
        command, reason = stepper.apply(
            DispatchState(ZERO_COMMAND, "no_plan", None, 0), now=0.0
        )
        self.assertEqual(command, ZERO_COMMAND)
        self.assertEqual(reason, "no_plan")
        self.assertTrue(stepper.ready(now=0.0))

    def test_disabled_is_the_old_free_running_behaviour(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5, enabled=False)
        command, reason = stepper.apply(self._state(99.0), now=99.0)
        self.assertEqual(command, self.command)
        self.assertEqual(reason, "active_plan")
        self.assertTrue(stepper.ready(now=99.0))

    # ----------------------------------------------------------- chunk program

    def _program_state(self, now: float, version: int = 1) -> DispatchState:
        return DispatchState(
            VelocityCommand(0.25, 0.0),
            "active_plan",
            0.0,
            version,
            (
                ProgramSegment(VelocityCommand(0.25, 0.0), 1.0),
                ProgramSegment(VelocityCommand(0.0, 0.30), 0.5),
                ProgramSegment(VelocityCommand(0.20, 0.0), 1.0),
            ),
        )

    def test_a_chunk_is_driven_segment_by_segment_in_order(self) -> None:
        # [1, 2, 1] is three moves, not one: forward, then left, then forward,
        # each held for the duration its own geometry gave it.
        stepper = StepController(step_s=1.0, settle_s=0.5)
        expected = [
            (0.0, VelocityCommand(0.25, 0.0)),
            (0.99, VelocityCommand(0.25, 0.0)),
            (1.0, VelocityCommand(0.0, 0.30)),
            (1.49, VelocityCommand(0.0, 0.30)),
            (1.5, VelocityCommand(0.20, 0.0)),
            (2.49, VelocityCommand(0.20, 0.0)),
        ]
        for now, command in expected:
            got, reason = stepper.apply(self._program_state(now), now=now)
            self.assertEqual(got, command, f"at t={now}")
            self.assertEqual(reason, "step")
        command, reason = stepper.apply(self._program_state(2.5), now=2.5)
        self.assertEqual(command, ZERO_COMMAND)
        self.assertEqual(reason, "step_done")

    def test_the_gate_waits_for_the_end_of_the_chunk(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5)
        stepper.apply(self._program_state(0.0), now=0.0)
        self.assertEqual(stepper.segments, 3)
        self.assertFalse(stepper.ready(now=1.0))
        self.assertFalse(stepper.ready(now=2.5))
        self.assertTrue(stepper.ready(now=3.0))

    def test_an_empty_program_still_holds_one_action_step(self) -> None:
        # Hand-built plans (operator stop, tests) carry no program; they keep
        # the old "one step_s" behaviour rather than becoming instantaneous.
        stepper = StepController(step_s=1.0, settle_s=0.5)
        stepper.apply(self._state(0.0), now=0.0)
        self.assertEqual(stepper.segments, 1)
        command, _ = stepper.apply(self._state(0.99), now=0.99)
        self.assertEqual(command, self.command)
        command, _ = stepper.apply(self._state(1.0), now=1.0)
        self.assertEqual(command, ZERO_COMMAND)

    def test_a_new_plan_mid_chunk_restarts_the_program(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5)
        stepper.apply(self._program_state(0.0, version=1), now=0.0)
        command, _ = stepper.apply(self._program_state(0.6, version=2), now=0.6)
        # Version 2 starts from its own first segment, not from the middle of
        # the old chunk.
        self.assertEqual(command, VelocityCommand(0.25, 0.0))
        self.assertEqual(stepper.segments, 3)


class LockstepConfigTests(unittest.TestCase):
    def test_lockstep_is_off_by_default_because_the_servo_drives(self) -> None:
        # Stop-and-go exists to guarantee the model always sees a still robot.
        # The servo guarantees the same thing by stamping the pose on the frame,
        # so leaving both on would only re-add the pause the servo removed.
        config = RuntimeConfig()
        self.assertFalse(config.lockstep_enabled)
        self.assertTrue(config.servo_enabled)
        # The timings are still the fallback path's, and still validated.
        self.assertEqual(config.action_step_s, 1.0)
        self.assertEqual(config.step_settle_s, 0.5)

    def test_the_step_must_not_outlive_its_watchdog(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"action_step_s": 2.0, "plan_hold_s": 1.5})

    def test_the_step_can_be_configured_independently_of_the_hold(self) -> None:
        config = RuntimeConfig.from_mapping(
            {"action_step_s": 0.8, "step_settle_s": 0.6, "plan_hold_s": 2.0}
        )
        self.assertEqual(config.action_step_s, 0.8)
        self.assertEqual(config.step_settle_s, 0.6)
        self.assertEqual(config.plan_hold_s, 2.0)

    def test_a_negative_settle_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"step_settle_s": -0.1})


class InferencePlanDefaultsTests(unittest.TestCase):
    def test_default_control_dt_is_recorded_not_used_as_a_divisor(self) -> None:
        plan = InferencePlan(command=VelocityCommand(), waypoints=None)
        self.assertEqual(plan.control_dt, CONTROL_DT)
        self.assertEqual(plan.velocity_source, "discrete_action")


class StepControllerFeedbackTests(unittest.TestCase):
    """Closed loop: a segment ends when /odom says it was achieved.

    Every case here has a nominal duration of 1.0 s, so "ended early" and
    "ended on time" are unambiguous.
    """

    def _turn_state(self, version: int = 1, deg: float = 15.0) -> DispatchState:
        import math

        return DispatchState(
            VelocityCommand(0.0, 0.30),
            "active_plan",
            0.0,
            version,
            (
                ProgramSegment(
                    VelocityCommand(0.0, 0.30),
                    1.0,
                    turn_rad=math.radians(deg),
                ),
            ),
        )

    def _forward_state(self, version: int = 1, metres: float = 0.25) -> DispatchState:
        return DispatchState(
            VelocityCommand(0.25, 0.0),
            "active_plan",
            0.0,
            version,
            (
                ProgramSegment(
                    VelocityCommand(0.25, 0.0),
                    1.0,
                    distance_m=metres,
                ),
            ),
        )

    def test_a_turn_ends_as_soon_as_the_angle_is_measured(self) -> None:
        import math

        stepper = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=True)
        stepper.apply(self._turn_state(), now=0.0, pose=Pose(0.0, 0.0, 0.0))
        # 14 degrees after 0.6 s: the reset of the nominal second is not spent.
        command, reason = stepper.apply(
            self._turn_state(), now=0.6, pose=Pose(0.0, 0.0, math.radians(14.0))
        )
        self.assertEqual(reason, "step_done")
        self.assertEqual(command, ZERO_COMMAND)
        self.assertTrue(stepper.ready(now=1.1))

    def test_a_turn_that_never_arrives_stops_at_its_timeout(self) -> None:
        stepper = StepController(
            step_s=1.0, settle_s=0.5, feedback_enabled=True, timeout_factor=2.0
        )
        stepper.apply(self._turn_state(), now=0.0, pose=Pose())
        # The chassis is stuck at 2 degrees: the segment must not run forever.
        command, reason = stepper.apply(self._turn_state(), now=1.9, pose=Pose(yaw=0.03))
        self.assertEqual(reason, "step")
        self.assertEqual(command, VelocityCommand(0.0, 0.30))
        command, reason = stepper.apply(self._turn_state(), now=2.0, pose=Pose(yaw=0.03))
        self.assertEqual(reason, "step_done")
        self.assertEqual(command, ZERO_COMMAND)

    def test_a_forward_segment_ignores_sideways_motion(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=True)
        stepper.apply(self._forward_state(), now=0.0, pose=Pose())
        # Slid 0.5 m sideways (a turn still finishing): not forward progress.
        _, reason = stepper.apply(
            self._forward_state(), now=0.5, pose=Pose(0.0, 0.5, 0.0)
        )
        self.assertEqual(reason, "step")
        # 0.24 m forward: inside the 0.02 m tolerance.
        _, reason = stepper.apply(
            self._forward_state(), now=0.6, pose=Pose(0.24, 0.5, 0.0)
        )
        self.assertEqual(reason, "step_done")

    def test_a_turn_measured_in_the_wrong_direction_is_not_done(self) -> None:
        import math

        stepper = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=True)
        stepper.apply(self._turn_state(), now=0.0, pose=Pose())
        # Asked to turn left, the chassis turned right 20 degrees.
        _, reason = stepper.apply(
            self._turn_state(), now=0.9, pose=Pose(yaw=math.radians(-20.0))
        )
        self.assertEqual(reason, "step")

    def test_no_pose_falls_back_to_the_nominal_duration(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=True)
        stepper.apply(self._turn_state(), now=0.0, pose=None)
        _, reason = stepper.apply(self._turn_state(), now=0.99, pose=None)
        self.assertEqual(reason, "step")
        _, reason = stepper.apply(self._turn_state(), now=1.0, pose=None)
        self.assertEqual(reason, "step_done")

    def test_feedback_off_is_the_old_timing_behaviour(self) -> None:
        import math

        stepper = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=False)
        stepper.apply(self._turn_state(), now=0.0, pose=Pose())
        # The angle is achieved, but feedback is off: the timer still decides.
        _, reason = stepper.apply(
            self._turn_state(), now=0.6, pose=Pose(yaw=math.radians(90.0))
        )
        self.assertEqual(reason, "step")
        _, reason = stepper.apply(
            self._turn_state(), now=1.0, pose=Pose(yaw=math.radians(90.0))
        )
        self.assertEqual(reason, "step_done")

    def test_a_chunk_ends_each_segment_on_its_own_measurement(self) -> None:
        import math

        state = DispatchState(
            VelocityCommand(0.25, 0.0),
            "active_plan",
            0.0,
            1,
            (
                ProgramSegment(VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25),
                ProgramSegment(
                    VelocityCommand(0.0, 0.30), 0.87, turn_rad=math.radians(15.0)
                ),
            ),
        )
        stepper = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=True)
        stepper.apply(state, now=0.0, pose=Pose())
        # Forward done in 0.4 s (odometry says 0.25 m), so the turn starts here.
        command, reason = stepper.apply(state, now=0.4, pose=Pose(x=0.25))
        self.assertEqual(reason, "step")
        self.assertEqual(command, VelocityCommand(0.0, 0.30))
        self.assertEqual(stepper.progress()["goal"], "turn +15.0 deg")
        # The turn is measured from the pose it started in, not from (0, 0).
        command, reason = stepper.apply(
            state, now=1.2, pose=Pose(x=0.25, yaw=math.radians(16.0))
        )
        self.assertEqual(reason, "step_done")
        self.assertEqual(command, ZERO_COMMAND)

    def test_progress_says_what_ends_the_segment(self) -> None:
        stepper = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=True)
        stepper.apply(self._forward_state(), now=0.0, pose=Pose())
        shown = stepper.progress()
        self.assertEqual(shown["segments"], 1)
        self.assertEqual(shown["segment"], 1)
        self.assertEqual(shown["goal"], "forward 0.250 m")
        self.assertEqual(shown["feedback"], "odom")
        # Without a pose the same segment is on the timer, and says so.
        timer = StepController(step_s=1.0, settle_s=0.5, feedback_enabled=True)
        timer.apply(self._forward_state(), now=0.0, pose=None)
        self.assertEqual(timer.progress()["feedback"], "timer")


class OdomConfigTests(unittest.TestCase):
    def test_closed_loop_defaults_are_on_and_measured_in_real_units(self) -> None:
        config = RuntimeConfig()
        self.assertTrue(config.odom_feedback_enabled)
        self.assertEqual(config.odom_topic, "/odom")
        self.assertLess(config.odom_turn_tolerance_deg, config.discrete_step_deg)
        self.assertLess(config.odom_distance_tolerance_m, config.discrete_forward_m)

    def test_the_rollback_switch_parses(self) -> None:
        parsed = RuntimeConfig.from_mapping({"odom_feedback_enabled": False})
        self.assertFalse(parsed.odom_feedback_enabled)
        # The other flags in this config reject strings the same way: YAML
        # already gives a bool, and "false" silently read as true is exactly
        # the failure this guards against.
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"odom_feedback_enabled": "false"})

    def test_a_timeout_factor_below_one_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"odom_segment_timeout_factor": 0.5})


class ServoConfigTests(unittest.TestCase):
    def test_the_servo_is_on_by_default_with_damped_gains(self) -> None:
        config = RuntimeConfig()
        self.assertTrue(config.servo_enabled)
        # controllers.py uses 2.0/1.5 against 0.6/0.5 m/s caps.  Ours are
        # 0.25/0.30, where those gains saturate at 12.5 cm / 11 deg and turn
        # the approach into bang-bang; 1.2/1.0 was chosen for this chassis.
        self.assertEqual(config.servo_kp_trans, 1.2)
        self.assertEqual(config.servo_kp_yaw, 1.0)
        self.assertGreater(config.servo_position_tolerance_m, 0.0)
        self.assertGreater(config.servo_yaw_tolerance_deg, 0.0)

    def test_the_rollback_switch_parses(self) -> None:
        self.assertFalse(RuntimeConfig.from_mapping({"servo_enabled": False}).servo_enabled)
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"servo_enabled": "false"})

    def test_a_zero_gain_or_tolerance_is_rejected(self) -> None:
        for key in (
            "servo_kp_trans",
            "servo_kp_yaw",
            "servo_position_tolerance_m",
            "servo_yaw_tolerance_deg",
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                RuntimeConfig.from_mapping({key: 0.0})


class PlanGoalTests(unittest.TestCase):
    """An answer becomes one world pose, measured from the frame's pose."""

    def test_a_chunk_is_applied_in_order_from_the_frame_pose(self) -> None:
        program = (
            ProgramSegment(VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25),
            ProgramSegment(VelocityCommand(0.0, 0.26), 1.0, turn_rad=0.2617993877991494),
            ProgramSegment(VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25),
        )
        goal = goal_from_program(Pose(), program)
        self.assertIsNotNone(goal)
        # Forward, left 15 degrees, forward again: the second leg is no longer
        # along +x, which is the whole reason the order matters.
        self.assertAlmostEqual(goal.yaw, 0.2617993877991494)
        self.assertAlmostEqual(goal.x, 0.25 + 0.25 * 0.9659258263, places=6)
        self.assertAlmostEqual(goal.y, 0.25 * 0.2588190451, places=6)

    def test_a_plan_without_geometry_has_no_goal(self) -> None:
        # A STOP, a camera-only answer (5/9) and a hand-built plan all hold a
        # velocity; none of them names a place, so there is nothing to servo to.
        program = (ProgramSegment(VelocityCommand(0.0, 0.0), 1.0),)
        self.assertIsNone(goal_from_program(Pose(), program))
        self.assertIsNone(goal_from_program(Pose(), ()))

    def test_the_store_carries_the_goal_and_the_pose_it_came_from(self) -> None:
        store = PlanStore(max_plan_age_s=1.5)
        frame_pose = Pose(x=1.0, y=2.0, yaw=0.0)
        store.update(
            InferencePlan(
                command=VelocityCommand(0.25, 0.0),
                waypoints=None,
                program=(ProgramSegment(VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25),),
            ),
            now=0.0,
            reference_pose=frame_pose,
        )
        state = store.dispatch(now=0.1)
        self.assertEqual(state.reference_pose, frame_pose)
        self.assertIsNotNone(state.goal)
        self.assertAlmostEqual(state.goal.x, 1.25)
        self.assertAlmostEqual(state.goal.y, 2.0)

    def test_without_a_pose_the_plan_has_no_goal(self) -> None:
        # No odometry on the frame: the dispatch tick must fall back to the
        # step controller instead of servoing toward a pose it invented.
        store = PlanStore(max_plan_age_s=1.5)
        store.update(
            InferencePlan(
                command=VelocityCommand(0.25, 0.0),
                waypoints=None,
                program=(ProgramSegment(VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25),),
            ),
            now=0.0,
        )
        self.assertIsNone(store.dispatch(now=0.1).goal)

    def test_clearing_the_store_clears_the_goal(self) -> None:
        store = PlanStore(max_plan_age_s=1.5)
        store.update(
            InferencePlan(
                command=VelocityCommand(0.25, 0.0),
                waypoints=None,
                program=(ProgramSegment(VelocityCommand(0.25, 0.0), 1.0, distance_m=0.25),),
            ),
            now=0.0,
            reference_pose=Pose(),
        )
        store.clear()
        self.assertIsNone(store.dispatch(now=0.1).goal)

    def test_a_trajectory_becomes_a_goal_from_the_same_pose(self) -> None:
        # A trajectory is drawn in the robot's frame, so it is a relative
        # motion like a discrete step and lands on the answer's own pose.  The
        # point pursued is the lookahead one, and the heading turns to face it,
        # so the servo arrives aligned with the path instead of side-stepping
        # onto it.
        goal = goal_from_waypoints(
            Pose(x=1.0, y=2.0, yaw=math.pi / 2.0),
            ((0.1, 0.0), (0.3, 0.0), (0.5, 0.0)),
            lookahead_m=0.30,
            stop_m=0.05,
        )
        self.assertIsNotNone(goal)
        self.assertAlmostEqual(goal.x, 1.0, places=6)
        self.assertAlmostEqual(goal.y, 2.3, places=6)
        self.assertAlmostEqual(goal.yaw, math.pi / 2.0, places=6)

    def test_a_trajectory_goal_is_bounded_by_the_advance(self) -> None:
        # The bearing still comes from the lookahead point, but one answer only
        # commits to ``advance_m`` of the path -- a one-metre servo goal is
        # what made the robot hold full throttle until it got there.
        waypoints = ((0.6, 0.0), (1.0, 0.0))
        bounded = goal_from_waypoints(
            Pose(),
            waypoints,
            lookahead_m=1.0,
            stop_m=0.05,
            advance_m=0.35,
        )
        self.assertIsNotNone(bounded)
        self.assertAlmostEqual(bounded.x, 0.35, places=6)
        # No bound (the old behaviour) still reaches the whole displacement.
        unbounded = goal_from_waypoints(
            Pose(), waypoints, lookahead_m=1.0, stop_m=0.05
        )
        self.assertIsNotNone(unbounded)
        self.assertAlmostEqual(unbounded.x, 1.0, places=6)

    def test_a_trajectory_inside_the_arrival_box_has_no_goal(self) -> None:
        # "Stay here" is not a target two centimetres away: with a goal the
        # servo would creep onto it and report done out of tolerance, and the
        # model's own STOP -- what actually finishes a task -- would never be
        # seen.
        self.assertIsNone(
            goal_from_waypoints(Pose(), ((0.01, 0.0), (0.03, 0.0)), stop_m=0.05)
        )
        self.assertIsNone(goal_from_waypoints(Pose(), (), stop_m=0.05))
        self.assertIsNone(goal_from_waypoints(Pose(), ((),), stop_m=0.05))

    def test_the_store_derives_a_goal_from_a_trajectory_plan(self) -> None:
        store = PlanStore(max_plan_age_s=1.5, lookahead_m=0.30, trajectory_stop_m=0.05)
        store.update(
            InferencePlan(
                command=VelocityCommand(0.10, 0.0),
                waypoints=((0.1, 0.0), (0.3, 0.0)),
                # No segment geometry: this is exactly the shape the adapter
                # builds for a trajectory, and the reason it used to have no
                # goal and drive open-loop.
                program=(ProgramSegment(VelocityCommand(0.10, 0.0), 1.2),),
            ),
            now=0.0,
            reference_pose=Pose(),
        )
        state = store.dispatch(now=0.1)
        self.assertIsNotNone(state.goal)
        self.assertAlmostEqual(state.goal.x, 0.30)
        self.assertEqual(state.waypoints, ((0.1, 0.0), (0.3, 0.0)))


if __name__ == "__main__":
    unittest.main()
