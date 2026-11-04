"""ROS 2 node publishing a guarded, continuously refreshed ``/cmd_vel``.

The command policy itself lives in :mod:`internvln_navigation.chassis_guard`;
this module is only the ROS plumbing around it.  It is the last thing to touch
a command before the chassis and the only writer of ``/cmd_vel``.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from geometry_msgs.msg import Twist
from rclpy.context import Context
from rclpy.node import Node
from rclpy.parameter import Parameter

from .chassis_guard import rate_limit, shape_command
from .core import RuntimeConfig


# The subset of RuntimeConfig this node owns.  The whole config is still parsed
# so the manifest keeps one source of truth for validation, but only these keys
# are declared as ROS parameters on this node.
GUARD_PARAMETER_NAMES: tuple[str, ...] = (
    "guard_input_topic",
    "guard_output_topic",
    "guard_forward_only",
    "guard_max_vx_mps",
    "guard_max_wz_rps",
    "guard_max_linear_accel_mps2",
    "guard_max_angular_accel_rps2",
    "guard_command_timeout_s",
    "guard_publish_hz",
    "guard_min_turn_radius_m",
    "guard_turn_policy",
    "guard_spin_vx_epsilon_mps",
)


class RangerChassisGuardNode(Node):
    """Republish the selected command through the Ranger chassis contract."""

    def __init__(
        self,
        *,
        config_overrides: Mapping[str, Any] | None = None,
        context: Context | None = None,
    ) -> None:
        defaults = RuntimeConfig()
        overrides = [
            Parameter(name, value=value)
            for name, value in dict(config_overrides or {}).items()
            if name in GUARD_PARAMETER_NAMES
        ]
        super().__init__(
            "ranger_chassis_guard",
            context=context,
            parameter_overrides=overrides,
        )
        values: dict[str, Any] = {}
        for name in GUARD_PARAMETER_NAMES:
            values[name] = self.declare_parameter(
                name, getattr(defaults, name)
            ).value
        # The guard keys are the only ones overridden, so every unrelated field
        # falls back to the package default and RuntimeConfig's cross-field
        # validation still sees a complete, valid configuration.
        self._config = RuntimeConfig.from_mapping(values)

        self._pending: tuple[float, float, float] | None = None
        self._output = (0.0, 0.0)
        self._last_tick = time.monotonic()
        self._last_note_log = 0.0
        self._last_extra_field_log = 0.0

        self._publisher = self.create_publisher(
            Twist, self._config.guard_output_topic, 1
        )
        self._subscription = self.create_subscription(
            Twist,
            self._config.guard_input_topic,
            self._on_command,
            1,
        )
        self._timer = self.create_timer(
            1.0 / self._config.guard_publish_hz, self._tick
        )
        self.get_logger().info(
            "Ranger chassis guard active: "
            f"{self._config.guard_input_topic} -> "
            f"{self._config.guard_output_topic} "
            f"@ {self._config.guard_publish_hz:.0f} Hz, "
            f"max_vx={self._config.guard_max_vx_mps:.3f} m/s, "
            f"max_wz={self._config.guard_max_wz_rps:.3f} rad/s, "
            f"min_turn_radius={self._config.guard_min_turn_radius_m:.3f} m, "
            f"turn_policy={self._config.guard_turn_policy}"
        )

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self._config

    def _on_command(self, message: Twist) -> None:
        # The Ranger switches its entire steering geometry on a non-zero
        # linear.y and ignores the other fields, so surface anything that would
        # silently change the motion mode instead of forwarding it.
        extra = (
            message.linear.y,
            message.linear.z,
            message.angular.x,
            message.angular.y,
        )
        if any(field != 0.0 for field in extra):
            now = time.monotonic()
            if now - self._last_extra_field_log >= 5.0:
                self._last_extra_field_log = now
                self.get_logger().warning(
                    "guard input carries non-planar Twist fields "
                    f"(linear.y={message.linear.y}, "
                    f"linear.z={message.linear.z}, "
                    f"angular.x={message.angular.x}, "
                    f"angular.y={message.angular.y}); they are zeroed before "
                    "the chassis because a non-zero linear.y switches the "
                    "Ranger into PARALLEL/SIDE_SLIP steering"
                )
        self._pending = (
            float(message.linear.x),
            float(message.angular.z),
            time.monotonic(),
        )

    def _tick(self) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now

        pending = self._pending
        if pending is None:
            self._stop("no command received yet")
            return
        vx, wz, stamp = pending
        if now - stamp > self._config.guard_command_timeout_s:
            # A timeout is a fail-safe, not a trajectory: drop to zero in one
            # step exactly as go2_chassis's 0.25 s command timeout did, rather
            # than ramping down and coasting further.
            self._stop("command timed out")
            return

        shaped = shape_command(
            vx,
            wz,
            forward_only=self._config.guard_forward_only,
            max_vx=self._config.guard_max_vx_mps,
            max_wz=self._config.guard_max_wz_rps,
            spin_vx_epsilon=self._config.guard_spin_vx_epsilon_mps,
            min_turn_radius=self._config.guard_min_turn_radius_m,
            turn_policy=self._config.guard_turn_policy,
        )
        self._output = (
            rate_limit(
                self._output[0],
                shaped.vx,
                self._config.guard_max_linear_accel_mps2,
                dt,
            ),
            rate_limit(
                self._output[1],
                shaped.wz,
                self._config.guard_max_angular_accel_rps2,
                dt,
            ),
        )
        self._publish(*self._output)
        self._log_note(shaped.note, now)

    def _stop(self, reason: str) -> None:
        if self._output != (0.0, 0.0):
            self._output = (0.0, 0.0)
            self._log_note(f"holding zero: {reason}", time.monotonic())
        self._publish(0.0, 0.0)

    def _publish(self, vx: float, wz: float) -> None:
        message = Twist()
        message.linear.x = float(vx)
        message.angular.z = float(wz)
        # Every other field stays at its zero default so the Ranger can never
        # leave DUAL_ACKERMAN mode by accident.
        self._publisher.publish(message)

    def _log_note(self, note: str, now: float) -> None:
        if not note or now - self._last_note_log < 1.0:
            return
        self._last_note_log = now
        self.get_logger().warning(f"chassis guard adjusted command: {note}")


def main() -> None:
    import rclpy

    rclpy.init(args=None)
    node = RangerChassisGuardNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
