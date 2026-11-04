"""Launch the InternVLA bridge and, in live mode, the Ranger chassis guard.

The guard node is started only when ``mode`` is ``live``.  That mirrors what the
Robonix provider does internally, and it matters: in dry-run the bridge
publishes no Twist at all, so a guard would contribute nothing but a 50 Hz
stream of zeros on ``/cmd_vel``.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


PACKAGE_NAME = "internvln_navigation"


def generate_launch_description() -> LaunchDescription:
    config = LaunchConfiguration("config")
    mode = LaunchConfiguration("mode")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "config",
                default_value="",
                description="Absolute path to an internvln_navigation parameter file.",
            ),
            DeclareLaunchArgument(
                "mode",
                default_value="dry-run",
                description=(
                    "'dry-run' publishes no velocity; 'live' also starts the "
                    "Ranger chassis guard."
                ),
            ),
            Node(
                package=PACKAGE_NAME,
                executable="internvln_node",
                name="internvln_navigation",
                output="screen",
                parameters=[config, {"mode": mode}],
            ),
            Node(
                package=PACKAGE_NAME,
                executable="internvln_chassis_guard",
                name="ranger_chassis_guard",
                output="screen",
                parameters=[config],
                # `IfCondition` treats only "true"/"1" as true, so the mode
                # string has to be compared explicitly rather than passed in.
                condition=IfCondition(
                    PythonExpression(["'", mode, "' == 'live'"])
                ),
            ),
        ]
    )
